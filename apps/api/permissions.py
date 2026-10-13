"""
DRF permission classes that delegate to :class:`PermissionService`.

``DenyAll`` is the project-wide default (see ``REST_FRAMEWORK`` in settings),
so a viewset that forgets to declare a permission class is closed rather than
open. Every endpoint opts in explicitly.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.common.audit import record
from apps.common.permissions import PermissionService


class DenyAll(BasePermission):
    """Default-deny. A new endpoint must choose its permission on purpose."""

    message = "This endpoint has no permission policy declared."

    def has_permission(self, request, view) -> bool:
        return False


class HasAction(BasePermission):
    """Gate on ``view.required_action``.

    Usage::

        class CameraViewSet(ModelViewSet):
            permission_classes = [HasAction]
            required_action = Action.CAMERA_VIEW
    """

    def has_permission(self, request, view) -> bool:
        action = getattr(view, "required_action", None)
        if action is None:
            self.message = (
                f"{type(view).__name__} uses HasAction but declares no "
                "required_action."
            )
            return False

        decision = PermissionService.can(request.user, action)
        if not decision:
            self.message = decision.reason
            record(
                f"api.denied:{action}",
                reason=decision.reason,
                success=False,
                metadata={"path": request.path},
            )
            return False
        return True


class HasWriteAction(HasAction):
    """Read with ``required_action``, write with ``required_write_action``."""

    def has_permission(self, request, view) -> bool:
        if not super().has_permission(request, view):
            return False
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True

        write_action = getattr(view, "required_write_action", None)
        if write_action is None:
            self.message = "This endpoint is read-only for your role."
            return False

        decision = PermissionService.can(request.user, write_action)
        if not decision:
            self.message = decision.reason
            record(
                f"api.denied:{write_action}",
                reason=decision.reason,
                success=False,
                metadata={"path": request.path, "method": request.method},
            )
            return False
        return True


class CanViewEmployeeHistory(BasePermission):
    """Object-level check for anything that exposes a person's movements."""

    def has_permission(self, request, view) -> bool:
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj) -> bool:
        employee = getattr(obj, "employee", obj)
        decision = PermissionService.can_view_history(request.user, employee)
        if not decision:
            self.message = decision.reason
            record(
                "api.history_denied",
                target_employee=employee if hasattr(employee, "employee_code") else None,
                reason=decision.reason,
                success=False,
            )
            return False
        return True
