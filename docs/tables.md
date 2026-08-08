# netbox_discovery/tables.py

## Purpose

`django-tables2` table classes for rendering paginated, sortable lists of `DiscoveryTarget` and `DiscoveryRun` objects.

---

## DiscoveryTargetTable

Renders the Discovery Targets list view.

### Columns

| Column | Notes |
|--------|-------|
| `name` | Linkified (opens detail page) |
| `description` | Plain text |
| `napalm_driver` | Plain text |
| `discovery_protocol` | Plain text |
| `scan_interval` | Plain integer (minutes) |
| `enabled` | Boolean tick/cross via `BooleanColumn` |
| `last_run` | DateTime |
| `run_count` | Computed — calls `record.runs.count()` |
| `actions` | Edit + Delete + custom **Run Now** button (green play icon) |

The custom **Run Now** button in `ActionsColumn.extra_buttons` links to `discoverytarget_run`.

---

## DiscoveryRunTable

Renders the Run History list view.

### Columns

| Column | Notes |
|--------|-------|
| `target` | Linkified to the target detail page |
| `status` | Plain text (badge colouring is in the template) |
| `started_at` / `completed_at` | DateTime |
| `hosts_scanned` / `devices_created` / `devices_updated` / `errors` | Integer counters |
| `actions` | Empty — runs are read-only from the list |

---

## How to Change

- **Add a column**: Add it to `fields` (and optionally `default_columns`) in the `Meta` class, and define the column object above with the appropriate `tables.*Column` type.
- **Add a computed column**: Set `orderable=False, empty_values=()` on the column, then implement `render_<column_name>(self, record)`.
- **Change the Run Now button**: Edit the `extra_buttons` HTML string in `DiscoveryTargetTable.actions`.
