from netbox.plugins import PluginConfig


class DiscoveryConfig(PluginConfig):
    name = "netbox_discovery"
    verbose_name = "Network Discovery"
    description = "Discovers network devices via CDP/LLDP and NAPALM, syncing facts into NetBox"
    version = "1.0.0"
    author = "NetBox Discovery Contributors"
    author_email = "noreply@example.com"
    base_url = "discovery"
    min_version = "4.0.0"

    default_config = {
        "holding_site_name": "Holding",
        "ssh_timeout": 10,
        "encryption_key": "",
        "default_username": "",
        "default_password": "",
        "default_enable_secret": "",
    }

    required_config = []

    def ready(self):
        super().ready()
        # Import jobs module so @system_job registers discovery_scheduler with NetBox.
        import netbox_discovery.jobs  # noqa: F401
        # Defer the os_version custom field creation to post_migrate so we
        # don't touch the DB during app initialisation (avoids RuntimeWarning).
        from django.db.models.signals import post_migrate
        post_migrate.connect(_on_post_migrate, sender=self)


def _on_post_migrate(sender, **kwargs):
    """Called after migrations complete — safe to query the DB here."""
    try:
        _ensure_os_version_custom_field()
    except Exception:
        pass


def _ensure_os_version_custom_field():
    from django.contrib.contenttypes.models import ContentType
    from extras.models import CustomField

    # NetBox 4.x uses TYPE_TEXT; fall back gracefully if the choice moves.
    try:
        from extras.choices import CustomFieldTypeChoices
        cf_type = CustomFieldTypeChoices.TYPE_TEXT
    except (ImportError, AttributeError):
        cf_type = "text"

    from dcim.models import Device
    device_ct = ContentType.objects.get_for_model(Device)

    cf, _ = CustomField.objects.get_or_create(
        name="os_version",
        defaults={
            "label": "OS Version",
            "type": cf_type,
            "description": "Device OS version collected by network discovery",
        },
    )
    # Ensure Device is in the field's object_types (ManyToMany)
    if device_ct not in cf.object_types.all():
        cf.object_types.add(device_ct)


config = DiscoveryConfig
