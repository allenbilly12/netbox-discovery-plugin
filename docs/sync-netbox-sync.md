# netbox_discovery/sync/netbox_sync.py

## Purpose

All NetBox write logic. Translates collected device data into NetBox ORM operations. Device records are never deleted by discovery; stale interfaces may be removed when a successful interface inventory indicates they no longer exist on the device.

---

## sync_device(mgmt_ip, data, holding_site_name, log_fn) → (hostname, was_created)

Main entry point. Called once per device from `jobs.py`'s `on_device` callback.

### Steps

1. **Hostname sanitization** — `_is_valid_hostname()` rejects CLI errors (`^`, `% Invalid input`), OS identifiers (`Kernel`, `localhost`), etc. Falls back to `mgmt_ip` as hostname.
2. **Manufacturer / DeviceType** — `get_or_create` with slug generation.
3. **DeviceRole (auto-classified)** — `classify_device()` from `sync/classify.py` maps the model string + NAPALM driver to a specific role (Router, Switch, Firewall, Wireless AP, etc.) with a color. Falls back to driver-based inference, then `"Network Device"`.
4. **Device lookup** — `_get_or_create_device()`: exact hostname → primary IP → domain-variant base hostname → create new.
5. **Update mutable fields** — device_type, serial, os_version custom field, role (re-classified on each sync).
6. **Hostname-to-site matching** — `_match_site_by_hostname()`: if device is on holding site, try to find a real site whose name is a prefix of the hostname (e.g. `GBLON10SWI01` → site `GBLON10`).
7. **Auto-tagging** — `_sync_device_tags()` applies classification-derived tags (vendor, device type, series) to the device. Tags are additive — never removed.
8. **Interfaces** — `_sync_interfaces()`: `get_or_create` each interface, update enabled/description/MTU/MAC, and prune stale interfaces after detaching cable/IP/LAG dependencies. Pruning is skipped when collector `get_interfaces()` failed for that device.
   - Interface names are canonicalized to expanded forms (for example `Eth1/3` → `Ethernet1/3`) before sync.
   - Short-name duplicates are treated as stale and removed during successful reconciliation.
9. **IP Addresses** — `_sync_ips()`: `get_or_create` each IP, assign to interface. Returns management IP object.
10. **Primary IP** — set `device.primary_ip4`. If conflict detected:
   - Preserve existing `primary_ip4` if it still exists on the device (do not overwrite with newly discovered candidate IPs).
   - Only change `primary_ip4` when current primary is no longer present on collected interface IP data.
   - If blocker is a **domain-variant** (same base hostname): auto-resolve by clearing blocker's primary IP.
   - Otherwise: log WARNING to conflict file and skip.
11. **VLANs** — `_sync_vlans()`: `get_or_create` each VLAN scoped to holding site.
12. **VRFs** — `_sync_vrfs()`: creates VRFs from `get_network_instances()` when enabled. Placeholder route distinguishers like `0:0` are ignored, duplicate existing VRF names are tolerated by reusing the first match, and conflicting RDs are skipped with warnings instead of aborting the device sync.
13. **Virtual Chassis** — `_sync_virtual_chassis()`: if `stack_members > 1`, create/update VC + member devices.
14. **Journal entries** — discovery writes journal entries for actual object changes (for example create, attribute updates, tags added, interface/IP changes, primary IP changes, stack membership/member updates). Informational no-op cases such as preserving an existing primary IP or skipping a prune are logged to the run output only, not persisted to the device journal.

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
Exact match first, then expands abbreviations from `_IFACE_EXPANSIONS` list (sorted longest-first to avoid prefix collisions: `"twe"` before `"te"`), then falls back to canonicalized abbreviation/full-name key matching.

LAG member sync intentionally skips unresolved member names instead of auto-creating placeholder interfaces, to avoid duplicate interface rows.

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
- **Add a new device classification rule**: Add a tuple to `_RULES` in `sync/classify.py`. Pattern order matters — specific patterns before generic ones. Each rule is `(regex_pattern, role_name, [tag_slugs])`.
- **Add a new vendor for driver-based fallback**: Add to `DRIVER_ROLE_FALLBACK` and `DRIVER_VENDOR_TAG` dicts in `sync/classify.py`.
