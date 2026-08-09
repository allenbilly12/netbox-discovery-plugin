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
        # last_run is bookkeeping written by the job runner. The scheduler
        # decides whether a target is due by comparing it against
        # scan_interval, so leaving it writable let an API client suppress or
        # force scheduled runs. Note `url`, `credential_password` and
        # `enable_secret` must NOT be listed here — DRF asserts that an
        # explicitly declared field cannot also appear in read_only_fields.
        read_only_fields = ("id", "display", "last_run", "created", "last_updated")
        # Required for ?brief=true and for nested representation of this
        # serializer inside others.
        brief_fields = ("id", "url", "display", "name", "description", "enabled")


class DiscoveryRunSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_discovery-api:discoveryrun-detail"
    )
    target = DiscoveryTargetSerializer(nested=True, read_only=True)

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
        # Only the model-backed fields. `url` and `target` are declared above
        # and DRF asserts that a declared field must not also appear in
        # read_only_fields ("Cannot both declare the field ... and include it
        # in ... read_only_fields") — setting `read_only_fields = fields` made
        # every request to this endpoint raise AssertionError. Both are already
        # read-only anyway: HyperlinkedIdentityField is read-only by
        # definition, and `target` is declared with read_only=True.
        read_only_fields = (
            "id",
            "display",
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
        brief_fields = ("id", "url", "display", "status", "started_at", "completed_at")
