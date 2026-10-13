"""
Camera management panel.

A purpose-built screen rather than the Django admin, because camera setup is
an operational task with its own needs: a vendor-aware URL builder, a live
connection test, visible health and gate rates, and honest warnings when a
camera is marked for identity work but cannot support it. The Django admin
can show these models, but it cannot tell an installer that the camera they
just mounted sees 42px faces.

Everything here is gated on CAMERA_VIEW / CAMERA_MANAGE and scoped through
``visible_cameras``, so a Security Operator sees the cameras in their zones
and nothing else.
"""

from __future__ import annotations

import datetime as dt

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View

from apps.cameras.forms import VENDOR_HELP, CameraForm, PipelineConfigForm
from apps.cameras.models import Camera, CameraAuditResult, CameraHealth, PipelineConfig
from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.common.scoping import visible_cameras

# A connection test runs inside the request, so it is kept short.
TEST_TIMEOUT_SECONDS = 12
TEST_MAX_FRAMES = 40


class CameraAccessMixin(LoginRequiredMixin):
    read_action = Action.CAMERA_VIEW
    write_action: str | None = None

    def check_access(self, request) -> None:
        action = self.write_action or self.read_action
        decision = PermissionService.can(request.user, action)
        if not decision:
            record(f"camera.denied:{action}", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)

    def scoped_cameras(self, request):
        return visible_cameras(request.user, Camera.objects.alive())


class CameraListView(CameraAccessMixin, View):
    """The camera estate, with the numbers that actually matter."""

    template_name = "cameras/list.html"

    def get(self, request):
        self.check_access(request)

        cameras = (
            self.scoped_cameras(request)
            .select_related("zone", "zone__floor", "group")
            .order_by("zone__floor__level", "zone__name", "name")
        )

        search = request.GET.get("q", "").strip()
        if search:
            cameras = cameras.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(zone__name__icontains=search)
            )
        status_filter = request.GET.get("status", "")
        if status_filter:
            cameras = cameras.filter(status=status_filter)
        purpose_filter = request.GET.get("purpose", "")
        if purpose_filter:
            cameras = cameras.filter(purpose=purpose_filter)

        cameras = list(cameras)

        # Latest health sample and recent gate rate per camera, in two
        # queries rather than one per camera.
        since = timezone.now() - dt.timedelta(minutes=30)
        latest = {
            row["camera"]: row
            for row in CameraHealth.objects.filter(
                camera__in=cameras, at__gte=since
            )
            .values("camera")
            .annotate(
                last_at=Max("at"),
                fps=Avg("fps_actual"),
                seen=Sum("faces_seen"),
                gated=Sum("faces_gated"),
                identified=Sum("faces_identified"),
            )
        }
        for camera in cameras:
            stats = latest.get(camera.pk)
            camera.recent = stats
            camera.gate_rate = None
            if stats and stats["seen"]:
                camera.gate_rate = stats["gated"] / stats["seen"]

        identity = [c for c in cameras if c.purpose == Camera.Purpose.IDENTITY]
        below_spec = [c for c in identity if not c.is_identity_capable]

        return render(
            request,
            self.template_name,
            {
                "cameras": cameras,
                "can_manage": bool(
                    PermissionService.can(request.user, Action.CAMERA_MANAGE)
                ),
                "counts": {
                    "total": len(cameras),
                    "online": sum(1 for c in cameras if c.status == Camera.Status.ONLINE),
                    "degraded": sum(
                        1 for c in cameras if c.status == Camera.Status.DEGRADED
                    ),
                    "offline": sum(
                        1 for c in cameras if c.status == Camera.Status.OFFLINE
                    ),
                    "disabled": sum(1 for c in cameras if not c.is_enabled),
                    "identity": len(identity),
                    "below_spec": len(below_spec),
                    "unaudited": sum(
                        1 for c in identity if c.measured_face_width_px is None
                    ),
                },
                "statuses": Camera.Status.choices,
                "purposes": Camera.Purpose.choices,
            },
        )


class CameraEditView(CameraAccessMixin, View):
    """Add or change one camera."""

    template_name = "cameras/form.html"
    write_action = Action.CAMERA_MANAGE

    def _context(self, request, form, camera=None) -> dict:
        return {
            "form": form,
            "camera": camera,
            "vendor_help": VENDOR_HELP,
            "warnings": form.get_warnings() if hasattr(form, "get_warnings") else [],
        }

    def get(self, request, code=None):
        self.check_access(request)

        if code:
            camera = get_object_or_404(self.scoped_cameras(request), code=code)
            form = CameraForm(instance=camera, editing=True)
        else:
            camera = None
            form = CameraForm()
        return render(request, self.template_name, self._context(request, form, camera))

    def post(self, request, code=None):
        self.check_access(request)

        if code:
            camera = get_object_or_404(self.scoped_cameras(request), code=code)
            form = CameraForm(request.POST, instance=camera, editing=True)
        else:
            camera = None
            form = CameraForm(request.POST)

        if not form.is_valid():
            return render(
                request, self.template_name, self._context(request, form, camera)
            )

        saved = form.save()

        record(
            "camera.updated" if code else "camera.created",
            object_type="cameras.Camera",
            object_id=saved.pk,
            reason=f"{saved.code} -> zone {saved.zone.code}, purpose {saved.purpose}",
            metadata={
                "code": saved.code,
                "zone": saved.zone.code,
                "purpose": saved.purpose,
                "enabled": saved.is_enabled,
                # Never the URL or the credentials.
            },
        )

        for warning in form.get_warnings():
            messages.warning(request, warning)

        messages.success(
            request,
            f"Camera {saved.code} saved. "
            + (
                "Test the connection next."
                if not code
                else "The vision worker picks up changes within a minute."
            ),
        )
        return redirect(reverse("cameras:detail", args=[saved.code]))


class CameraDetailView(CameraAccessMixin, View):
    """One camera: health history, audit results and recognition throughput."""

    template_name = "cameras/detail.html"

    def get(self, request, code):
        self.check_access(request)
        camera = get_object_or_404(
            self.scoped_cameras(request).select_related("zone", "zone__floor"),
            code=code,
        )

        since = timezone.now() - dt.timedelta(hours=24)
        health = CameraHealth.objects.filter(camera=camera, at__gte=since).order_by(
            "-at"
        )[:60]

        totals = CameraHealth.objects.filter(camera=camera, at__gte=since).aggregate(
            seen=Sum("faces_seen"),
            gated=Sum("faces_gated"),
            identified=Sum("faces_identified"),
            fps=Avg("fps_actual"),
            reconnects=Sum("reconnects"),
            dropped=Sum("frames_dropped"),
        )
        gate_rate = None
        if totals["seen"]:
            gate_rate = totals["gated"] / totals["seen"]

        from apps.tracking.models import Detection

        recent_detections = Detection.objects.filter(
            camera=camera, captured_at__gte=since
        ).aggregate(
            total=Count("id"),
            identified=Count("id", filter=Q(matched_employee__isnull=False)),
            unknown=Count("id", filter=Q(matched_employee__isnull=True)),
        )

        return render(
            request,
            self.template_name,
            {
                "camera": camera,
                "health": health,
                "totals": totals,
                "gate_rate": gate_rate,
                "detections": recent_detections,
                "audits": CameraAuditResult.objects.filter(camera=camera).order_by(
                    "-ran_at"
                )[:5],
                "can_manage": bool(
                    PermissionService.can(request.user, Action.CAMERA_MANAGE)
                ),
            },
        )


class CameraToggleView(CameraAccessMixin, View):
    """Enable or disable ingest for one camera."""

    write_action = Action.CAMERA_MANAGE

    def post(self, request, code):
        self.check_access(request)
        camera = get_object_or_404(self.scoped_cameras(request), code=code)

        if not camera.is_enabled and camera.zone.tracking_prohibited:
            messages.error(
                request,
                f"Zone '{camera.zone.name}' has tracking prohibited; this camera "
                "cannot be enabled.",
            )
            return redirect(reverse("cameras:detail", args=[camera.code]))

        camera.is_enabled = not camera.is_enabled
        if not camera.is_enabled:
            camera.status = Camera.Status.DISABLED
        camera.save(update_fields=["is_enabled", "status", "updated_at"])

        record(
            "camera.enabled" if camera.is_enabled else "camera.disabled",
            object_type="cameras.Camera",
            object_id=camera.pk,
            reason=request.POST.get("reason", ""),
            metadata={"code": camera.code, "zone": camera.zone.code},
        )
        messages.info(
            request,
            f"Camera {camera.code} "
            + ("enabled. A worker starts within a minute." if camera.is_enabled
               else "disabled. Its worker stops within a minute, and its zone "
                    "will show a coverage gap."),
        )
        return redirect(reverse("cameras:detail", args=[camera.code]))


class CameraTestView(CameraAccessMixin, View):
    """Open the stream now and report what it actually delivers.

    The most useful button on the panel: an installer aiming a camera needs
    to know the resolution, the frame rate and above all the face size it is
    producing, while they are still on the ladder. It is a one-camera version
    of ``scripts/camera_audit.py``.

    Runs inside the request with a hard time limit, so it samples briefly
    rather than measuring properly - the audit script is still the thing to
    run for a real verdict.
    """

    write_action = Action.CAMERA_MANAGE

    def post(self, request, code):
        self.check_access(request)
        camera = get_object_or_404(self.scoped_cameras(request), code=code)

        result = self._probe(camera)

        record(
            "camera.tested",
            object_type="cameras.Camera",
            object_id=camera.pk,
            reason=result.get("summary", "")[:500],
            success=result.get("ok", False),
            metadata={"code": camera.code, **{
                k: v for k, v in result.items() if k != "summary"
            }},
        )

        # Record the measured face size so the capability badge stops
        # guessing, but only when faces were actually seen.
        if result.get("median_face_px"):
            camera.measured_face_width_px = int(result["median_face_px"])
            camera.save(update_fields=["measured_face_width_px", "updated_at"])

        return JsonResponse(result)

    def _probe(self, camera: Camera) -> dict:
        import statistics
        import time

        import cv2

        started = time.monotonic()
        capture = cv2.VideoCapture(camera.rtsp_url, cv2.CAP_FFMPEG)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not capture.isOpened():
            capture.release()
            return {
                "ok": False,
                "summary": "Could not open the stream.",
                "advice": (
                    "Check the IP address and port, that the service account "
                    "credentials are correct, that RTSP is enabled on the "
                    "camera, and that this host can reach the camera network."
                ),
            }

        frames = 0
        faces_found = 0
        widths: list[int] = []
        resolution = None
        detector = None

        try:
            from vision.detect.scrfd import SCRFDDetector

            detector = SCRFDDetector()
        except FileNotFoundError:
            detector = None

        while frames < TEST_MAX_FRAMES:
            if time.monotonic() - started > TEST_TIMEOUT_SECONDS:
                break
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            frames += 1
            if resolution is None:
                resolution = f"{frame.shape[1]}x{frame.shape[0]}"
            # Detection is the slow part; sample it.
            if detector is not None and frames % 5 == 0:
                for face in detector.detect(frame, max_faces=3):
                    faces_found += 1
                    widths.append(face.width)

        elapsed = max(1e-6, time.monotonic() - started)
        capture.release()

        if frames == 0:
            return {
                "ok": False,
                "summary": "The stream opened but delivered no frames.",
                "advice": (
                    "Often a codec or transport problem. Set the camera to "
                    "H.264 rather than H.265, and confirm RTSP over TCP."
                ),
            }

        median = statistics.median(widths) if widths else None
        result = {
            "ok": True,
            "frames": frames,
            "fps": round(frames / elapsed, 1),
            "resolution": resolution,
            "faces_found": faces_found,
            "median_face_px": round(median, 1) if median else None,
        }

        if detector is None:
            result["summary"] = (
                f"Stream works: {resolution} at ~{result['fps']} fps. "
                "Face measurement needs the recognition models to be installed."
            )
            result["advice"] = (
                "Run scripts/download_models.py to enable face measurement."
            )
            return result

        if median is None:
            result["summary"] = (
                f"Stream works: {resolution} at ~{result['fps']} fps, but no "
                "face was seen in the sample."
            )
            result["advice"] = (
                "Either nobody walked past during the test, or this camera only "
                "sees backs and tops of heads. Stand in front of it and test "
                "again, then run the full audit."
            )
            return result

        if median >= 100:
            verdict = "good for identity"
            advice = "Face size is comfortable. Run the full audit to confirm."
        elif median >= 60:
            verdict = "marginal for identity"
            advice = (
                "It will work with errors. Lowering the camera or reducing the "
                "downward tilt would help."
            )
        else:
            verdict = "presence only"
            advice = (
                "Faces are far too small to identify anyone. Move the camera to "
                "a doorway at face height, or set its purpose to presence."
            )

        result["summary"] = (
            f"Stream works: {resolution} at ~{result['fps']} fps. "
            f"Median face {median:.0f}px - {verdict}."
        )
        result["advice"] = advice
        result["verdict"] = verdict
        return result


class CameraPreviewView(CameraAccessMixin, View):
    """Serve the latest annotated frame the worker published."""

    def get(self, request, code):
        self.check_access(request)
        camera = get_object_or_404(self.scoped_cameras(request), code=code)

        from apps.cameras.live import preview_frame

        return preview_frame(request, camera)


class CameraWatchView(CameraAccessMixin, View):
    """Register interest, so the worker starts drawing overlays.

    Nothing is annotated or published while nobody is watching, so an idle
    camera costs no extra work.
    """

    def post(self, request, code):
        self.check_access(request)
        camera = get_object_or_404(self.scoped_cameras(request), code=code)

        from apps.cameras.live import request_preview

        return request_preview(request, camera)


class PipelineConfigView(CameraAccessMixin, View):
    """Matching thresholds. Developer-only."""

    template_name = "cameras/pipeline.html"
    read_action = Action.PIPELINE_VIEW
    write_action = None

    def get(self, request):
        self.check_access(request)
        config = PipelineConfig.active()
        can_edit = bool(PermissionService.can(request.user, Action.PIPELINE_CONFIGURE))
        return render(
            request,
            self.template_name,
            {
                "config": config,
                "form": PipelineConfigForm(instance=config) if can_edit else None,
                "can_edit": can_edit,
            },
        )

    def post(self, request):
        decision = PermissionService.can(request.user, Action.PIPELINE_CONFIGURE)
        if not decision:
            record("pipeline.denied", reason=decision.reason, success=False)
            raise PermissionDenied(decision.reason)

        config = PipelineConfig.active()
        form = PipelineConfigForm(request.POST, instance=config)
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"config": config, "form": form, "can_edit": True},
            )

        updated = form.save(commit=False)
        updated.changed_by = request.user
        updated.save()

        record(
            "pipeline.configured",
            object_type="cameras.PipelineConfig",
            object_id=updated.pk,
            reason=updated.change_reason,
            metadata={
                "high": updated.match_threshold_high,
                "low": updated.match_threshold_low,
                "vote_window": updated.vote_window,
                "min_face_px": updated.min_face_px,
            },
        )
        messages.success(
            request,
            "Thresholds saved. Workers pick them up within 30 seconds. "
            "Watch the correction queue for the next few days.",
        )
        return redirect(reverse("cameras:pipeline"))
