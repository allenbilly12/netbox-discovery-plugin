# netbox_discovery/sync/netbox_sync.py

## Purpose

All NetBox write logic. Translates collected device data into NetBox ORM operations. **Never deletes anything** — only `get_or_create` and updates.

---

## sync_device(mgmt_ip, data, holding_site_name, log_fn) → (hostname, was_created)

Main entry point. Called once per device from `jobs.py`'s `on_device` callback.

### Steps

1. **Hostname sanitization** — `_is_valid_hostname()` rejects CLI errors (`^`, `% Invalid input`), OS identifiers (`Kernel`, `localhost`), etc. Falls back to `mgmt_ip` as hostname.
2. **Manufacturer / DeviceType** — `get_or_create` with slug generation.
3. **DeviceRole** — `get_or_create` for `"Network Device"`.
4. **Device lookup** — `_get_or_create_device()`: exact hostname → primary IP → domain-variant base hostname → create new.
5. **Update mutable fields** — device_type, serial, os_version custom field.
6. **Hostname-to-site matching** — `_match_site_by_hostname()`: if device is on holding site, try to find a real site whose name is a prefix of the hostname (e.g. `GBLON10SWI01` → site `GBLON10`).
7. **Interfaces** — `_sync_interfaces()`: `get_or_create` each interface, update enabled/description/MTU/MAC.
8. **IP Addresses** — `_sync_ips()`: `get_or_create` each IP, assign to interface. Returns management IP object.
9. **Primary IP** — set `device.primary_ip4`. If conflict detected:
   - If blocker is a **domain-variant** (same base hostname): auto-resolve by clearing blocker's primary IP.
   - Otherwise: log WARNING to conflict file and skip.
10. **VLANs** — `_sync_vlans()`: `get_or_create` each VLAN scoped to holding site.
11. **Virtual Chassis** — `_sync_virtual_chassis()`: if `stack_members > 1`, create/update VC + member devices.

---

## sync_cables(neighbor_records, log_fn) → int

Post-crawl pass. Creates `Cable` objects between matched interfaces. Called from `jobs.py` after all devices are synced.

### Logic

For each `{hostname, neighbors}` record:
1. Find local device via `_find_device_by_hostname()`
2. For each neighbor entry: find local interface (`_find_interface()`), remote device, remote interface
3. Skip if either interface already has `cable_id`
4. Skip if this pair was already seen this run (bidirectional dedup via `frozenset`)
5. Create `Cable(a_terminations=[local_iface], b_terminations=[remote_iface], status="connected")`
6. Each creation is wrapped in `transaction.atomic()` — one failure doesn't abort the rest

---

## Key Helper Functions

### _is_valid_hostname(hostname)
Returns `False` for hostnames starting with `^`, containing `% invalid`/`invalid input`, or matching known non-network identifiers (`kernel`, `localhost`, etc.).

### _match_site_by_hostname(hostname, exclude_site_name)
Strips domain suffix, normalises separators, finds the **longest** site name that is a prefix of the hostname. Requires ≥4 chars. Returns `Site` or `None`.

Example: `GBLON10SWI01` → site `GBLON10`

### _find_device_by_hostname(hostname)
Exact name match → domain-variant match (same `_base_hostname()`).

### _find_interface(device, name)
Exact match first, then expands abbreviations from `_IFACE_EXPANSIONS` list (sorted longest-first to avoid prefix collisions: `"twe"` before `"te"`).

### _get_or_create_device(hostname, mgmt_ip, site, ...)
1. Exact hostname match
2. Primary IP match (follow `IPAddress.assigned_object.device`)
3. Domain-variant match (`name__iexact=base` or `name__istartswith=base + "."`)
4. Create new

### _sync_virtual_chassis(master_device, hostname, stack_members, ...)
- Creates/gets `VirtualChassis` named after hostname
- Sets master device's VC fields (position = active member's position)
- For each non-master member: finds/creates a `Device` named `{base}-sw{position}`
- Member devices always use `master_device.site` (never the holding site)

---

## Conflict Log

IP conflicts that cannot be auto-resolved are written to `/var/log/netbox/discovery_conflicts.log` via `_get_conflict_logger()`. The logger uses `RotatingFileHandler` (5 MB × 5 files).

---

## How to Change

- **Add a new sync step**: Add it inside the `with transaction.atomic():` block in `sync_device()`. Everything is atomic per device.
- **Add a new field to sync**: Add it to the `changed = False` update block after `_get_or_create_device()`.
- **Add a new invalid hostname pattern**: Add to `_INVALID_HOSTNAME_EXACT` (set) or `_INVALID_HOSTNAME_FRAGMENTS` (substring list).
- **Add a new interface abbreviation**: Add to `_IFACE_EXPANSIONS` list — keep sorted longest-prefix-first.
- **Change cable sync behaviour**: Edit `sync_cables()` — the `frozenset` dedup and `cable_id` skip are the two idempotency guards.
