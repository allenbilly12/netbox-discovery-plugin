import logging

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from netbox.views import generic

from .filtersets import DiscoveryRunFilterSet, DiscoveryTargetFilterSet
from .forms import (
    DiscoveryRunFilterForm,
    DiscoveryTargetFilterForm,
    DiscoveryTargetForm,
)
from .models import DiscoveryRun, DiscoveryTarget
from .tables import DiscoveryRunTable, DiscoveryTargetTable

logger = logging.getLogger("netbox.plugins.netbox_discovery")


# ---------------------------------------------------------------------------
# DiscoveryTarget views
# ---------------------------------------------------------------------------


class DiscoveryTargetListView(generic.ObjectListView):
    queryset = DiscoveryTarget.objects.all()
    table = DiscoveryTargetTable
    filterset = DiscoveryTargetFilterSet
    filterset_form = DiscoveryTargetFilterForm
    template_name = "netbox_discovery/discoverytarget_list.html"


class DiscoveryTargetView(generic.ObjectView):
    queryset = DiscoveryTarget.objects.prefetch_related("runs")
    template_name = "netbox_discovery/discoverytarget.html"

    def get_extra_context(self, request, instance):
        recent_runs = list(instance.runs.order_by("-started_at")[:10])
        run_table = DiscoveryRunTable(recent_runs)
        return {
            "recent_runs_table": run_table,
        }


class DiscoveryTargetEditView(generic.ObjectEditView):
    queryset = DiscoveryTarget.objects.all()
    form = DiscoveryTargetForm
    template_name = "netbox_discovery/discoverytarget_edit.html"


class DiscoveryTargetDeleteView(generic.ObjectDeleteView):
    queryset = DiscoveryTarget.objects.all()
    default_return_url = "plugins:netbox_discovery:discoverytarget_list"


# ---------------------------------------------------------------------------
# Run Now action view
# ---------------------------------------------------------------------------


class DiscoveryTargetRunView(View):
    """
    POST-only view that enqueues a DiscoveryJob for the given target and
    redirects back to the target detail page.
    """

    def post(self, request, pk):
        target = get_object_or_404(DiscoveryTarget, pk=pk)

        if not target.enabled:
            messages.warning(
                request,
                f"Target '{target.name}' is disabled. Enable it before running.",
            )
            return redirect(target.get_absolute_url())

        if not target.get_effective_username() or not target.get_effective_password():
            messages.error(
                request,
                f"Target '{target.name}' has no credentials configured. "
                "Please set a username/password or configure global defaults in PLUGINS_CONFIG.",
            )
            return redirect(target.get_absolute_url())

        try:
            from .jobs import DiscoveryJob

            DiscoveryJob.enqueue(
                data={"target_id": target.pk},
                name=f"Discovery: {target.name}",
            )
            messages.success(
                request,
                f"Discovery job enqueued for '{target.name}'. "
                "Check Run History for progress.",
            )
        except Exception as exc:
            logger.exception("Failed to enqueue DiscoveryJob for target %s", target.pk)
            messages.error(request, f"Failed to enqueue job: {exc}")

        return redirect(target.get_absolute_url())

    def get(self, request, pk):
        # GET: redirect to the target page with a confirmation prompt via the template
        target = get_object_or_404(DiscoveryTarget, pk=pk)
        return redirect(target.get_absolute_url())


# ---------------------------------------------------------------------------
# DiscoveryRun views
# ---------------------------------------------------------------------------


class DiscoveryRunListView(generic.ObjectListView):
    queryset = DiscoveryRun.objects.select_related("target").order_by("-started_at")
    table = DiscoveryRunTable
    filterset = DiscoveryRunFilterSet
    filterset_form = DiscoveryRunFilterForm
    template_name = "netbox_discovery/discoveryrun_list.html"


class DiscoveryRunView(generic.ObjectView):
    queryset = DiscoveryRun.objects.select_related("target")
    template_name = "netbox_discovery/discoveryrun.html"

    def get_extra_context(self, request, instance):
        log_lines = instance.log.splitlines() if instance.log else []
        return {"log_lines": log_lines}
