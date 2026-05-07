import django.db.models.deletion
import taggit.managers
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dcim", "0001_initial"),
        ("extras", "0001_initial"),
        ("ipam", "0001_initial"),
        ("netbox_discovery", "0005_sync_model_metadata"),
    ]

    operations = [
        migrations.CreateModel(
            name="MacAddressTableEntry",
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
                    "interface_name",
                    models.CharField(
                        help_text="Raw interface name as reported by the device (preserved for unresolved entries).",
                        max_length=100,
                    ),
                ),
                ("mac_address", models.CharField(db_index=True, max_length=17)),
                ("vlan_vid", models.PositiveSmallIntegerField(blank=True, db_index=True, null=True)),
                ("is_static", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
                (
                    "device",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discovered_mac_entries",
                        to="dcim.device",
                    ),
                ),
                (
                    "interface",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discovered_mac_entries",
                        to="dcim.interface",
                    ),
                ),
                (
                    "vlan",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="ipam.vlan",
                    ),
                ),
                ("tags", taggit.managers.TaggableManager(through="extras.TaggedItem", to="extras.Tag")),
            ],
            options={
                "verbose_name": "MAC Address Table Entry",
                "verbose_name_plural": "MAC Address Table Entries",
                "ordering": ["device", "vlan_vid", "mac_address"],
            },
        ),
        migrations.AddIndex(
            model_name="macaddresstableentry",
            index=models.Index(fields=["mac_address", "vlan_vid"], name="netbox_disc_mac_add_a3a7e0_idx"),
        ),
        migrations.AddConstraint(
            model_name="macaddresstableentry",
            constraint=models.UniqueConstraint(
                fields=("device", "mac_address", "vlan_vid", "interface_name"),
                name="discovery_mac_unique",
            ),
        ),
    ]
