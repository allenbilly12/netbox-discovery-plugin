import logging
from collections import defaultdict

from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
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
# Duplicate Devices views
# ---------------------------------------------------------------------------


def _base_name(device_name: str) -> str:
    """Return the short hostname (before first '.'), lowercased."""
    return device_name.split(".")[0].lower() if device_name else ""


class DuplicateDevicesView(View):
    """
    Lists NetBox devices that share the same base hostname but have different
    full names (e.g. router1.emea.bcd.local vs router1.us.bcd.local).
    """

    template_name = "netbox_discovery/duplicate_devices.html"

    def get(self, request):
        from dcim.models import Device

        groups: dict = defaultdict(list)
        for device in Device.objects.select_related(
            "site", "device_type__manufacturer", "role"
        ).order_by("name"):
            key = _base_name(device.name)
            if key:
                groups[key].append(device)

        duplicates = [
            {"base": base, "devices": devs}
            for base, devs in sorted(groups.items())
            if len(devs) > 1
        ]

        return render(request, self.template_name, {
            "duplicates": duplicates,
            "duplicate_group_count": len(duplicates),
        })


class MergeDevicesView(View):
    """
    POST: keep one device, copy missing data from the duplicate, then delete it.
    """

    def post(self, request):
        from dcim.models import Device
        from django.conf import settings

        keep_id = request.POST.get("keep_id")
        delete_id = request.POST.get("delete_id")

        if not keep_id or not delete_id or keep_id == delete_id:
            messages.error(request, "Invalid merge request — select two different devices.")
            return redirect("plugins:netbox_discovery:duplicate_devices")

        keeper = get_object_or_404(Device, pk=keep_id)
        duplicate = get_object_or_404(Device, pk=delete_id)

        holding_site_name = (
            settings.PLUGINS_CONFIG.get("netbox_discovery", {}).get("holding_site", "Holding")
        )

        changed = False

        # Site: prefer whichever device has a real (non-holding) site.
        # A manually-assigned site is always considered authoritative.
        keeper_on_holding = not keeper.site or keeper.site.name == holding_site_name
        dup_has_real_site = duplicate.site and duplicate.site.name != holding_site_name
        if keeper_on_holding and dup_has_real_site:
            keeper.site = duplicate.site
            changed = True

        # Serial: copy if keeper has none
        if not keeper.serial and duplicate.serial:
            keeper.serial = duplicate.serial
            changed = True

        # os_version custom field
        dup_cf = duplicate.custom_field_data or {}
        keep_cf = dict(keeper.custom_field_data or {})
        if not keep_cf.get("os_version") and dup_cf.get("os_version"):
            keep_cf["os_version"] = dup_cf["os_version"]
            keeper.custom_field_data = keep_cf
            changed = True

        # Virtual Chassis: if duplicate is in a VC and keeper is not, transfer membership.
        dup_vc = duplicate.virtual_chassis
        if dup_vc and not keeper.virtual_chassis:
            keeper.virtual_chassis = dup_vc
            keeper.vc_position = duplicate.vc_position
            keeper.vc_priority = duplicate.vc_priority
            changed = True

        if changed:
            keeper.save()

        # Resolve the VC the duplicate currently belongs to (re-fetch to avoid stale cache).
        # This must happen regardless of whether we transferred the VC above.
        dup_vc_fresh = (
            duplicate.virtual_chassis.__class__.objects.filter(pk=duplicate.virtual_chassis_id).first()
            if duplicate.virtual_chassis_id
            else None
        )
        if dup_vc_fresh:
            # If duplicate is currently the VC master, reassign master before we delete it.
            if dup_vc_fresh.master_id == duplicate.pk:
                # Prefer the keeper (now a VC member) or fall back to any other member.
                new_master = (
                    keeper
                    if keeper.virtual_chassis_id == dup_vc_fresh.pk
                    else Device.objects.filter(virtual_chassis=dup_vc_fresh)
                         .exclude(pk=duplicate.pk)
                         .first()
                )
                dup_vc_fresh.master = new_master
                dup_vc_fresh.save()

            # Detach duplicate from VC before deletion to avoid FK constraint errors.
            duplicate.virtual_chassis = None
            duplicate.vc_position = None
            duplicate.vc_priority = None
            duplicate.save()

        dup_name = duplicate.name
        duplicate.delete()
        messages.success(
            request,
            f"Merged '{dup_name}' into '{keeper.name}' and deleted the duplicate.",
        )
        return redirect("plugins:netbox_discovery:duplicate_devices")


class DeleteDuplicateDeviceView(View):
    """POST: delete a single device identified as a duplicate."""

    def post(self, request, pk):
        from dcim.models import Device

        device = get_object_or_404(Device, pk=pk)
        name = device.name
        device.delete()
        messages.success(request, f"Device '{name}' deleted.")
        return redirect("plugins:netbox_discovery:duplicate_devices")


# ---------------------------------------------------------------------------
# DiscoveryTarget views
# ---------------------------------------------------------------------------


class DiscoveryTargetBulkDeleteView(generic.BulkDeleteView):
    queryset = DiscoveryTarget.objects.all()
    filterset = DiscoveryTargetFilterSet
    table = DiscoveryTargetTable
    default_return_url = "plugins:netbox_discovery:discoverytarget_list"


class DiscoveryTargetListView(generic.ObjectListView):
    queryset = DiscoveryTarget.objects.all()
    table = DiscoveryTargetTable
    filterset = DiscoveryTargetFilterSet
    filterset_form = DiscoveryTargetFilterForm
    template_name = "netbox_discovery/discoverytarget_list.html"
    bulk_delete_url = "plugins:netbox_discovery:discoverytarget_bulk_delete"


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


class DiscoveryRunDeleteView(generic.ObjectDeleteView):
    queryset = DiscoveryRun.objects.all()
    default_return_url = "plugins:netbox_discovery:discoveryrun_list"


class DiscoveryRunBulkDeleteView(generic.BulkDeleteView):
    queryset = DiscoveryRun.objects.select_related("target")
    filterset = DiscoveryRunFilterSet
    table = DiscoveryRunTable
    default_return_url = "plugins:netbox_discovery:discoveryrun_list"


class DiscoveryRunListView(generic.ObjectListView):
    queryset = DiscoveryRun.objects.select_related("target").order_by("-started_at")
    table = DiscoveryRunTable
    filterset = DiscoveryRunFilterSet
    filterset_form = DiscoveryRunFilterForm
    template_name = "netbox_discovery/discoveryrun_list.html"
    bulk_delete_url = "plugins:netbox_discovery:discoveryrun_bulk_delete"


class DiscoveryRunView(generic.ObjectView):
    queryset = DiscoveryRun.objects.select_related("target")
    template_name = "netbox_discovery/discoveryrun.html"

    def get_extra_context(self, request, instance):
        log_lines = instance.log.splitlines() if instance.log else []
        results = instance.device_results or []
        return {
            "log_lines": log_lines,
            "results_created": [r for r in results if r.get("status") == "created"],
            "results_updated": [r for r in results if r.get("status") == "updated"],
            "results_failed": [r for r in results if r.get("status") == "failed"],
        }
