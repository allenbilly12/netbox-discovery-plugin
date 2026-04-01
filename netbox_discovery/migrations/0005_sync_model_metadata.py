from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_discovery", "0004_discoverytarget_exclusions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discoveryrun",
            name="device_results",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="credential_username",
            field=models.CharField(
                blank=True,
                help_text="SSH username. Leave blank to use global default.",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="enabled",
            field=models.BooleanField(
                default=True,
                help_text="Enable or disable scheduled runs for this target.",
            ),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="max_depth",
            field=models.PositiveIntegerField(
                default=3,
                help_text="Maximum CDP/LLDP neighbor recursion depth.",
            ),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="scan_interval",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Auto-run interval in minutes. Set to 0 to disable scheduled runs.",
            ),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="ssh_timeout",
            field=models.PositiveIntegerField(
                default=10,
                help_text="SSH connection timeout in seconds.",
            ),
        ),
        migrations.AlterField(
            model_name="discoverytarget",
            name="targets",
            field=models.TextField(
                help_text="One IP address or CIDR range per line. Example: 10.0.0.1 or 192.168.1.0/24"
            ),
        ),
    ]
