"""Template context: the acting user's role and granted actions.

Lets templates write ``{% if "camera.manage" in granted_actions %}`` instead
of checking role names, so the UI follows the same matrix as the backend.
Hiding a control is cosmetic - the view still enforces it.
"""

from __future__ import annotations

from apps.common.permissions import PermissionService
from apps.common.roles import MFA_REQUIRED_ROLES, Role


def role_context(request) -> dict:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {
            "granted_actions": frozenset(),
            "role_label": "",
            "needs_mfa_enrolment": False,
            "needs_mfa_verification": False,
        }

    has_mfa = user.has_confirmed_mfa
    verified = getattr(user, "is_verified", None)
    return {
        "granted_actions": PermissionService.granted_actions(user),
        "role_label": Role(user.role).label if user.role else "",
        # Drives the "enrol an authenticator" banner for roles that require it.
        "needs_mfa_enrolment": user.role in MFA_REQUIRED_ROLES and not has_mfa,
        # Enrolled but this session has not proved the second factor yet.
        "needs_mfa_verification": has_mfa and not (callable(verified) and verified()),
    }
