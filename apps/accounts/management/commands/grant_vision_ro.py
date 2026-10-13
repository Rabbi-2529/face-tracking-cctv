"""
Grant the vision workers exactly the read access they need, and nothing else.

Vision workers need two things: the enabled camera list (with decrypted RTSP
URLs, which they decrypt themselves using the shared key) and the active face
gallery. They publish results to Redis and never write to Postgres, so write
access is withheld entirely - a compromised camera-side worker cannot alter
anybody's movement history.

Run after migrations:
    docker compose exec web python manage.py grant_vision_ro --password '...'
then set VISION_DB_USER=vision_ro and VISION_DB_PASSWORD in .env.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

READ_TABLES = [
    "cameras_camera",
    "cameras_pipelineconfig",
    "org_zone",
    "org_floor",
    "org_building",
    "employees_employee",
    "employees_faceenrollment",
    "employees_consentrecord",
]


class Command(BaseCommand):
    help = "Grant the vision_ro role read-only access to the tables vision needs."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password",
            required=True,
            help="Password to set for the vision_ro role.",
        )
        parser.add_argument("--role", default="vision_ro")

    def handle(self, *args, **options) -> None:
        role = options["role"]
        password = options["password"]
        if len(password) < 16:
            raise CommandError("Use at least 16 characters for a service role.")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                [role],
            )
            if cur.fetchone() is None:
                raise CommandError(
                    f"Role '{role}' does not exist. It is created by "
                    "docker/db/init/01-extensions.sql on first database init."
                )

            # Parameterised DDL is not supported, and identifiers cannot be
            # bound, so the role name is validated above against pg_roles and
            # the password is escaped as a literal.
            cur.execute(
                f'ALTER ROLE "{role}" WITH LOGIN PASSWORD %s',
                [password],
            )

            cur.execute(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"')
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')

            missing: list[str] = []
            for table in READ_TABLES:
                cur.execute("SELECT to_regclass(%s)", [f"public.{table}"])
                if cur.fetchone()[0] is None:
                    missing.append(table)
                    continue
                cur.execute(f'GRANT SELECT ON public."{table}" TO "{role}"')
                self.stdout.write(f"  granted SELECT on {table}")

            # No sequence usage, no INSERT/UPDATE/DELETE anywhere.
            cur.execute(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM "{role}"')

        if missing:
            self.stderr.write(
                self.style.WARNING(
                    "tables not found (run migrations first): " + ", ".join(missing)
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{role} configured read-only. Set VISION_DB_USER={role} and "
                "VISION_DB_PASSWORD in .env, then recreate the vision service."
            )
        )
