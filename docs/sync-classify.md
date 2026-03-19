# netbox_discovery/sync/classify.py

## Purpose

Classifies discovered devices into NetBox device roles and auto-assigns tags based on model strings from NAPALM `get_facts()`. Uses vendor model naming conventions (Cisco, Juniper, Arista, Fortinet, Palo Alto, HP/Aruba, F5, Ubiquiti, Check Point).

---

## classify_device(model, vendor, driver) → dict

Main entry point. Returns `{"role": str, "tags": [str], "color": str}`.

### Classification priority

1. **Model-based** — regex patterns matched against NAPALM model string (first match wins)
2. **Driver-based fallback** — if model didn't match, NAPALM driver name infers a role (e.g. `fortios` → Firewall)
3. **Vendor tag injection** — vendor tag is always included (from driver or vendor name)
4. **Final fallback** — `"Network Device"` with grey color

---

## Supported Roles

| Role | Color | Example Models |
|------|-------|----------------|
| Router | green | ISR4431, ASR1002, MX240, C8300 |
| Switch | blue | WS-C3850, C9300, N9K, EX4300, DCS-7050 |
| Firewall | red | ASA5516, FPR4100, SRX340, FortiGate-60F, PA-850 |
| Wireless AP | orange | AIR-AP, C9120, CW9166, FortiAP, UAP |
| Wireless Controller | deep orange | C9800, AIR-CT, vWLC |
| IP Phone | purple | CP-8841, SEP... |
| SAN Switch | brown | MDS-9148 |
| Load Balancer | cyan | BIG-IP, VIPRION |
| Management | blue-grey | Panorama |
| Network Device | grey | (fallback) |

---

## How to Add a New Rule

Add a tuple to the `_RULES` list in the format:
```python
(r"REGEX_PATTERN", "Role Name", ["tag1", "tag2"]),
```

**Pattern ordering matters** — first match wins. Place specific patterns before generic ones (e.g. `C9120` wireless AP before generic `Catalyst` switch).

---

## How to Add a New Vendor Fallback

Add entries to both `DRIVER_ROLE_FALLBACK` (driver → role) and `DRIVER_VENDOR_TAG` (driver → vendor tag slug).
