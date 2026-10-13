"""
Accuracy: the pilot's main instrument.

Phase 6 is about measuring before trusting. This page puts the evidence the
system already collects in one place, per camera and per zone, and lets a
Developer try thresholds against real scores before changing anything:

* **Camera quality** - how many faces the quality gate throws away, how many
  identifications are only "probable", and the camera's measured face size.
  A camera that gates most faces needs moving, not tuning.
* **Zone quality** - reported mis-identifications and implausible movements,
  the two labelled error signals the review queues produce.
* **Threshold what-if** - the distribution of real match scores, and how
  many would be confirmed / probable / unknown under proposed thresholds,
  next to the highest score of any match a reviewer has confirmed was wrong.

Aggregates only: no names, no images, no embeddings. Gated on PIPELINE_VIEW.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import Floor
from django.utils import timezone
from django.views.generic import TemplateView

from apps.cameras.models import Camera, CameraHealth, PipelineConfig
from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.employees.models import IdentityCorrection
from apps.org.models import Zone
from apps.tracking.models import (
    Detection,
    MatchConfidence,
    MovementEvent,
    PresenceInterval,
)
from apps.tracking.views import ActionRequiredMixin

BUCKETS = 20  # score histogram: 0.00-0.05, 0.05-0.10, ...

# Advice thresholds. Deliberately simple and stated on the page, so nobody
# mistakes them for a model.
GATE_RATE_REPOSITION = 0.5
PROBABLE_SHARE_WARN = 0.4
CORRECTION_RATE_WARN = 0.02


def _float(raw, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, 0.0), 1.0)


def camera_quality(since) -> list[dict]:
    detections = defaultdict(lambda: defaultdict(int))
    for row in (
        Detection.objects.filter(captured_at__gte=since)
        .values("camera_id", "confidence")
        .annotate(n=Count("id"))
    ):
        detections[row["camera_id"]][row["confidence"]] = row["n"]

    health = {
        row["camera_id"]: row
        for row in CameraHealth.objects.filter(at__gte=since)
        .values("camera_id")
        .annotate(seen=Sum("faces_seen"), gated=Sum("faces_gated"))
    }

    rows = []
    for camera in Camera.objects.alive().filter(zone__tracking_prohibited=False).select_related("zone"):
        counts = detections[camera.pk]
        identified = counts[MatchConfidence.CONFIRMED] + counts[MatchConfidence.PROBABLE]
        total = identified + counts[MatchConfidence.UNKNOWN]
        seen = (health.get(camera.pk) or {}).get("seen") or 0
        gated = (health.get(camera.pk) or {}).get("gated") or 0
        gate_rate = gated / seen if seen else None
        probable_share = counts[MatchConfidence.PROBABLE] / identified if identified else None

        advice = []
        if camera.purpose == Camera.Purpose.IDENTITY and not camera.is_identity_capable:
            advice.append("faces under 60px or unmeasured - run the camera audit")
        if gate_rate is not None and gate_rate > GATE_RATE_REPOSITION:
            advice.append("most faces fail the quality gate - reposition, do not tune")
        if probable_share is not None and probable_share > PROBABLE_SHARE_WARN:
            advice.append("many matches only probable - check enrolment poses and lighting")
        rows.append(
            {
                "camera": camera,
                "total": total,
                "confirmed": counts[MatchConfidence.CONFIRMED],
                "probable": counts[MatchConfidence.PROBABLE],
                "unknown": counts[MatchConfidence.UNKNOWN],
                "gate_rate": gate_rate,
                "probable_share": probable_share,
                "advice": advice,
            }
        )
    rows.sort(key=lambda r: (not r["advice"], -r["total"]))
    return rows


def zone_quality(since) -> list[dict]:
    intervals = dict(
        PresenceInterval.objects.filter(entered_at__gte=since)
        .values_list("zone_id")
        .annotate(n=Count("id"))
    )
    movements = {
        row["to_zone_id"]: row
        for row in MovementEvent.objects.filter(occurred_at__gte=since)
        .values("to_zone_id")
        .annotate(total=Count("id"), implausible=Count("id", filter=Q(is_plausible=False)))
    }
    corrections = {
        row["presence_interval__zone_id"]: row
        for row in IdentityCorrection.objects.filter(
            created_at__gte=since, presence_interval__isnull=False
        )
        .values("presence_interval__zone_id")
        .annotate(
            accepted=Count("id", filter=Q(status=IdentityCorrection.Status.ACCEPTED)),
            open=Count("id", filter=Q(status=IdentityCorrection.Status.OPEN)),
        )
    }

    rows = []
    for zone in Zone.objects.alive().filter(tracking_prohibited=False).select_related("floor"):
        n = intervals.get(zone.pk, 0)
        moves = movements.get(zone.pk) or {"total": 0, "implausible": 0}
        corr = corrections.get(zone.pk) or {"accepted": 0, "open": 0}
        correction_rate = corr["accepted"] / n if n else None
        implausible_rate = moves["implausible"] / moves["total"] if moves["total"] else None
        advice = []
        if correction_rate is not None and correction_rate > CORRECTION_RATE_WARN:
            advice.append("confirmed wrong identifications above 2%")
        if implausible_rate and implausible_rate > 0.1:
            advice.append("many impossible arrivals - a missing door in the graph, or bad matches")
        if not (n or moves["total"] or corr["accepted"] or corr["open"]):
            continue
        rows.append(
            {
                "zone": zone,
                "intervals": n,
                "movements": moves["total"],
                "implausible": moves["implausible"],
                "implausible_rate": implausible_rate,
                "corrections_accepted": corr["accepted"],
                "corrections_open": corr["open"],
                "correction_rate": correction_rate,
                "advice": advice,
            }
        )
    rows.sort(key=lambda r: (not r["advice"], -r["intervals"]))
    return rows


def known_error_scores(since) -> list[float]:
    """Highest match score behind each identification a reviewer accepted as wrong.

    These are the labelled false positives. Any of them scoring above the
    "confirmed" threshold is a wrong name the system presented as certain.
    """
    scores = []
    accepted = IdentityCorrection.objects.filter(
        status=IdentityCorrection.Status.ACCEPTED,
        created_at__gte=since,
        presence_interval__isnull=False,
    ).select_related("presence_interval")
    for correction in accepted:
        interval = correction.presence_interval
        best = Detection.objects.filter(
            matched_employee_id=interval.employee_id,
            camera__zone_id=interval.zone_id,
            captured_at__gte=interval.entered_at,
            captured_at__lte=interval.last_seen_at,
            match_score__isnull=False,
        ).aggregate(best=Max("match_score"))["best"]
        if best is not None:
            scores.append(round(float(best), 3))
    return sorted(scores, reverse=True)


def score_histogram(since) -> list[int]:
    counts = [0] * BUCKETS
    rows = (
        Detection.objects.filter(captured_at__gte=since, match_score__isnull=False)
        .annotate(bucket=Floor(F("match_score") * BUCKETS))
        .values("bucket")
        .annotate(n=Count("id"))
    )
    for row in rows:
        index = int(row["bucket"] or 0)
        # A score of exactly 1.0 belongs in the top bucket, not past it.
        counts[min(max(index, 0), BUCKETS - 1)] += row["n"]
    return counts


def what_if(since, high: float, low: float) -> dict:
    scored = Detection.objects.filter(captured_at__gte=since, match_score__isnull=False)
    return scored.aggregate(
        confirmed=Count("id", filter=Q(match_score__gte=high)),
        probable=Count("id", filter=Q(match_score__gte=low, match_score__lt=high)),
        unknown=Count("id", filter=Q(match_score__lt=low)),
    )


class AccuracyView(ActionRequiredMixin, TemplateView):
    template_name = "tracking/accuracy.html"
    required_action = Action.PIPELINE_VIEW

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request
        try:
            days = min(max(int(request.GET.get("days", 7)), 1), 30)
        except ValueError:
            days = 7
        since = timezone.now() - dt.timedelta(days=days)

        config = PipelineConfig.active()
        high = _float(request.GET.get("high"), config.match_threshold_high)
        low = _float(request.GET.get("low"), config.match_threshold_low)
        valid = low < high

        histogram = score_histogram(since)
        peak = max(histogram) or 1
        errors = known_error_scores(since)
        current = what_if(since, config.match_threshold_high, config.match_threshold_low)
        proposed = what_if(since, high, low) if valid else None

        ctx.update(
            {
                "days": days,
                "config": config,
                "high": high,
                "low": low,
                "valid": valid,
                "cameras": camera_quality(since),
                "zones": zone_quality(since),
                "histogram": [
                    {
                        "from": i / BUCKETS,
                        "to": (i + 1) / BUCKETS,
                        "n": n,
                        "pct": round(100 * n / peak),
                        "band": (
                            "confirmed" if i / BUCKETS >= high
                            else "probable" if i / BUCKETS >= low
                            else "unknown"
                        ),
                    }
                    for i, n in enumerate(histogram)
                ],
                "current": current,
                "proposed": proposed,
                "known_errors": errors,
                "errors_above_high": [s for s in errors if s >= high],
                "suggested_high": (
                    min(0.95, round(max(errors) + 0.02, 2)) if errors else None
                ),
                "can_configure": bool(
                    PermissionService.can(request.user, Action.PIPELINE_CONFIGURE)
                ),
            }
        )
        record("accuracy.view", metadata={"days": days, "high": high, "low": low})
        return ctx
