import django.db.models.deletion
import django.utils.timezone
import taggit.managers
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("extras", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DiscoveryTarget",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.CharField(blank=True, max_length=500)),
                ("targets", models.TextField()),
                ("credential_username", models.CharField(blank=True, max_length=100)),
                (
                    "credential_password",
                    models.CharField(blank=True, db_column="credential_password", max_length=512),
                ),
                (
                    "enable_secret",
                    models.CharField(blank=True, db_column="enable_secret", max_length=512),
                ),
                (
                    "napalm_driver",
                    models.CharField(
                        choices=[
                            ("auto", "Auto-detect"),
                            ("ios", "Cisco IOS / IOS-XE"),
                            ("nxos", "Cisco NX-OS (SSH)"),
                            ("nxos_ssh", "Cisco NX-OS (NX-API)"),
                            ("eos", "Arista EOS"),
                            ("junos", "Juniper JunOS"),
                            ("fortios", "Fortinet FortiOS"),
                        ],
                        default="auto",
                        max_length=20,
                    ),
                ),
                (
                    "discovery_protocol",
                    models.CharField(
                        choices=[("lldp", "LLDP"), ("cdp", "CDP"), ("both", "Both (LLDP + CDP)")],
                        default="both",
                        max_length=10,
                    ),
                ),
                ("max_depth", models.PositiveIntegerField(default=3)),
                ("ssh_timeout", models.PositiveIntegerField(default=10)),
                ("scan_interval", models.PositiveIntegerField(default=0)),
                ("enabled", models.BooleanField(default=True)),
                ("last_run", models.DateTimeField(blank=True, null=True)),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        through="extras.TaggedItem",
                        to="extras.Tag",
                        verbose_name="Tags",
                    ),
                ),
            ],
            options={
                "verbose_name": "Discovery Target",
                "verbose_name_plural": "Discovery Targets",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="DiscoveryRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False),
                ),
                ("created", models.DateTimeField(auto_now_add=True, null=True)),
                ("last_updated", models.DateTimeField(auto_now=True, null=True)),
                (
                    "custom_field_data",
                    models.JSONField(blank=True, default=dict, encoder=None),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                            ("partial", "Partial (some errors)"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("hosts_scanned", models.IntegerField(default=0)),
                ("devices_created", models.IntegerField(default=0)),
                ("devices_updated", models.IntegerField(default=0)),
                ("errors", models.IntegerField(default=0)),
                ("log", models.TextField(blank=True)),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        to="netbox_discovery.discoverytarget",
                    ),
                ),
                (
                    "tags",
                    taggit.managers.TaggableManager(
                        through="extras.TaggedItem",
                        to="extras.Tag",
                        verbose_name="Tags",
                    ),
                ),
            ],
            options={
                "verbose_name": "Discovery Run",
                "verbose_name_plural": "Discovery Runs",
                "ordering": ["-started_at"],
            },
        ),
    ]
