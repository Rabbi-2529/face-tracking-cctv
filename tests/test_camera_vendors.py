"""Managed camera vendors and the camera form's address building."""

from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse


def _vendor(code):
    from apps.cameras.models import CameraVendor

    return CameraVendor.objects.get(code=code)


def test_former_fixed_choices_are_seeded(db):
    from apps.cameras.models import CameraVendor

    codes = set(CameraVendor.objects.values_list("code", flat=True))
    assert {"dahua", "hikvision", "axis", "cpplus", "onvif", "file"} <= codes


@pytest.mark.parametrize(
    ("template", "ok"),
    [
        ("rtsp://{credentials}{host}:{port}/media/video{channel}", True),
        ("rtsp://{credentials}{host}:{port}/x?{nonsense}", False),   # unknown placeholder
        ("rtsp://{credentials}camera:{port}/x", False),               # no {host}
        ("http://{host}/x", False),                                   # not rtsp
    ],
)
def test_vendor_template_is_validated(db, template, ok):
    from apps.cameras.models import CameraVendor

    vendor = CameraVendor(name="T", code="t", stream_template=template)
    if ok:
        vendor.full_clean()
    else:
        with pytest.raises(ValidationError):
            vendor.full_clean()


def _form(org, vendor, **extra):
    from apps.cameras.forms import CameraForm

    data = {
        "name": "Door", "code": "cam-door", "zone": org["reception"].pk,
        "vendor": vendor.pk, "purpose": "identity", "width": 1920, "height": 1080,
        "native_fps": 15, "sample_fps": 4, "is_enabled": "on",
        "connection_mode": "build", "host": "10.0.0.5", "rtsp_port": 554,
        "username": "svc", "password": "p@ss:w/rd", "channel": 2, "stream": "0",
    }
    data.update(extra)
    return CameraForm(data)


def test_address_is_built_from_the_vendor_template(org):
    form = _form(org, _vendor("dahua"))
    assert form.is_valid(), form.errors
    camera = form.save()
    # Credentials percent-encoded, channel and stream filled in.
    assert camera.rtsp_url == (
        "rtsp://svc:p%40ss%3Aw%2Frd@10.0.0.5:554/cam/realmonitor?channel=2&subtype=0"
    )
    assert camera.vendor.code == "dahua"


def test_hikvision_channel_numbering(org):
    form = _form(org, _vendor("hikvision"), stream="1")
    assert form.is_valid(), form.errors
    assert form.save().rtsp_url.endswith("/Streaming/Channels/202")


def test_a_new_vendor_is_usable_immediately(org):
    from apps.cameras.models import CameraVendor

    uniview = CameraVendor.objects.create(
        name="Uniview", code="uniview",
        stream_template="rtsp://{credentials}{host}:{port}/media/video{channel}",
    )
    form = _form(org, uniview)
    assert form.is_valid(), form.errors
    assert form.save().rtsp_url.endswith("/media/video2")


def test_paste_only_vendor_refuses_the_builder(org):
    form = _form(org, _vendor("onvif"))
    assert not form.is_valid()
    assert "connection_mode" in form.errors


def test_file_vendor_needs_an_absolute_path(org):
    file_vendor = _vendor("file")
    assert not _form(org, file_vendor, connection_mode="raw", raw_url="walk.mp4").is_valid()
    form = _form(org, file_vendor, connection_mode="raw", raw_url="/samples/walk.mp4")
    assert form.is_valid(), form.errors


def test_inactive_vendors_are_not_offered_for_new_cameras(org):
    from apps.cameras.forms import CameraForm

    axis = _vendor("axis")
    axis.is_active = False
    axis.save()
    assert axis not in CameraForm().fields["vendor"].queryset


def test_vendor_management_needs_camera_manage(client, users):
    client.force_login(users["operator"])
    assert client.get(reverse("cameras:vendors")).status_code == 403
    client.force_login(users["super_admin"])
    assert client.get(reverse("cameras:vendors")).status_code == 200


def test_admin_adds_a_vendor(client, users):
    from apps.accounts.models import AuditLog
    from apps.cameras.models import CameraVendor

    client.force_login(users["super_admin"])
    response = client.post(
        reverse("cameras:vendor_create"),
        {
            "name": "Uniview", "code": "uniview", "connection": "template",
            "stream_template": "rtsp://{credentials}{host}:{port}/media/video{channel}",
            "default_port": 554, "setup_notes": "", "is_active": "on",
        },
    )
    assert response.status_code == 302
    assert CameraVendor.objects.filter(code="uniview").exists()
    assert AuditLog.objects.filter(action="camera.vendor_created").exists()


def test_a_vendor_in_use_cannot_be_deleted(client, users, org):
    from apps.cameras.models import Camera, CameraVendor

    dahua = _vendor("dahua")
    Camera.objects.create(code="c1", name="c1", zone=org["reception"], vendor=dahua, rtsp_url="rtsp://x/1")
    spare = CameraVendor.objects.create(name="Spare", code="spare", connection="raw")

    client.force_login(users["super_admin"])
    client.post(reverse("cameras:vendor_delete", args=[dahua.pk]))
    client.post(reverse("cameras:vendor_delete", args=[spare.pk]))

    assert CameraVendor.objects.filter(pk=dahua.pk).exists()
    assert not CameraVendor.objects.filter(pk=spare.pk).exists()


def test_add_camera_page_renders_vendor_data(client, users):
    client.force_login(users["super_admin"])
    response = client.get(reverse("cameras:create"))
    assert response.status_code == 200
    assert "vendor-data" in response.content.decode()
    assert any(v["name"] == "Dahua" for v in response.context["vendor_data"].values())
