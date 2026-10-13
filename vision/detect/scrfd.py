"""
SCRFD face detector (det_10g.onnx from the InsightFace buffalo_l pack).

Implemented directly against ONNX Runtime rather than importing insightface,
which keeps the vision package to numpy + OpenCV + onnxruntime and avoids
pulling a build-from-source dependency into the image.

Model signature, confirmed with ``scripts/download_models.py --inspect``::

    in   input.1   [1, 3, H, W]
    out  (9 tensors, 3 strides x {score, bbox, kps})
         scores  [12800,1]  [3200,1]  [800,1]
         bboxes  [12800,4]  [3200,4]  [800,4]
         kps     [12800,10] [3200,10] [800,10]

12800 = (640/8)^2 x 2 anchors, 3200 = (640/16)^2 x 2, 800 = (640/32)^2 x 2,
so strides are (8, 16, 32) with two anchors per location. Outputs arrive
grouped by type: indices 0-2 are scores, 3-5 are boxes, 6-8 are keypoints.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger("vision.detect")

STRIDES = (8, 16, 32)
ANCHORS_PER_LOCATION = 2
FEATURE_MAP_COUNT = len(STRIDES)

# SCRFD was trained with this normalisation; changing it silently wrecks
# detection quality rather than erroring.
INPUT_MEAN = 127.5
INPUT_STD = 128.0


@dataclass
class Face:
    """One detected face in image coordinates."""

    bbox: np.ndarray  # [x1, y1, x2, y2] float32
    landmarks: np.ndarray  # 5x2 float32
    score: float

    @property
    def width(self) -> int:
        return int(round(float(self.bbox[2] - self.bbox[0])))

    @property
    def height(self) -> int:
        return int(round(float(self.bbox[3] - self.bbox[1])))

    @property
    def interocular(self) -> float:
        """Eye-to-eye distance. A more honest size measure than box width,
        which grows with hair and chin."""
        return float(np.linalg.norm(self.landmarks[1] - self.landmarks[0]))

    def bbox_int(self) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = (int(round(float(v))) for v in self.bbox)
        return x1, y1, x2, y2


class SCRFDDetector:
    """Face detector. One instance per process; sessions are not thread-safe."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        providers: list[str] | None = None,
        input_size: tuple[int, int] = (640, 640),
        score_threshold: float = 0.5,
        nms_threshold: float = 0.4,
        intra_op_threads: int = 2,
    ) -> None:
        import onnxruntime as ort

        if model_path is None:
            model_dir = os.environ.get("MODEL_DIR", "/models")
            model_path = os.path.join(model_dir, "det_10g.onnx")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"SCRFD model not found at {model_path}. Fetch it with:\n"
                "  docker compose --profile vision run --rm vision "
                "python scripts/download_models.py"
            )

        if providers is None:
            requested = os.environ.get("EXEC_PROVIDER", "CPUExecutionProvider")
            available = ort.get_available_providers()
            if requested in available:
                providers = [requested]
                if requested != "CPUExecutionProvider":
                    # Always keep CPU as a fallback so a driver problem
                    # degrades throughput instead of killing the worker.
                    providers.append("CPUExecutionProvider")
            else:
                logger.warning(
                    "execution provider %s unavailable (have: %s); using CPU",
                    requested,
                    ", ".join(available),
                )
                providers = ["CPUExecutionProvider"]

        options = ort.SessionOptions()
        # One worker process per camera already gives parallelism; letting
        # each session grab every core would oversubscribe badly.
        options.intra_op_num_threads = intra_op_threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(
            model_path, sess_options=options, providers=providers
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]
        self.input_size = input_size
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.providers = self.session.get_providers()

        if len(self.output_names) != FEATURE_MAP_COUNT * 3:
            raise RuntimeError(
                f"expected {FEATURE_MAP_COUNT * 3} outputs from SCRFD, got "
                f"{len(self.output_names)}. This build of det_10g.onnx does not "
                "match the decoder in vision/detect/scrfd.py."
            )

        # Anchor centres are a pure function of the input size, so build them
        # once rather than per frame.
        self._anchor_cache: dict[tuple[int, int, int], np.ndarray] = {}

        logger.info(
            "SCRFD loaded from %s (providers=%s, input=%sx%s)",
            model_path,
            ",".join(self.providers),
            input_size[0],
            input_size[1],
        )

    # ------------------------------------------------------------- anchors

    def _anchor_centres(self, height: int, width: int, stride: int) -> np.ndarray:
        key = (height, width, stride)
        cached = self._anchor_cache.get(key)
        if cached is not None:
            return cached

        rows, cols = height // stride, width // stride
        ys, xs = np.mgrid[:rows, :cols]
        centres = np.stack([xs, ys], axis=-1).astype(np.float32) * stride
        centres = centres.reshape(-1, 2)
        if ANCHORS_PER_LOCATION > 1:
            centres = np.repeat(centres, ANCHORS_PER_LOCATION, axis=0)

        self._anchor_cache[key] = centres
        return centres

    # ------------------------------------------------------------- inference

    def _preprocess(self, image: np.ndarray) -> tuple[np.ndarray, float]:
        """Letterbox to the model input size, preserving aspect ratio.

        Returns the blob and the scale factor needed to map detections back
        to original image coordinates. Squashing the aspect ratio instead
        would distort faces and cost accuracy.
        """
        target_w, target_h = self.input_size
        height, width = image.shape[:2]
        scale = min(target_w / width, target_h / height)

        new_w, new_h = int(round(width * scale)), int(round(height * scale))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

        canvas = np.zeros((target_h, target_w, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = resized

        blob = cv2.dnn.blobFromImage(
            canvas,
            scalefactor=1.0 / INPUT_STD,
            size=(target_w, target_h),
            mean=(INPUT_MEAN, INPUT_MEAN, INPUT_MEAN),
            swapRB=True,  # model expects RGB; OpenCV gives BGR
        )
        return blob, scale

    def detect(self, image: np.ndarray, *, max_faces: int = 0) -> list[Face]:
        """Detect faces in a BGR image."""
        if image is None or image.size == 0:
            return []

        blob, scale = self._preprocess(image)
        outputs = self.session.run(self.output_names, {self.input_name: blob})

        boxes_all: list[np.ndarray] = []
        kps_all: list[np.ndarray] = []
        scores_all: list[np.ndarray] = []

        target_w, target_h = self.input_size

        for index, stride in enumerate(STRIDES):
            scores = outputs[index].reshape(-1)
            # Distances are predicted in stride units; scale to pixels.
            box_distances = outputs[index + FEATURE_MAP_COUNT].reshape(-1, 4) * stride
            kps_distances = (
                outputs[index + FEATURE_MAP_COUNT * 2].reshape(-1, 10) * stride
            )

            keep = scores >= self.score_threshold
            if not keep.any():
                continue

            centres = self._anchor_centres(target_h, target_w, stride)
            centres = centres[: scores.shape[0]]

            centres = centres[keep]
            scores_kept = scores[keep]
            box_distances = box_distances[keep]
            kps_distances = kps_distances[keep]

            # SCRFD predicts distances from the anchor centre to each edge
            # (left, top, right, bottom).
            x1 = centres[:, 0] - box_distances[:, 0]
            y1 = centres[:, 1] - box_distances[:, 1]
            x2 = centres[:, 0] + box_distances[:, 2]
            y2 = centres[:, 1] + box_distances[:, 3]
            boxes_all.append(np.stack([x1, y1, x2, y2], axis=-1))

            # Keypoints are offsets from the same centre, interleaved x,y.
            kps = kps_distances.reshape(-1, 5, 2) + centres[:, None, :]
            kps_all.append(kps)

            scores_all.append(scores_kept)

        if not boxes_all:
            return []

        boxes = np.concatenate(boxes_all, axis=0) / scale
        kps = np.concatenate(kps_all, axis=0) / scale
        scores = np.concatenate(scores_all, axis=0)

        # Clip to the real frame: a letterboxed detection can spill past the
        # edge, which would make the crop and the width wrong.
        height, width = image.shape[:2]
        boxes[:, 0] = boxes[:, 0].clip(0, width - 1)
        boxes[:, 1] = boxes[:, 1].clip(0, height - 1)
        boxes[:, 2] = boxes[:, 2].clip(0, width - 1)
        boxes[:, 3] = boxes[:, 3].clip(0, height - 1)

        order = nms(boxes, scores, self.nms_threshold)
        if max_faces > 0:
            order = order[:max_faces]

        faces = [
            Face(
                bbox=boxes[i].astype(np.float32),
                landmarks=kps[i].astype(np.float32),
                score=float(scores[i]),
            )
            for i in order
        ]
        # Largest face first: the person at the door matters more than
        # someone in the background.
        faces.sort(key=lambda f: f.width * f.height, reverse=True)
        return faces


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    """Greedy non-maximum suppression. Returns surviving indices."""
    if boxes.shape[0] == 0:
        return []

    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break

        rest = order[1:]
        xx1 = np.maximum(x1[current], x1[rest])
        yy1 = np.maximum(y1[current], y1[rest])
        xx2 = np.minimum(x2[current], x2[rest])
        yy2 = np.minimum(y2[current], y2[rest])

        intersection = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        union = areas[current] + areas[rest] - intersection
        iou = np.where(union > 0, intersection / union, 0.0)

        order = rest[iou <= threshold]

    return keep
