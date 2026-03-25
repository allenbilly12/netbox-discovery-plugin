from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import DiscoveryRun, DiscoveryTarget


class DiscoveryTargetSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_discovery-api:discoverytarget-detail"
    )

    # Accept secrets on write without ever returning them in responses.
    credential_password = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
    )
    enable_secret = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
    )

    class Meta:
        model = DiscoveryTarget
        fields = (
            "id",
            "url",
            "display",
            "name",
            "description",
            "targets",
            "exclusions",
            "credential_username",
            "credential_password",
            "enable_secret",
            "napalm_driver",
            "discovery_protocol",
            "max_depth",
            "ssh_timeout",
            "max_workers",
            "scan_interval",
            "enabled",
            "last_run",
            "created",
            "last_updated",
            "tags",
            "custom_fields",
        )


class DiscoveryRunSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_discovery-api:discoveryrun-detail"
    )
    target = serializers.SerializerMethodField()

    class Meta:
        model = DiscoveryRun
        fields = (
            "id",
            "url",
            "display",
            "target",
            "status",
            "started_at",
            "completed_at",
            "hosts_scanned",
            "devices_created",
            "devices_updated",
            "errors",
            "log",
            "device_results",
            "created",
            "last_updated",
        )
        read_only_fields = fields

    def get_target(self, obj):
        return {"id": obj.target_id, "name": obj.target.name, "url": obj.target.get_absolute_url()}
