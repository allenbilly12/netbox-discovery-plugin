# Plan: Additional Data Collection Features

## Context

The plugin currently collects device facts, interfaces, IPs, VLANs, neighbors, LAG membership, and stack members — all focused on topology discovery. Several useful data points are either already collected but not synced, or readily available via NAPALM getters that aren't called yet. This plan adds new features in priority order, filling real gaps in NetBox as a source of truth.

---

## Tier 1 — Quick Wins (no new NAPALM calls, no speed impact)

### 1.1 Map NAPALM Driver → NetBox `Platform`
- **Why**: `Device.platform` is never set. Many NetBox workflows (config contexts, Ansible inventory, scripts) depend on it. Zero-cost — uses `data["driver"]` already passed to `sync_device()`.
- **Collector**: No changes.
- **Sync** (`netbox_sync.py`):
  - Import `Platform` from `dcim.models`
  - After device creation, `Platform.objects.get_or_create(name=driver_name, defaults={"slug": ..., "manufacturer": mfr, "napalm_driver": driver_name})`
  - Set `device.platform = platform` if not already set
- **Config**: Always on (default `True` in `__init__.py`).

### 1.2 Sync Interface Speed
- **Why**: `get_interfaces()` already returns `speed` (Mbps) but it's ignored. NetBox `Interface.speed` stores kbps. Also refines interface type mapping (e.g., SFP GigE vs copper GigE).
- **Collector**: No changes.
- **Sync** (`netbox_sync.py` — `_sync_interfaces()`):
  - Read `iface_data.get("speed", 0)`
  - Set `iface.speed = speed * 1000` (Mbps → kbps) if speed > 0
  - Optionally refine type: if name says `1000base-t` but speed is 10000, override to `10gbase-x-sfpp`
- **Config**: Always on.

### 1.3 Sync FQDN as Custom Field
- **Why**: `get_facts()` returns `fqdn` but it's discarded. Useful for DNS lookups and search.
- **Collector**: No changes (already in `result["facts"]["fqdn"]`).
- **Sync** (`netbox_sync.py`): Set `device.custom_field_data["fqdn"] = fqdn` (same pattern as `os_version`).
- **Setup** (`__init__.py`): Add `_ensure_fqdn_custom_field()` in `_on_post_migrate`.
- **Config**: Always on.

### 1.4 Create Prefixes from Interface IPs
- **Why**: The plugin creates `IPAddress` records but not `Prefix` records. Prefixes are a core IPAM model. Data is already collected (`interfaces_ip` has prefix lengths).
- **Collector**: No changes.
- **Sync** (`netbox_sync.py`): New `_sync_prefixes(interfaces_ip, site, log_fn)` helper.
  - For each IP/prefix pair, compute network address via `netaddr` (e.g., `10.0.1.5/24` → `10.0.1.0/24`)
  - `Prefix.objects.get_or_create(prefix=cidr, defaults={"site": device.site, "status": "active"})`
  - Skip /32, /128, link-local
  - Import `Prefix` from `ipam.models`
- **Config**: Opt-in — `create_prefixes: False` (prefix management is often manually curated).

---

## Tier 2 — High Value (new NAPALM calls, +5-15s per device each)

### 2.1 VRF Discovery via `get_network_instances()`
- **Why**: VRFs are core to multi-tenant networks. NetBox has `ipam.VRF` model but the plugin ignores VRFs entirely.
- **Collector** (`collector.py`):
  - Add `"vrfs": {}` to result, `"vrfs": "pending"` to step_status
  - New step: `get_network_instances()` → returns `{vrf_name: {"name": ..., "type": ..., "state": {"route_distinguisher": ...}}}`
  - Non-fatal (not all drivers support it)
- **Sync** (`netbox_sync.py`): New `_sync_vrfs(vrfs_raw, log_fn)`.
  - `VRF.objects.get_or_create(name=vrf_name, defaults={"rd": route_distinguisher})`
  - Import `VRF` from `ipam.models`
- **STEPS**: +1, **Timeout**: +1 NAPALM call multiplier
- **Driver support**: ios, eos, junos (good). nxos_ssh (partial). fortios (no).
- **Config**: Opt-in — `collect_vrfs: False`.

### 2.2 Inventory Items from `show inventory`
- **Why**: Track PSUs, SFPs, line cards, supervisor modules. The plugin already parses `show inventory` for stacks but only extracts "Switch N" entries. Extending to all entries is straightforward.
- **Collector** (`collector.py`):
  - Add `"inventory_items": []` to result, `"inventory": "pending"` to step_status
  - New step: parse ALL `NAME/PID/SN` entries from `show inventory` (IOS, NX-OS) / `show chassis hardware` (Junos) / `show inventory` (EOS)
  - For IOS stacks: reuse the CLI output already fetched in the stack step
  - Return: `[{"name": "Power Supply 1", "pid": "PWR-C1-350WAC", "serial": "...", "description": "..."}]`
- **Sync** (`netbox_sync.py`): New `_sync_inventory_items(device, items, log_fn)`.
  - `InventoryItem.objects.get_or_create(device=device, name=item_name, defaults={"part_id": pid, "serial": serial, ...})`
  - Import `InventoryItem` from `dcim.models`
- **STEPS**: +1 (but reuses existing CLI on IOS)
- **Config**: Opt-in — `collect_inventory: False`.

### 2.3 Environment Monitoring via `get_environment()`
- **Why**: PSU/fan status, temperature, CPU/memory utilization. Useful for capacity planning.
- **Collector** (`collector.py`):
  - Add `"environment": {}` to result, `"environment": "pending"` to step_status
  - New step: `get_environment()` → `{"fans": {...}, "temperature": {...}, "power": {...}, "cpu": {...}, "memory": {...}}`
- **Sync** (`netbox_sync.py`):
  - Store CPU/memory as custom fields on Device
  - PSU/fan entries could enrich InventoryItem records (if 2.2 is also enabled)
- **Setup** (`__init__.py`): Add environment custom fields in `_on_post_migrate`
- **STEPS**: +1, **Timeout**: +1 NAPALM call
- **Config**: Opt-in — `collect_environment: False`.

---

## Files to Modify

| File | Changes |
|------|---------|
| `netbox_discovery/__init__.py` | New config keys, new custom field setup functions |
| `netbox_discovery/discovery/collector.py` | New optional steps (VRFs, inventory, environment), dynamic STEPS |
| `netbox_discovery/sync/netbox_sync.py` | New sync helpers, Platform mapping, speed sync, prefix creation |
| `netbox_discovery/discovery/neighbor.py` | Update step_status summary string, dynamic collect_timeout |
| `netbox_discovery/jobs.py` | Thread options dict from target/config through to collector |

---

## Dynamic STEPS & Timeout

Replace hardcoded `STEPS = 7` with dynamic calculation based on enabled options:
```python
STEPS = 7  # base: facts, interfaces, LAG, IPs, VLANs, neighbors, stack
if options.get("collect_vrfs"): STEPS += 1
if options.get("collect_inventory"): STEPS += 1
if options.get("collect_environment"): STEPS += 1
```

Similarly in `neighbor.py`, compute `collect_timeout` dynamically:
```python
base_calls = 5
optional_calls = sum(1 for k in ("collect_vrfs", "collect_environment") if options.get(k))
collect_timeout = read_timeout * (base_calls + optional_calls) + 30
```

---

## Config Keys to Add (`__init__.py` → `default_config`)

```python
"sync_platform": True,           # 1.1 — always on
"sync_interface_speed": True,    # 1.2 — always on
"sync_fqdn": True,              # 1.3 — always on
"create_prefixes": False,        # 1.4 — opt-in
"collect_vrfs": False,           # 2.1 — opt-in
"collect_inventory": False,      # 2.2 — opt-in
"collect_environment": False,    # 2.3 — opt-in
```

---

## Implementation Order

1. Config infrastructure — add all new keys to `default_config`
2. Tier 1.1 — Platform mapping (highest value, simplest)
3. Tier 1.2 — Interface speed sync
4. Tier 1.3 — FQDN custom field
5. Tier 1.4 — Prefix creation
6. Tier 2.1 — VRF discovery
7. Tier 2.2 — Inventory items
8. Tier 2.3 — Environment monitoring

---

## Verification

- Run a discovery job against a test device and check:
  - Device has `platform` FK set (1.1)
  - Interfaces have `speed` populated in kbps (1.2)
  - Device has `fqdn` custom field (1.3)
  - Prefixes created in IPAM matching interface subnets (1.4, if enabled)
  - VRFs created in IPAM (2.1, if enabled)
  - InventoryItems visible on device page (2.2, if enabled)
  - Environment custom fields populated (2.3, if enabled)
- Verify disabled features (default off) produce no new data
- Verify non-supporting drivers (e.g., fortios for VRFs) fail gracefully with `step_status: "skip"`
- Check discovery run logs show correct step numbering with dynamic STEPS

---

## Key Patterns to Follow

- **Non-fatal try/except**: Every new NAPALM call wrapped with step_status set to "skip" or "fail"
- **get_or_create + update, never delete**: All new sync helpers must follow this invariant
- **Journal entries**: Material changes logged via `_add_journal_entry()`
- **Thread safety**: New helpers called within `sync_device()` inherit the `transaction.atomic()` block
- **Custom fields**: Auto-created in `__init__.py` via `post_migrate` signal
- **Import locality**: NetBox Django models imported inside functions, not at module level
