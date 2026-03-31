# netbox_discovery/templates/netbox_discovery/

## Purpose

Django/Jinja2 HTML templates for all plugin UI pages. Extend NetBox base templates to inherit navigation, styles, and layout.

---

## discoverytarget_list.html

List of Discovery Targets. Extends `generic/object_list.html`. No custom content — the table and filters are rendered by the NetBox generic view.

## discoverytarget.html

Target detail page. Extends `generic/object.html`.

**Custom content:**
- Target attributes table (name, description, targets, credentials status, settings)
- Recent runs table (last 10 runs via `recent_runs_table` context variable)
- **Run Now** form — POST to `discoverytarget_run` with `confirm=1` hidden field

## discoverytarget_edit.html

Create/edit form. Extends `generic/object_edit.html`. No custom content — NetBox renders fieldsets from `DiscoveryTargetForm` automatically.

## discoveryrun_list.html

List of Discovery Runs. Extends `generic/object_list.html`. No custom content.

## discoveryrun.html

Run detail page. Extends `generic/object.html`.

**Custom content:**
- **Run Summary** card: target link, status badge, started/completed timestamps
- **Results** card: four counters (Hosts Scanned, Devices Created, Devices Updated, Errors). Created/Updated/Errors counts are clickable if the corresponding `results_*` list is non-empty — click toggles a Bootstrap 5 collapse panel
- **Collapsible detail panels**: tables of IP/hostname/driver for created and updated devices; IP/error for failed devices
- **Job Log** card: `<pre>` with syntax-highlighted log output (dark theme). Auto-scrolls to bottom. If `status == "running"`, auto-refreshes every 5 seconds via `setTimeout(location.reload, 5000)`

## duplicate_devices.html

Duplicate Devices tool. Extends `base/layout.html` (not `generic/object.html` — this is not a model detail page).

**Custom content:**
- Badge showing group count
- Explanation paragraph
- Alert if no duplicates found
- Per-group Bootstrap card with a table of devices
- Per-device action buttons:
  - **Merge →** buttons (one per other device in the group): POST to `duplicate_devices_merge` with `keep_id` / `delete_id`
  - **Delete** button: POST to `duplicate_device_delete` with device PK
- All destructive actions have `onsubmit="return confirm(...)"` guards

---

## How to Change

- **Add a new card to a detail page**: Add a `<div class="card mb-3">...</div>` block inside `{% block content %}`.
- **Change badge colours**: Status-to-colour mapping is inline in the templates (Bootstrap `bg-*` classes).
- **Add a new collapsible panel to discoveryrun.html**: Add a `data-bs-toggle="collapse"` link on the counter and a `<div class="collapse" id="...">` panel below.
- **Change auto-refresh interval**: Edit `setTimeout(function () { location.reload(); }, 5000)` in `discoveryrun.html`.
