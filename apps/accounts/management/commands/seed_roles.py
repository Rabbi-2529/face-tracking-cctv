"""
Create the Django Groups that back each role and wire up model permissions so
the Django admin is usable per role in Phase 1.

Authorisation itself lives in apps/common/roles.py - these groups exist so the
*admin site*, which checks Django model permissions, behaves consistently with
the action matrix. Idempotent: safe to run on every deploy.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.common.roles import Role
from apps.common.scoping import SCOPE_REQUIRED_ROLES

# Model-level admin permissions per role, as "app_label.model" -> verbs.
# Deliberately narrower than the action matrix: the admin site is for
# configuration, not for browsing people's movements.
ADMIN_MODEL_PERMISSIONS: dict[str, dict[str, list[str]]] = {
    Role.DEVELOPER: {
        "cameras.camera": ["add", "change", "delete", "view"],
        "cameras.camerahealth": ["view"],
        "cameras.pipelineconfig": ["add", "change", "view"],
        "cameras.cameraauditresult": ["add", "change", "view"],
        "cameras.cameragroup": ["add", "change", "delete", "view"],
        "org.building": ["add", "change", "delete", "view"],
        "org.floor": ["add", "change", "delete", "view"],
        "org.zone": ["add", "change", "delete", "view"],
        "org.zoneadjacency": ["add", "change", "delete", "view"],
        "org.department": ["view"],
        "org.designation": ["view"],
        "employees.employee": ["view"],
        "tracking.detection": ["view"],
        "tracking.presenceinterval": ["view"],
        "tracking.movementevent": ["view", "change"],
        "tracking.unknownfacecluster": ["view", "change"],
        "tracking.dailysummary": ["view"],
    },
    Role.SUPER_ADMIN: {
        "accounts.user": ["add", "change", "delete", "view"],
        "accounts.userscope": ["add", "change", "delete", "view"],
        "accounts.accesselevation": ["change", "view"],
        "accounts.auditlog": ["view"],
        "accounts.loginevent": ["view"],
        "cameras.camera": ["add", "change", "delete", "view"],
        "cameras.camerahealth": ["view"],
        "cameras.cameragroup": ["add", "change", "delete", "view"],
        "cameras.pipelineconfig": ["view"],
        "org.building": ["add", "change", "delete", "view"],
        "org.floor": ["add", "change", "delete", "view"],
        "org.zone": ["add", "change", "delete", "view"],
        "org.zoneadjacency": ["add", "change", "delete", "view"],
        "org.department": ["add", "change", "delete", "view"],
        "org.designation": ["add", "change", "delete", "view"],
        "employees.employee": ["add", "change", "delete", "view"],
        "employees.faceenrollment": ["view", "change", "delete"],
        "employees.consentrecord": ["add", "change", "view"],
        "employees.identitycorrection": ["change", "view"],
        "tracking.presenceinterval": ["view"],
        "tracking.movementevent": ["view", "change"],
        "tracking.unknownfacecluster": ["view", "change"],
        "tracking.dailysummary": ["view"],
        "watchlist.trackingrequest": ["view", "change"],
        "watchlist.tracksession": ["view"],
    },
    Role.SECURITY_ADMIN: {
        "cameras.camera": ["add", "change", "view"],
        "cameras.camerahealth": ["view"],
        "org.zone": ["add", "change", "view"],
        "org.zoneadjacency": ["add", "change", "delete", "view"],
        "employees.employee": ["view"],
        "tracking.presenceinterval": ["view"],
        "tracking.movementevent": ["view", "change"],
        "tracking.unknownfacecluster": ["view", "change"],
        "watchlist.trackingrequest": ["view", "change"],
        "watchlist.tracksession": ["view"],
    },
    Role.HR: {
        "employees.employee": ["add", "change", "view"],
        "employees.faceenrollment": ["add", "view", "delete"],
        "employees.consentrecord": ["add", "change", "view"],
        "employees.identitycorrection": ["add", "view"],
        "org.department": ["view"],
        "org.designation": ["view"],
        "tracking.dailysummary": ["view"],
        "watchlist.trackingrequest": ["add", "view"],
    },
    Role.SECURITY_OPERATOR: {
        "cameras.camera": ["view"],
        "org.zone": ["view"],
    },
    Role.AUDITOR: {
        "accounts.auditlog": ["view"],
        "accounts.loginevent": ["view"],
        "accounts.accesselevation": ["view"],
        "employees.consentrecord": ["view"],
        "watchlist.trackingrequest": ["view"],
        "watchlist.tracksession": ["view"],
    },
    # Employees never reach the admin site; their access is the self-service
    # portal only.
    Role.EMPLOYEE: {},
}


class Command(BaseCommand):
    help = "Create/refresh role groups and their Django admin permissions."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Remove permissions from a group that are no longer in the map.",
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        prune = options["prune"]
        created_count = 0

        for role, model_perms in ADMIN_MODEL_PERMISSIONS.items():
            group, created = Group.objects.get_or_create(name=f"role:{role}")
            created_count += int(created)

            wanted: list[Permission] = []
            for model_path, verbs in model_perms.items():
                app_label, model = model_path.split(".")
                for verb in verbs:
                    codename = f"{verb}_{model}"
                    perm = Permission.objects.filter(
                        codename=codename, content_type__app_label=app_label
                    ).first()
                    if perm is None:
                        self.stderr.write(
                            self.style.WARNING(
                                f"  permission not found: {app_label}.{codename} "
                                "(run migrations first)"
                            )
                        )
                        continue
                    wanted.append(perm)

            if prune:
                group.permissions.set(wanted)
            else:
                group.permissions.add(*wanted)

            self.stdout.write(
                f"  {group.name}: {len(wanted)} permission(s)"
                + (" [pruned]" if prune else "")
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"roles seeded ({created_count} group(s) created, "
                f"{len(ADMIN_MODEL_PERMISSIONS)} total)"
            )
        )

        self._warn_about_missing_scopes()
        self._warn_about_missing_mfa()

    def _warn_about_missing_scopes(self) -> None:
        """Scoped roles with no scope see nothing. Say so plainly."""
        from apps.accounts.models import User

        offenders = User.objects.filter(
            role__in=list(SCOPE_REQUIRED_ROLES), is_active=True, scope__isnull=True
        )
        for user in offenders:
            self.stderr.write(
                self.style.WARNING(
                    f"  {user.email} has role '{user.role}' but no UserScope - "
                    "they will see no employees or zones until one is set."
                )
            )

    def _warn_about_missing_mfa(self) -> None:
        from django.conf import settings

        from apps.accounts.models import User
        from apps.common.roles import MFA_REQUIRED_ROLES

        if not getattr(settings, "MFA_ENFORCED", False):
            return
        for user in User.objects.filter(
            role__in=list(MFA_REQUIRED_ROLES), is_active=True
        ):
            if not user.has_confirmed_mfa:
                self.stderr.write(
                    self.style.WARNING(
                        f"  {user.email} ({user.role}) has no confirmed MFA device - "
                        "gated actions will be refused until enrolled."
                    )
                )
