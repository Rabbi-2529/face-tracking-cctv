"""
Create the first Super Admin, idempotently.

Only used for the initial boot. The account is created with
``must_change_password=True``, so the bootstrap password from the environment
cannot remain in use.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.common.roles import Role


class Command(BaseCommand):
    help = "Create or update the bootstrap Super Admin account."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--name", default="System Administrator")

    def handle(self, *args, **options) -> None:
        email = options["email"].strip().lower()
        password = options["password"]

        if len(password) < 12:
            raise CommandError("Bootstrap password must be at least 12 characters.")

        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                "full_name": options["name"],
                "role": Role.SUPER_ADMIN,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )

        if created:
            user.set_password(password)
            # The password came from an environment variable, which is not a
            # secret the operator should keep using.
            user.must_change_password = True
            user.save()
            self.stdout.write(
                self.style.SUCCESS(f"created Super Admin {email} (must change password)")
            )
        else:
            self.stdout.write(f"Super Admin {email} already exists; leaving it alone")

        group = Group.objects.filter(name=f"role:{Role.SUPER_ADMIN}").first()
        if group:
            user.groups.add(group)

        if created:
            self.stdout.write(
                self.style.WARNING(
                    "Next steps: sign in, change the password, enrol an "
                    "authenticator app, then remove BOOTSTRAP_ADMIN_* from .env"
                )
            )
