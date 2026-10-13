"""
Users, scoping, break-glass elevation and the audit log.

The access design in one paragraph: a user has exactly one :class:`Role`,
which grants a fixed set of actions (apps/common/roles.py). Roles whose
access is inherently partial - HR, Security Operator - additionally carry a
:class:`UserScope` naming the departments and zones they may see. Production
access beyond a role's base set is only reachable through an approved,
time-boxed :class:`AccessElevation`. Every identity-bearing read writes an
:class:`AuditLog` row.
"""

from __future__ import annotations

import datetime as dt

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from apps.common.models import TimeStampedModel, UUIDPrimaryKeyModel
from apps.common.roles import NEVER_ELEVATABLE, Role


class UserManager(BaseUserManager):
    """Email-based manager; this project has no usernames."""

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("An email address is required")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        extra.setdefault("role", Role.EMPLOYEE)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.SUPER_ADMIN)
        if extra.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, UUIDPrimaryKeyModel, TimeStampedModel):
    email = models.EmailField(unique=True, db_index=True)
    full_name = models.CharField(max_length=150)

    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.EMPLOYEE,
        db_index=True,
        help_text="Determines which actions this user may perform. "
        "See apps/common/roles.py for the matrix.",
    )

    # Links a login to the person it belongs to, which is what makes
    # "view my own movement history" possible.
    employee = models.OneToOneField(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="user_account",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False, help_text="Can sign in to the Django admin site."
    )

    must_change_password = models.BooleanField(
        default=False,
        help_text="Forces a password change on next sign-in. Set automatically "
        "for accounts created by an administrator.",
    )
    password_changed_at = models.DateTimeField(null=True, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["email"]
        indexes = [models.Index(fields=["role", "is_active"])]

    def __str__(self) -> str:
        return f"{self.full_name or self.email} ({self.get_role_display()})"

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email

    # ------------------------------------------------------------------ MFA

    @property
    def has_confirmed_mfa(self) -> bool:
        """True when the user has at least one confirmed TOTP device."""
        try:
            from django_otp.plugins.otp_totp.models import TOTPDevice
        except ImportError:  # pragma: no cover
            return False
        return TOTPDevice.objects.filter(user=self, confirmed=True).exists()

    # ---------------------------------------------------------- convenience

    @property
    def is_developer(self) -> bool:
        return self.role == Role.DEVELOPER

    def set_password(self, raw_password):
        super().set_password(raw_password)
        self.password_changed_at = timezone.now()
        self.must_change_password = False


class UserScope(TimeStampedModel):
    """Which departments and zones a scoped role may see.

    Absent or empty means **nothing**, never everything. A role whose access
    is scope-dependent and whose scope is unset sees no rows; failing closed
    is the only safe default for a surveillance system.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="scope")
    departments = models.ManyToManyField(
        "org.Department",
        blank=True,
        related_name="scoped_users",
        help_text="Departments whose employees this user may see.",
    )
    zones = models.ManyToManyField(
        "org.Zone",
        blank=True,
        related_name="scoped_users",
        help_text="Zones whose cameras and live view this user may see.",
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "user scope"
        verbose_name_plural = "user scopes"

    def __str__(self) -> str:
        return f"scope for {self.user.email}"


class AccessElevation(UUIDPrimaryKeyModel, TimeStampedModel):
    """Break-glass: temporary extra actions, approved and time-boxed.

    This is how a Developer reaches real employee identities in production
    when a bug genuinely requires it. The access still happens - the point is
    that it cannot happen quietly, it ends by itself, and authority over
    people (approving a tracking request, managing users, purging data) is
    never obtainable this way.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        ACTIVE = "active", "Active"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="elevations"
    )
    actions = ArrayField(
        models.CharField(max_length=64),
        default=list,
        help_text="Action strings to grant temporarily.",
    )
    reason = models.TextField(help_text="Why this access is needed. Required.")

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="elevations_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # Maximum window an elevation may cover, enforced on approval.
    MAX_DURATION = dt.timedelta(hours=4)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["user", "status", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"elevation for {self.user.email} ({self.status})"

    def clean(self):
        from django.core.exceptions import ValidationError

        blocked = set(self.actions or []) & NEVER_ELEVATABLE
        if blocked:
            raise ValidationError(
                {
                    "actions": "These actions can never be granted by elevation: "
                    + ", ".join(sorted(blocked))
                }
            )
        if self.starts_at and self.expires_at:
            if self.expires_at <= self.starts_at:
                raise ValidationError({"expires_at": "Must be after starts_at."})
            if self.expires_at - self.starts_at > self.MAX_DURATION:
                raise ValidationError(
                    {
                        "expires_at": "An elevation may not exceed "
                        f"{self.MAX_DURATION}."
                    }
                )

    @property
    def is_live(self) -> bool:
        now = timezone.now()
        return (
            self.status == self.Status.ACTIVE
            and self.starts_at is not None
            and self.expires_at is not None
            and self.starts_at <= now < self.expires_at
        )


class AuditLog(models.Model):
    """Append-only record of everything that touches identity.

    There is intentionally no ``updated_at`` and no admin edit path: rows are
    written once. Retention is governed by ``settings.RETENTION["audit_days"]``
    and is far longer than the biometric data it describes.
    """

    at = models.DateTimeField(auto_now_add=True, db_index=True)

    actor = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
        help_text="Null for system actions or deleted accounts.",
    )
    action = models.CharField(max_length=64, db_index=True)

    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.CharField(max_length=64, blank=True)

    # Denormalised so an audit row stays meaningful after the employee
    # record itself has been purged.
    target_employee = models.ForeignKey(
        "employees.Employee",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    target_employee_code = models.CharField(max_length=32, blank=True)

    reason = models.TextField(blank=True)
    success = models.BooleanField(default=True, db_index=True)

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    request_path = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["-at", "actor"]),
            models.Index(fields=["action", "-at"]),
            models.Index(fields=["target_employee", "-at"]),
        ]
        verbose_name = "audit entry"
        verbose_name_plural = "audit log"

    def __str__(self) -> str:
        who = self.actor.email if self.actor else "system"
        return f"{self.at:%Y-%m-%d %H:%M:%S} {who} {self.action}"

    def save(self, *args, **kwargs):
        # Capture the employee code at write time so the row survives purges.
        if self.target_employee_id and not self.target_employee_code:
            self.target_employee_code = getattr(
                self.target_employee, "employee_code", ""
            )
        if self.pk is not None:
            raise ValueError("AuditLog rows are append-only and cannot be modified.")
        super().save(*args, **kwargs)


class LoginEvent(models.Model):
    """Successful and failed sign-ins, kept separately from the audit log.

    django-axes handles lockout; this is the human-readable history shown on
    the account page and used to spot credential sharing.
    """

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    email_attempted = models.EmailField(db_index=True)
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="logins"
    )
    succeeded = models.BooleanField(default=False, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    failure_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["email_attempted", "-at"])]

    def __str__(self) -> str:
        return f"{'OK' if self.succeeded else 'FAIL'} {self.email_attempted} @ {self.at:%Y-%m-%d %H:%M}"
