"""
ArcFace embedder (w600k_r50.onnx from the InsightFace buffalo_l pack).

Model signature, confirmed with ``scripts/download_models.py --inspect``::

    in   input.1   [None, 3, 112, 112]
    out  683       [N, 512]

The input batch dimension is dynamic, which the pipeline uses: embedding four
faces in one call is markedly cheaper than four calls.

Embeddings are L2-normalised on the way out, so cosine similarity between two
of them is a plain dot product. Everything downstream - the gallery matrix,
the track voter, the match thresholds - assumes unit vectors.

**These vectors are biometric templates.** They are not anonymous, they are
not hashes, and they must never be exposed through an API or logged.
"""

from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger("vision.embed")

EMBEDDING_DIM = 512
INPUT_SIZE = 112

# ArcFace normalisation: (pixel - 127.5) / 127.5, giving roughly [-1, 1].
# Note this differs from SCRFD's /128.0 - using the wrong one degrades
# matching quietly rather than erroring.
INPUT_MEAN = 127.5
INPUT_STD = 127.5


class ArcFaceEmbedder:
    """512-d face embedder. One instance per process."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        providers: list[str] | None = None,
        intra_op_threads: int = 2,
        model_version: str = "buffalo_l",
    ) -> None:
        import onnxruntime as ort

        if model_path is None:
            model_dir = os.environ.get("MODEL_DIR", "/models")
            model_path = os.path.join(model_dir, "w600k_r50.onnx")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"ArcFace model not found at {model_path}. Fetch it with:\n"
                "  docker compose --profile vision run --rm vision "
                "python scripts/download_models.py"
            )

        if providers is None:
            requested = os.environ.get("EXEC_PROVIDER", "CPUExecutionProvider")
            available = ort.get_available_providers()
            if requested in available:
                providers = [requested]
                if requested != "CPUExecutionProvider":
                    providers.append("CPUExecutionProvider")
            else:
                providers = ["CPUExecutionProvider"]

        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(
            model_path, sess_options=options, providers=providers
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        # Recorded on every enrolment: embeddings from different models are
        # not comparable, so a model change means re-embedding the gallery.
        self.model_version = model_version

        logger.info(
            "ArcFace loaded from %s (providers=%s)",
            model_path,
            ",".join(self.session.get_providers()),
        )

    # ------------------------------------------------------------ inference

    def _blob(self, crops: list[np.ndarray]) -> np.ndarray:
        """Stack aligned 112x112 BGR crops into an NCHW RGB batch."""
        batch = np.empty((len(crops), 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
        for index, crop in enumerate(crops):
            if crop.shape[0] != INPUT_SIZE or crop.shape[1] != INPUT_SIZE:
                raise ValueError(
                    f"crop {index} is {crop.shape[1]}x{crop.shape[0]}, expected "
                    f"{INPUT_SIZE}x{INPUT_SIZE}. Align it first with "
                    "vision.align.align_face()."
                )
            # BGR -> RGB, HWC -> CHW, normalise.
            rgb = crop[:, :, ::-1].astype(np.float32)
            batch[index] = ((rgb - INPUT_MEAN) / INPUT_STD).transpose(2, 0, 1)
        return batch

    def embed_batch(self, crops: list[np.ndarray]) -> np.ndarray:
        """Embed several aligned crops. Returns an (N, 512) unit-norm array."""
        if not crops:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)

        outputs = self.session.run(
            [self.output_name], {self.input_name: self._blob(crops)}
        )
        embeddings = np.asarray(outputs[0], dtype=np.float32).reshape(
            len(crops), EMBEDDING_DIM
        )
        return l2_normalise(embeddings)

    def embed(self, crop: np.ndarray) -> np.ndarray:
        """Embed one aligned crop. Returns a (512,) unit-norm vector."""
        return self.embed_batch([crop])[0]


def l2_normalise(vectors: np.ndarray) -> np.ndarray:
    """Scale each row to unit length, leaving zero rows alone."""
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim == 1:
        norm = float(np.linalg.norm(vectors))
        return vectors if norm == 0 else (vectors / norm).astype(np.float32)

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (vectors / norms).astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two embeddings.

    For already-normalised vectors this is just a dot product; the explicit
    normalisation here guards against a caller passing a raw vector.
    """
    a = l2_normalise(np.asarray(a, dtype=np.float32).reshape(-1))
    b = l2_normalise(np.asarray(b, dtype=np.float32).reshape(-1))
    return float(np.dot(a, b))
