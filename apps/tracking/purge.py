"""
Dual-authorisation purge: early, irreversible deletion of tracking data.

Flow: a Super Admin requests (scope + kinds + reason) -> a different Super
Admin approves -> the deletion runs at once. Rejection, cancellation and
expiry (7 days undecided) end a request without touching data.

What a purge can never touch: the audit log, sign-in history, tracking
requests, consent records and corrections. Those are the record of how the
data was used, and they must outlive it.

Scope is one person, a date range, or both. "Everyone, all time" is refused -
that is not a purge but a wipe, and belongs in a rebuild, not a button.
"""

from __future__ import annotations

import datetime as dt
import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.common.audit import record
from apps.common.permissions import PermissionService
from apps.common.roles import Action
from apps.tracking.models import PurgeRequest

logger = logging.getLogger("cctv.audit")
Kind = PurgeRequest.Kind


# ------------------------------------------------------------------ scope


def _window(date_from: dt.date | None, date_to: dt.date | None):
    """Inclusive local dates as an aware [start, end) range; None = open."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(dt.datetime.combine(date_from, dt.time.min), tz) if date_from else None
    end = (
        timezone.make_aware(dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min), tz)
        if date_to else None
    )
    return start, end


def _in_window(qs, field: str, start, end, *, date_field: bool = False):
    if date_field:
        if start:
            qs = qs.filter(**{f"{field}__gte": timezone.localtime(start).date()})
        if end:
            qs = qs.filter(**{f"{field}__lt": timezone.localtime(end).date()})
        return qs
    if start:
        qs = qs.filter(**{f"{field}__gte": start})
    if end:
        qs = qs.filter(**{f"{field}__lt": end})
    return qs


def scoped(employee, date_from, date_to) -> dict:
    """Querysets for every purgeable kind within the scope."""
    from apps.alerts.models import Alert
    from apps.employees.models import FaceEnrollment
    from apps.tracking.models import (
        DailySummary,
        Detection,
        MovementEvent,
        PresenceInterval,
    )

    start, end = _window(date_from, date_to)

    def person(qs, field):
        return qs.filter(**{field: employee}) if employee is not None else qs

    return {
        "intervals": _in_window(person(PresenceInterval.objects.all(), "employee"), "entered_at", start, end),
        "movements": _in_window(person(MovementEvent.objects.all(), "employee"), "occurred_at", start, end),
        "summaries": _in_window(person(DailySummary.objects.all(), "employee"), "date", start, end, date_field=True),
        "detections": _in_window(person(Detection.objects.all(), "matched_employee"), "captured_at", start, end),
        "alerts": _in_window(person(Alert.objects.all(), "employee"), "triggered_at", start, end),
        "face_templates": (
            _in_window(FaceEnrollment.objects.filter(employee=employee), "created_at", start, end)
            if employee is not None else FaceEnrollment.objects.none()
        ),
    }


def preview(employee, date_from, date_to, kinds) -> dict:
    """How many records each requested kind would delete."""
    qs = scoped(employee, date_from, date_to)
    counts = {}
    if Kind.MOVEMENT in kinds:
        counts["presence intervals"] = qs["intervals"].count()
        counts["movements"] = qs["movements"].count()
        counts["daily summaries"] = qs["summaries"].count()
    if Kind.DETECTIONS in kinds:
        counts["detections"] = qs["detections"].count()
    if Kind.SNAPSHOTS in kinds or Kind.DETECTIONS in kinds:
        counts["snapshot images"] = qs["detections"].exclude(snapshot_key="").count()
    if Kind.ALERTS in kinds:
        counts["alerts"] = qs["alerts"].count()
    if Kind.FACE_TEMPLATES in kinds:
        counts["face templates"] = qs["face_templates"].count()
    return counts


def validate(employee, date_from, date_to, kinds, reason) -> None:
    errors = {}
    if not kinds:
        errors["kinds"] = "Choose at least one kind of data."
    elif any(k not in Kind.values for k in kinds):
        errors["kinds"] = "Unknown kind of data."
    if employee is None and not (date_from or date_to):
        errors["employee"] = "Choose a person, a date range, or both. Everyone for all time is not a purge."
    if employee is None and Kind.FACE_TEMPLATES in (kinds or []):
        errors["kinds"] = "Face templates can only be purged for one named person."
    if date_from and date_to and date_from > date_to:
        errors["date_to"] = "The end date is before the start date."
    if len((reason or "").strip()) < 20:
        errors["reason"] = "Give the reason in at least 20 characters; the approver and auditors read it."
    if errors:
        raise ValidationError(errors)


# --------------------------------------------------------------- workflow


def _require(user) -> None:
    decision = PermissionService.can(user, Action.PURGE_EXECUTE)
    if not decision:
        raise ValidationError(decision.reason)


def create(requester, *, employee, date_from, date_to, kinds, reason) -> PurgeRequest:
    _require(requester)
    validate(employee, date_from, date_to, kinds, reason)
    request = PurgeRequest.objects.create(
        employee=employee,
        employee_code=employee.employee_code if employee else "",
        date_from=date_from,
        date_to=date_to,
        kinds=list(kinds),
        reason=reason.strip(),
        requested_by=requester,
        preview=preview(employee, date_from, date_to, kinds),
    )
    record(
        "purge.requested",
        actor=requester,
        target_employee=employee,
        object_type="tracking.PurgeRequest",
        object_id=request.pk,
        reason=request.reason[:500],
        metadata={"scope": request.scope_label, "kinds": request.kinds, "preview": request.preview},
    )
    return request


def expire_stale() -> int:
    cutoff = timezone.now() - dt.timedelta(days=PurgeRequest.EXPIRY_DAYS)
    stale = list(PurgeRequest.objects.filter(status=PurgeRequest.Status.PENDING, created_at__lt=cutoff))
    for request in stale:
        request.status = PurgeRequest.Status.EXPIRED
        request.decision_note = f"Expired after {PurgeRequest.EXPIRY_DAYS} days without a second approval."
        request.save(update_fields=["status", "decision_note", "updated_at"])
        record("purge.expired", actor=None, object_type="tracking.PurgeRequest", object_id=request.pk,
               reason=request.scope_label)
    return len(stale)


def _pending(request: PurgeRequest) -> None:
    expire_stale()
    request.refresh_from_db()
    if request.status != PurgeRequest.Status.PENDING:
        raise ValidationError(f"This request is {request.get_status_display().lower()}.")


def reject(request: PurgeRequest, user, note: str) -> PurgeRequest:
    _require(user)
    _pending(request)
    if not (note or "").strip():
        raise ValidationError("Say why it is rejected.")
    request.status = PurgeRequest.Status.REJECTED
    request.decided_by, request.decided_at, request.decision_note = user, timezone.now(), note.strip()
    request.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    record("purge.rejected", actor=user, target_employee=request.employee,
           object_type="tracking.PurgeRequest", object_id=request.pk, reason=note[:500])
    return request


def cancel(request: PurgeRequest, user) -> PurgeRequest:
    _pending(request)
    if user.pk != request.requested_by_id:
        raise ValidationError("Only the requester can cancel a request.")
    request.status = PurgeRequest.Status.CANCELLED
    request.decided_by, request.decided_at = user, timezone.now()
    request.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    record("purge.cancelled", actor=user, object_type="tracking.PurgeRequest", object_id=request.pk,
           reason=request.scope_label)
    return request


def approve_and_execute(request: PurgeRequest, approver, note: str = "") -> PurgeRequest:
    """The second approval. Deletes immediately; there is no undo."""
    _require(approver)
    _pending(request)
    if approver.pk == request.requested_by_id:
        raise ValidationError("A purge needs a second person: you cannot approve your own request.")

    files: list[str] = []
    with transaction.atomic():
        locked = PurgeRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status != PurgeRequest.Status.PENDING:
            raise ValidationError("This request was decided by someone else a moment ago.")
        counts = _delete(locked, files)
        locked.status = PurgeRequest.Status.EXECUTED
        locked.decided_by, locked.decided_at = approver, timezone.now()
        locked.decision_note = (note or "").strip()
        locked.executed_at = timezone.now()
        locked.result = counts
        locked.save()
        record(
            "purge.executed",
            actor=approver,
            target_employee=locked.employee,
            object_type="tracking.PurgeRequest",
            object_id=locked.pk,
            reason=(note or locked.reason)[:500],
            metadata={"scope": locked.scope_label, "kinds": locked.kinds, "deleted": counts,
                      "requested_by": locked.requested_by.email},
        )
        # Image files go only once the row deletions are committed, so a
        # rollback can never leave rows pointing at deleted images.
        transaction.on_commit(lambda: _delete_files(locked.pk, files))
    request.refresh_from_db()
    return request


def _delete(request: PurgeRequest, files: list[str]) -> dict:
    """Delete rows in scope; collect the image files to remove after commit."""
    kinds = set(request.kinds)
    qs = scoped(request.employee, request.date_from, request.date_to)
    counts: dict[str, int] = {}

    def keys(queryset, *fields):
        for row in queryset.values_list(*fields):
            files.extend(k for k in row if k)

    if Kind.DETECTIONS in kinds:
        keys(qs["detections"], "snapshot_key")
        counts["detections"] = qs["detections"].delete()[0]
    elif Kind.SNAPSHOTS in kinds:
        keys(qs["detections"], "snapshot_key")
        counts["snapshot images"] = qs["detections"].exclude(snapshot_key="").update(snapshot_key="")

    if Kind.MOVEMENT in kinds:
        keys(qs["intervals"], "first_snapshot_key", "last_snapshot_key")
        counts["movements"] = qs["movements"].delete()[0]
        counts["daily summaries"] = qs["summaries"].delete()[0]
        counts["presence intervals"] = qs["intervals"].delete()[0]
    elif Kind.SNAPSHOTS in kinds:
        keys(qs["intervals"], "first_snapshot_key", "last_snapshot_key")
        qs["intervals"].update(first_snapshot_key="", last_snapshot_key="")

    if Kind.ALERTS in kinds:
        keys(qs["alerts"], "evidence_snapshot_key")
        counts["alerts"] = qs["alerts"].delete()[0]
    elif Kind.SNAPSHOTS in kinds:
        keys(qs["alerts"], "evidence_snapshot_key")
        qs["alerts"].update(evidence_snapshot_key="")

    if Kind.FACE_TEMPLATES in kinds:
        files.extend(name for name in qs["face_templates"].values_list("image", flat=True) if name)
        counts["face templates"] = qs["face_templates"].delete()[0]

    return counts


def _delete_files(request_pk, names: list[str]) -> None:
    from django.core.files.storage import default_storage

    removed = failed = 0
    for name in set(names):
        try:
            default_storage.delete(name)
            removed += 1
        except Exception:
            failed += 1
            logger.warning("purge %s: could not delete file %s", request_pk, name)
    PurgeRequest.objects.filter(pk=request_pk).update(
        result=_merge(PurgeRequest.objects.get(pk=request_pk).result, {"image files": removed, "file errors": failed})
    )


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    merged.update({k: v for k, v in extra.items() if v or k == "image files"})
    return merged
