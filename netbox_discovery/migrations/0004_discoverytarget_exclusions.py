from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_discovery", "0003_discoveryrun_device_results"),
    ]

    operations = [
        migrations.AddField(
            model_name="discoverytarget",
            name="exclusions",
            field=models.TextField(
                blank=True,
                help_text="IPs or CIDR ranges to exclude from scanning, one per line. Example: 10.0.0.5 or 192.168.1.0/28",
                default="",
            ),
            preserve_default=False,
        ),
    ]
