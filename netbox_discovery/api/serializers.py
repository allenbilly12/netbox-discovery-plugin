from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers

from ..models import DiscoveryRun, DiscoveryTarget


class DiscoveryTargetSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_discovery-api:discoverytarget-detail"
    )

    # Never expose passwords in API responses
    credential_password = serializers.SerializerMethodField()
    enable_secret = serializers.SerializerMethodField()

    class Meta:
        model = DiscoveryTarget
        fields = (
            "id",
            "url",
            "display",
            "name",
            "description",
            "targets",
            "credential_username",
            "credential_password",
            "enable_secret",
            "napalm_driver",
            "discovery_protocol",
            "max_depth",
            "ssh_timeout",
            "scan_interval",
            "enabled",
            "last_run",
            "created",
            "last_updated",
            "tags",
            "custom_fields",
        )

    def get_credential_password(self, obj):
        return "********" if obj._credential_password else ""

    def get_enable_secret(self, obj):
        return "********" if obj._enable_secret else ""


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
            "created",
            "last_updated",
        )
        read_only_fields = fields

    def get_target(self, obj):
        return {"id": obj.target_id, "name": obj.target.name, "url": obj.target.get_absolute_url()}
