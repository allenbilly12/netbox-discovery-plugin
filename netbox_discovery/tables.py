import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import DiscoveryRun, DiscoveryTarget


class DiscoveryTargetTable(NetBoxTable):
    name = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    napalm_driver = tables.Column(verbose_name="Driver")
    discovery_protocol = tables.Column(verbose_name="Protocol")
    scan_interval = tables.Column(verbose_name="Interval (min)")
    last_run = tables.DateTimeColumn(verbose_name="Last Run")
    run_count = tables.Column(
        verbose_name="Runs",
        orderable=False,
        empty_values=(),
    )
    actions = columns.ActionsColumn(
        actions=("edit", "delete"),
        extra_buttons="""
            <a href="{% url 'plugins:netbox_discovery:discoverytarget_run' pk=record.pk %}"
               class="btn btn-sm btn-success"
               title="Run Now">
                <i class="mdi mdi-play"></i>
            </a>
        """,
    )

    class Meta(NetBoxTable.Meta):
        model = DiscoveryTarget
        fields = (
            "pk",
            "name",
            "description",
            "napalm_driver",
            "discovery_protocol",
            "max_depth",
            "scan_interval",
            "enabled",
            "last_run",
            "run_count",
            "actions",
        )
        default_columns = (
            "name",
            "napalm_driver",
            "discovery_protocol",
            "scan_interval",
            "enabled",
            "last_run",
            "run_count",
            "actions",
        )

    def render_run_count(self, record):
        return record.runs.count()


class DiscoveryRunTable(NetBoxTable):
    target = tables.Column(linkify=True)
    status = tables.Column()
    started_at = tables.DateTimeColumn(verbose_name="Started")
    completed_at = tables.DateTimeColumn(verbose_name="Completed")
    hosts_scanned = tables.Column(verbose_name="Scanned")
    devices_created = tables.Column(verbose_name="Created")
    devices_updated = tables.Column(verbose_name="Updated")
    errors = tables.Column(verbose_name="Errors")
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = DiscoveryRun
        fields = (
            "pk",
            "target",
            "status",
            "started_at",
            "completed_at",
            "hosts_scanned",
            "devices_created",
            "devices_updated",
            "errors",
            "actions",
        )
        default_columns = (
            "target",
            "status",
            "started_at",
            "completed_at",
            "hosts_scanned",
            "devices_created",
            "devices_updated",
            "errors",
        )
