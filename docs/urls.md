# netbox_discovery/urls.py

## Purpose

Maps URL patterns to view classes. All URLs are relative to the plugin base (`/plugins/discovery/`).
The `app_name = "netbox_discovery"` sets the namespace for `reverse()` lookups (`plugins:netbox_discovery:<name>`).

---

## URL Table

| Pattern | Name | View |
|---------|------|------|
| `targets/` | `discoverytarget_list` | `DiscoveryTargetListView` |
| `targets/add/` | `discoverytarget_add` | `DiscoveryTargetEditView` |
| `targets/<pk>/` | `discoverytarget` | `DiscoveryTargetView` |
| `targets/<pk>/edit/` | `discoverytarget_edit` | `DiscoveryTargetEditView` |
| `targets/<pk>/delete/` | `discoverytarget_delete` | `DiscoveryTargetDeleteView` |
| `targets/delete/` | `discoverytarget_bulk_delete` | `DiscoveryTargetBulkDeleteView` |
| `targets/<pk>/run/` | `discoverytarget_run` | `DiscoveryTargetRunView` |
| `runs/` | `discoveryrun_list` | `DiscoveryRunListView` |
| `runs/<pk>/` | `discoveryrun` | `DiscoveryRunView` |
| `runs/<pk>/delete/` | `discoveryrun_delete` | `DiscoveryRunDeleteView` |
| `runs/delete/` | `discoveryrun_bulk_delete` | `DiscoveryRunBulkDeleteView` |
| `duplicate-devices/` | `duplicate_devices` | `DuplicateDevicesView` |
| `duplicate-devices/merge/` | `duplicate_devices_merge` | `MergeDevicesView` |
| `duplicate-devices/<pk>/delete/` | `duplicate_device_delete` | `DeleteDuplicateDeviceView` |

---

## How to Change

- **Add a new URL**: Add a `path(...)` entry here and create the corresponding view in `views.py`.
- **Add a nav link**: After adding the URL, add a `PluginMenuItem` in `navigation.py`.
- **Bulk action URLs**: Follow the pattern `targets/delete/` (no `<pk>` — IDs are passed as POST body).
