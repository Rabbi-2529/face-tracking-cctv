"""Phase 7 rollout tooling: go-live check, per-floor readiness, local assets, help."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.urls import reverse


@pytest.fixture
def backups(settings, tmp_path):
    settings.BACKUP_DIR = tmp_path
    return tmp_path


def _checks(result):
    return {c.name: c for c in result["checks"]}


def test_golive_fails_on_demo_accounts_and_missing_backups(db, users, backups):
    from apps.accounts.models import User
    from apps.tracking.golive import FAIL, golive_readiness

    User.objects.create_user(email="admin@demo.local", password="x" * 12, full_name="Demo", role="super_admin")
    result = golive_readiness()
    checks = _checks(result)

    assert not result["ready"]
    assert checks["No demo accounts"].status == FAIL
    assert "admin@demo.local" in checks["No demo accounts"].detail
    assert checks["Recent backup"].status == FAIL


def test_golive_accepts_a_fresh_encrypted_backup(db, users, backups):
    from apps.tracking.golive import PASS, WARN, golive_readiness

    (backups / "cctv-20261013-010000.dump.enc").write_bytes(b"Salted__" + b"x" * 64)
    assert _checks(golive_readiness())["Recent backup"].status == PASS

    (backups / "cctv-20261013-020000.dump").write_bytes(b"x" * 64)
    # The newest one is unencrypted: a warning, not a pass.
    assert _checks(golive_readiness())["Recent backup"].status == WARN


def test_golive_checks_production_settings(db, users, backups, settings):
    from apps.tracking.golive import FAIL, PASS, golive_readiness

    settings.MFA_ENFORCED = False
    settings.ENVIRONMENT = "production"
    settings.SECRET_KEY = "replace-me-with-64-random-chars"
    checks = _checks(golive_readiness())
    assert checks["MFA enforced"].status == FAIL
    assert checks["Secret key is real"].status == FAIL
    assert checks["Environment is production"].status == PASS


def test_golive_flags_privileged_users_without_authenticators(db, users, backups):
    from apps.tracking.golive import FAIL, golive_readiness

    check = _checks(golive_readiness())["Privileged users have authenticators"]
    assert check.status == FAIL
    assert "admin@test.local" in check.detail


def test_golive_command_exits_nonzero_when_not_ready(db, users, backups):
    out = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("golive_check", stdout=out)
    assert exc.value.code == 1
    assert "NOT ready to go live" in out.getvalue()


def test_readiness_can_be_limited_to_one_floor(db, org):
    from apps.cameras.models import Camera
    from apps.tracking.pilot import pilot_readiness

    Camera.objects.create(code="cam-ground", name="g", zone=org["reception"], purpose="identity",
                          rtsp_url="rtsp://x/1", measured_face_width_px=110)
    Camera.objects.create(code="cam-first", name="f", zone=org["office"], purpose="identity",
                          rtsp_url="rtsp://x/2", measured_face_width_px=40)

    whole = _checks(pilot_readiness())
    ground = _checks(pilot_readiness(floor=org["ground"]))
    name = "Identity cameras see faces of 60px or more"
    assert "cam-first" in whole[name].detail
    assert "cam-first" not in ground[name].detail
    assert ground[name].status == "pass"


def test_pilot_check_command_accepts_a_floor(db, org):
    out = io.StringIO()
    with pytest.raises(SystemExit):
        call_command("pilot_check", floor="Ground", stdout=out)
    assert "Readiness for HQ / Ground" in out.getvalue()


def test_pages_use_local_libraries_when_configured(client, users, settings):
    client.force_login(users["super_admin"])
    settings.ASSETS_LOCAL = False
    assert "cdnjs.cloudflare.com" in client.get(reverse("tracking:dashboard")).content.decode()

    settings.ASSETS_LOCAL = True
    html = client.get(reverse("tracking:dashboard")).content.decode()
    assert "cdnjs.cloudflare.com" not in html and "cdn.datatables.net" not in html
    assert "vendor/jquery/jquery.min" in html


def test_vendored_files_match_their_manifest():
    import json
    from hashlib import sha256
    from pathlib import Path

    vendor = Path(__file__).resolve().parent.parent / "static" / "vendor"
    manifest = json.loads((vendor / "MANIFEST.json").read_text())
    assert len(manifest) >= 7
    for relative, entry in manifest.items():
        assert sha256((vendor / relative).read_bytes()).hexdigest() == entry["sha256"], relative


@pytest.mark.parametrize(
    ("user_key", "role_label"),
    [("hr", "HR"), ("operator", "Security Operator"), ("auditor", "Auditor"), ("employee", "Employee (self-service)")],
)
def test_help_opens_on_the_users_own_role(client, users, user_key, role_label):
    client.force_login(users[user_key])
    response = client.get(reverse("help"))
    assert response.status_code == 200
    first = response.context["guides"][0]
    assert first["label"] == role_label
    assert len(response.context["guides"]) == 7


def test_help_needs_sign_in(client):
    assert client.get(reverse("help")).status_code == 302
