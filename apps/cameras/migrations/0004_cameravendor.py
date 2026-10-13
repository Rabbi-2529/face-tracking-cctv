"""Camera vendors become a managed table, seeded with the former fixed choices."""

import django.db.models.deletion
from django.db import migrations, models

DAHUA_PATH = "rtsp://{credentials}{host}:{port}/cam/realmonitor?channel={channel}&subtype={stream}"

SEED = [
    {
        "code": "dahua",
        "name": "Dahua",
        "connection": "template",
        "stream_template": DAHUA_PATH,
        "setup_notes": (
            "Create an Operator-level user under Setting > System > Account. Enable "
            "RTSP (port 554) and ONVIF, and turn OFF UPnP and P2P. Use the main "
            "stream for identity cameras."
        ),
    },
    {
        "code": "hikvision",
        "name": "Hikvision",
        "connection": "template",
        # Channel and stream as one number: 101 is channel 1 main stream.
        "stream_template": "rtsp://{credentials}{host}:{port}/Streaming/Channels/{hik_channel}",
        "setup_notes": (
            "Create an Operator user under Configuration > System > User "
            "Management. Channel 1 main stream is 101."
        ),
    },
    {
        "code": "axis",
        "name": "Axis",
        "connection": "template",
        "stream_template": (
            "rtsp://{credentials}{host}:{port}/axis-media/media.amp"
            "?videocodec=h264&resolution={width}x{height}"
        ),
        "setup_notes": "Create a viewer account; RTSP is on by default.",
    },
    {
        "code": "cpplus",
        "name": "CP Plus",
        "connection": "template",
        # CP Plus hardware is Dahua under the badge.
        "stream_template": DAHUA_PATH,
        "setup_notes": "Uses the Dahua stream path. Create a non-admin user and disable P2P.",
    },
    {
        "code": "onvif",
        "name": "Generic ONVIF",
        "connection": "raw",
        "stream_template": "",
        "setup_notes": "Paste the full RTSP URL from the device's own configuration page.",
    },
    {
        "code": "file",
        "name": "Video file (testing)",
        "connection": "file",
        "stream_template": "",
        "setup_notes": (
            "A video file, for testing. Give the path as the vision container sees "
            "it - the project's ./samples directory is mounted at /samples."
        ),
    },
]


def seed_and_link(apps, schema_editor):
    CameraVendor = apps.get_model("cameras", "CameraVendor")
    Camera = apps.get_model("cameras", "Camera")
    by_code = {}
    for row in SEED:
        vendor, _ = CameraVendor.objects.get_or_create(code=row["code"], defaults=row)
        by_code[vendor.code] = vendor
    for camera in Camera.objects.all().only("pk", "vendor"):
        target = by_code.get(camera.vendor) or by_code["dahua"]
        Camera.objects.filter(pk=camera.pk).update(vendor_ref=target)


def unlink(apps, schema_editor):
    Camera = apps.get_model("cameras", "Camera")
    for camera in Camera.objects.select_related("vendor_ref"):
        if camera.vendor_ref_id:
            Camera.objects.filter(pk=camera.pk).update(vendor=camera.vendor_ref.code)


class Migration(migrations.Migration):
    dependencies = [
        ("cameras", "0003_camerahealth_faces_gated_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="CameraVendor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=60, unique=True)),
                ("code", models.SlugField(max_length=30, unique=True)),
                (
                    "connection",
                    models.CharField(
                        choices=[
                            ("template", "Build the URL from a template"),
                            ("raw", "Paste the full stream URL"),
                            ("file", "Video file path (testing)"),
                        ],
                        default="template",
                        max_length=10,
                    ),
                ),
                (
                    "stream_template",
                    models.CharField(
                        blank=True,
                        help_text="e.g. rtsp://{credentials}{host}:{port}/cam/realmonitor?channel={channel}&subtype={stream}",
                        max_length=300,
                    ),
                ),
                ("default_port", models.PositiveIntegerField(default=554)),
                (
                    "setup_notes",
                    models.TextField(blank=True, help_text="Shown on the camera form when this vendor is chosen."),
                ),
                (
                    "is_active",
                    models.BooleanField(default=True, help_text="Inactive vendors are not offered for new cameras."),
                ),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.AddField(
            model_name="camera",
            name="vendor_ref",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="cameras",
                to="cameras.cameravendor",
            ),
        ),
        migrations.RunPython(seed_and_link, unlink),
    ]
