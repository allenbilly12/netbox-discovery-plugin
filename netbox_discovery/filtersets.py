import django_filters
from netbox.filtersets import NetBoxModelFilterSet

from .choices import (
    DiscoveryProtocolChoices,
    DiscoveryRunStatusChoices,
    NapalmDriverChoices,
)
from .models import DiscoveryRun, DiscoveryTarget, MacAddressTableEntry


class DiscoveryTargetFilterSet(NetBoxModelFilterSet):
    napalm_driver = django_filters.MultipleChoiceFilter(
        choices=NapalmDriverChoices.choices,
    )
    discovery_protocol = django_filters.MultipleChoiceFilter(
        choices=DiscoveryProtocolChoices.choices,
    )
    enabled = django_filters.BooleanFilter()

    class Meta:
        model = DiscoveryTarget
        fields = ("name", "napalm_driver", "discovery_protocol", "enabled")

    def search(self, queryset, name, value):
        return queryset.filter(name__icontains=value)


class DiscoveryRunFilterSet(NetBoxModelFilterSet):
    target_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DiscoveryTarget.objects.all(),
        field_name="target",
        label="Target",
    )
    # Sourced from choices.py rather than duplicated, so the two cannot drift.
    status = django_filters.MultipleChoiceFilter(
        choices=DiscoveryRunStatusChoices.choices,
    )

    class Meta:
        model = DiscoveryRun
        fields = ("target_id", "status")

    def search(self, queryset, name, value):
        return queryset.filter(target__name__icontains=value)


class MacAddressTableEntryFilterSet(NetBoxModelFilterSet):
    device_id = django_filters.NumberFilter(field_name="device", label="Device (id)")
    device = django_filters.CharFilter(field_name="device__name", lookup_expr="icontains")
    interface_id = django_filters.NumberFilter(field_name="interface", label="Interface (id)")
    vlan_vid = django_filters.NumberFilter(field_name="vlan_vid")
    mac_address = django_filters.CharFilter(field_name="mac_address", lookup_expr="icontains")
    is_static = django_filters.BooleanFilter()

    class Meta:
        model = MacAddressTableEntry
        fields = ("device_id", "interface_id", "mac_address", "vlan_vid", "is_static")

    def search(self, queryset, name, value):
        # Allow free-text search by MAC fragment, interface name, or device name
        from django.db.models import Q

        return queryset.filter(
            Q(mac_address__icontains=value)
            | Q(interface_name__icontains=value)
            | Q(device__name__icontains=value)
        )
