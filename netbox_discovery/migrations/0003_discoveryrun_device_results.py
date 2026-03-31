from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_discovery", "0002_discoverytarget_max_workers"),
    ]

    operations = [
        migrations.AddField(
            model_name="discoveryrun",
            name="device_results",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Per-device results from this run: [{ip, hostname, status, driver, error}]",
            ),
        ),
    ]
