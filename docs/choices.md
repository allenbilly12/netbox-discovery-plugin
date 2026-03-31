# netbox_discovery/choices.py

## Purpose

Central definition of all `TextChoices` enumerations used by models, forms, and the API.

---

## NapalmDriverChoices

| Value | Label |
|-------|-------|
| `auto` | Auto-detect |
| `ios` | Cisco IOS / IOS-XE |
| `nxos` | Cisco NX-OS (SSH) |
| `nxos_ssh` | Cisco NX-OS (NX-API) |
| `eos` | Arista EOS |
| `junos` | Juniper JunOS |
| `fortios` | Fortinet FortiOS |

## DiscoveryProtocolChoices

| Value | Label |
|-------|-------|
| `lldp` | LLDP |
| `cdp` | CDP |
| `both` | Both (LLDP + CDP) |

## DiscoveryRunStatusChoices

| Value | Label |
|-------|-------|
| `pending` | Pending |
| `running` | Running |
| `completed` | Completed |
| `failed` | Failed |
| `partial` | Partial (some errors) |

---

## How to Change

- **Add a new driver**: Add a new entry to `NapalmDriverChoices` and ensure `driver_detect.py` includes it in `DETECTION_ORDER` (or handles it explicitly).
- **Add a new run status**: Add it here and handle it in `jobs.py` (`final_status` assignment) and in the `discoveryrun.html` template (badge colour).
