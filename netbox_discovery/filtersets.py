import django_filters
from netbox.filtersets import NetBoxModelFilterSet

from .choices import DiscoveryProtocolChoices, NapalmDriverChoices
from .models import DiscoveryRun, DiscoveryTarget


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
    status = django_filters.MultipleChoiceFilter(
        choices=[
            ("pending", "Pending"),
            ("running", "Running"),
            ("completed", "Completed"),
            ("failed", "Failed"),
            ("partial", "Partial"),
        ]
    )

    class Meta:
        model = DiscoveryRun
        fields = ("target_id", "status")

    def search(self, queryset, name, value):
        return queryset.filter(target__name__icontains=value)
