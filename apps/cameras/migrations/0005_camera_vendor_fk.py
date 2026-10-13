"""Replace the old text vendor column with the link to CameraVendor."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("cameras", "0004_cameravendor"),
    ]

    operations = [
        migrations.RemoveField(model_name="camera", name="vendor"),
        migrations.RenameField(model_name="camera", old_name="vendor_ref", new_name="vendor"),
        migrations.AlterField(
            model_name="camera",
            name="vendor",
            field=models.ForeignKey(
                blank=True,
                help_text="Managed under Cameras > Vendors.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="cameras",
                to="cameras.cameravendor",
            ),
        ),
    ]
