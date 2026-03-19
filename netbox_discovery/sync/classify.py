"""
Device model classification rules.

Maps device model strings (from NAPALM get_facts()) to NetBox device roles
and auto-assigned tags based on vendor model naming conventions.

Pattern ordering matters — first match wins, so specific patterns (e.g.
Cisco C9120 Wireless AP) must precede generic ones (e.g. Catalyst switch).
"""

import re
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Classification rules: pattern → (role_name, [tag_slugs])
#
# Sources:
#   Cisco:    cisco.com product pages (Catalyst, Nexus, ISR, ASR, ASA, Meraki)
#   Juniper:  juniper.net (MX, EX, QFX, SRX, PTX, ACX)
#   Arista:   arista.com (DCS-7xxx series)
#   Fortinet: fortinet.com (FortiGate, FortiSwitch, FortiAP)
#   Palo Alto, HP/Aruba, F5, Ubiquiti, Check Point
# ---------------------------------------------------------------------------

_RULES = [
    # ── Cisco: Wireless Access Points ────────────────────────────────
    # Must come before generic Catalyst C9xxx patterns
    (r"AIR-[AC]?AP",        "Wireless AP",         ["cisco", "wireless", "access-point"]),
    (r"AIR-CAP",            "Wireless AP",         ["cisco", "wireless", "access-point"]),
    (r"C9105",              "Wireless AP",         ["cisco", "wireless", "wifi6", "access-point"]),
    (r"C9115",              "Wireless AP",         ["cisco", "wireless", "wifi6", "access-point"]),
    (r"C9117",              "Wireless AP",         ["cisco", "wireless", "wifi6", "access-point"]),
    (r"C9120",              "Wireless AP",         ["cisco", "wireless", "wifi6", "access-point"]),
    (r"C9130",              "Wireless AP",         ["cisco", "wireless", "wifi6", "access-point"]),
    (r"C9136",              "Wireless AP",         ["cisco", "wireless", "wifi6e", "access-point"]),
    (r"C9162",              "Wireless AP",         ["cisco", "wireless", "wifi6e", "access-point"]),
    (r"C9164",              "Wireless AP",         ["cisco", "wireless", "wifi6e", "access-point"]),
    (r"C9166",              "Wireless AP",         ["cisco", "wireless", "wifi6e", "access-point"]),
    (r"CW916[0-9]",         "Wireless AP",         ["cisco", "wireless", "wifi6e", "access-point"]),
    (r"CW917[0-9]",         "Wireless AP",         ["cisco", "wireless", "wifi7", "access-point"]),
    (r"CW9178",             "Wireless AP",         ["cisco", "wireless", "wifi7", "access-point"]),

    # ── Cisco: Wireless Controllers ──────────────────────────────────
    (r"AIR-CT",             "Wireless Controller", ["cisco", "wireless", "controller"]),
    (r"C9800",              "Wireless Controller", ["cisco", "wireless", "controller", "catalyst"]),
    (r"WLC",                "Wireless Controller", ["cisco", "wireless", "controller"]),
    (r"vWLC",               "Wireless Controller", ["cisco", "wireless", "controller", "virtual"]),

    # ── Cisco: Meraki ────────────────────────────────────────────────
    (r"MR[0-9]",            "Wireless AP",         ["cisco", "meraki", "wireless", "access-point"]),
    (r"MS[0-9]",            "Switch",              ["cisco", "meraki", "switch"]),
    (r"MX[0-9]",            "Firewall",            ["cisco", "meraki", "firewall"]),

    # ── Cisco: IP Phones / Voice ─────────────────────────────────────
    (r"^CP-",               "IP Phone",            ["cisco", "voice", "ip-phone"]),
    (r"^SEP",               "IP Phone",            ["cisco", "voice", "ip-phone"]),
    (r"IP\s*Phone",         "IP Phone",            ["cisco", "voice", "ip-phone"]),
    (r"ATA\s*19[0-9]",      "IP Phone",            ["cisco", "voice", "analog-adapter"]),

    # ── Cisco: Firewalls ─────────────────────────────────────────────
    (r"ASAv",               "Firewall",            ["cisco", "firewall", "asa", "virtual"]),
    (r"ASA",                "Firewall",            ["cisco", "firewall", "asa"]),
    (r"FTD",                "Firewall",            ["cisco", "firewall", "firepower"]),
    (r"FPR",                "Firewall",            ["cisco", "firewall", "firepower"]),
    (r"Firepower",          "Firewall",            ["cisco", "firewall", "firepower"]),

    # ── Cisco: SAN Switches ──────────────────────────────────────────
    (r"MDS",                "SAN Switch",          ["cisco", "storage", "san", "fibre-channel"]),

    # ── Cisco: Routers ───────────────────────────────────────────────
    (r"ISR[0-9]",           "Router",              ["cisco", "router", "isr"]),
    (r"ISR\s*1[0-9]{3}",    "Router",              ["cisco", "router", "isr"]),
    (r"ISR\s*4[0-9]{3}",    "Router",              ["cisco", "router", "isr"]),
    (r"ISR\s*8[0-9]{3}",    "Router",              ["cisco", "router", "isr"]),
    (r"ASR",                "Router",              ["cisco", "router", "asr"]),
    (r"CSR",                "Router",              ["cisco", "router", "csr", "virtual"]),
    (r"C8[0-9]{3}",         "Router",              ["cisco", "router", "catalyst-8000"]),
    (r"Catalyst\s*8",        "Router",              ["cisco", "router", "catalyst-8000"]),
    (r"NCS",                "Router",              ["cisco", "router", "ncs"]),
    (r"CRS",                "Router",              ["cisco", "router", "crs"]),
    (r"CISCO[0-9]{4}",      "Router",              ["cisco", "router"]),

    # ── Cisco: Switches (Catalyst) ───────────────────────────────────
    (r"WS-C",               "Switch",              ["cisco", "switch", "catalyst"]),
    (r"C9200",              "Switch",              ["cisco", "switch", "catalyst", "c9200"]),
    (r"C9300",              "Switch",              ["cisco", "switch", "catalyst", "c9300"]),
    (r"C9400",              "Switch",              ["cisco", "switch", "catalyst", "c9400"]),
    (r"C9500",              "Switch",              ["cisco", "switch", "catalyst", "c9500"]),
    (r"C9600",              "Switch",              ["cisco", "switch", "catalyst", "c9600"]),
    (r"C2960",              "Switch",              ["cisco", "switch", "catalyst"]),
    (r"C3560",              "Switch",              ["cisco", "switch", "catalyst"]),
    (r"C3650",              "Switch",              ["cisco", "switch", "catalyst"]),
    (r"C3750",              "Switch",              ["cisco", "switch", "catalyst"]),
    (r"C3850",              "Switch",              ["cisco", "switch", "catalyst"]),
    (r"Catalyst",           "Switch",              ["cisco", "switch", "catalyst"]),

    # ── Cisco: Switches (Nexus / Data Center) ────────────────────────
    (r"N[0-9]+K",           "Switch",              ["cisco", "switch", "nexus", "data-center"]),
    (r"Nexus",              "Switch",              ["cisco", "switch", "nexus", "data-center"]),
    (r"NX-",                "Switch",              ["cisco", "switch", "nexus", "data-center"]),

    # ── Cisco: Industrial Switches ───────────────────────────────────
    (r"IE-",                "Switch",              ["cisco", "switch", "industrial"]),
    (r"CGS-",               "Switch",              ["cisco", "switch", "industrial"]),

    # ── Juniper: Firewalls ───────────────────────────────────────────
    (r"vSRX",               "Firewall",            ["juniper", "firewall", "srx", "virtual"]),
    (r"SRX",                "Firewall",            ["juniper", "firewall", "srx"]),

    # ── Juniper: Routers ─────────────────────────────────────────────
    (r"MX[0-9]",            "Router",              ["juniper", "router", "mx"]),
    (r"PTX",                "Router",              ["juniper", "router", "ptx"]),
    (r"ACX",                "Router",              ["juniper", "router", "acx"]),
    (r"NFX",                "Router",              ["juniper", "router", "nfx"]),

    # ── Juniper: Switches ────────────────────────────────────────────
    (r"EX[0-9]",            "Switch",              ["juniper", "switch", "ex"]),
    (r"QFX",                "Switch",              ["juniper", "switch", "qfx", "data-center"]),

    # ── Arista: Switches ─────────────────────────────────────────────
    (r"DCS-7[0-9]{3}",      "Switch",              ["arista", "switch", "data-center"]),
    (r"DCS-",               "Switch",              ["arista", "switch"]),
    (r"vEOS",               "Switch",              ["arista", "switch", "virtual"]),
    (r"cEOS",               "Switch",              ["arista", "switch", "virtual", "container"]),

    # ── Fortinet: Firewalls ──────────────────────────────────────────
    (r"FortiGate",          "Firewall",            ["fortinet", "firewall", "fortigate"]),
    (r"^FGT?-",             "Firewall",            ["fortinet", "firewall", "fortigate"]),
    (r"^FG[0-9]",           "Firewall",            ["fortinet", "firewall", "fortigate"]),
    (r"FortiWeb",           "Firewall",            ["fortinet", "firewall", "fortiweb", "waf"]),

    # ── Fortinet: Switches ───────────────────────────────────────────
    (r"FortiSwitch",        "Switch",              ["fortinet", "switch", "fortiswitch"]),
    (r"^FS-",               "Switch",              ["fortinet", "switch", "fortiswitch"]),

    # ── Fortinet: Wireless ───────────────────────────────────────────
    (r"FortiAP",            "Wireless AP",         ["fortinet", "wireless", "fortiap"]),
    (r"^FAP-",              "Wireless AP",         ["fortinet", "wireless", "fortiap"]),

    # ── Fortinet: WAN / Extender ─────────────────────────────────────
    (r"FortiExtender",      "Router",              ["fortinet", "wan", "fortiextender"]),

    # ── Palo Alto: Firewalls ─────────────────────────────────────────
    (r"^PA-",               "Firewall",            ["paloalto", "firewall"]),
    (r"^VM-[0-9]",          "Firewall",            ["paloalto", "firewall", "virtual"]),
    (r"Panorama",           "Management",          ["paloalto", "management", "panorama"]),

    # ── HP / Aruba: Wireless ─────────────────────────────────────────
    # Wireless rules before switch rules (some Aruba models overlap)
    (r"Aruba.*IAP",         "Wireless AP",         ["hpe", "aruba", "wireless", "instant-ap"]),
    (r"Aruba.*AP-",         "Wireless AP",         ["hpe", "aruba", "wireless", "access-point"]),
    (r"^IAP-",              "Wireless AP",          ["hpe", "aruba", "wireless", "instant-ap"]),
    (r"Aruba\s+7[02][0-9]{2}", "Wireless Controller", ["hpe", "aruba", "wireless", "controller"]),

    # ── HP / Aruba: Switches ─────────────────────────────────────────
    (r"ProCurve",           "Switch",              ["hpe", "aruba", "switch", "procurve"]),
    (r"Aruba.*CX",          "Switch",              ["hpe", "aruba", "switch", "aruba-cx"]),
    (r"Aruba\s+[2-6][0-9]{3}", "Switch",           ["hpe", "aruba", "switch"]),
    (r"Aruba\s+8[34][0-9]{2}", "Switch",           ["hpe", "aruba", "switch", "aruba-cx", "data-center"]),
    (r"J[0-9]{4}[A-Z]",     "Switch",              ["hpe", "aruba", "switch"]),

    # ── Check Point: Firewalls ───────────────────────────────────────
    (r"Check\s*Point",      "Firewall",            ["checkpoint", "firewall"]),

    # ── F5: Load Balancers ───────────────────────────────────────────
    (r"BIG-IP",             "Load Balancer",       ["f5", "load-balancer"]),
    (r"VIPRION",            "Load Balancer",       ["f5", "load-balancer"]),

    # ── Ubiquiti ─────────────────────────────────────────────────────
    (r"USW",                "Switch",              ["ubiquiti", "unifi", "switch"]),
    (r"US-[0-9]",           "Switch",              ["ubiquiti", "unifi", "switch"]),
    (r"UAP",                "Wireless AP",         ["ubiquiti", "unifi", "wireless"]),
    (r"U6",                 "Wireless AP",         ["ubiquiti", "unifi", "wireless", "wifi6"]),
    (r"U7",                 "Wireless AP",         ["ubiquiti", "unifi", "wireless", "wifi7"]),
    (r"USG",                "Firewall",            ["ubiquiti", "unifi", "firewall"]),
    (r"UDM",                "Firewall",            ["ubiquiti", "unifi", "firewall"]),
    (r"UXG",                "Firewall",            ["ubiquiti", "unifi", "firewall"]),
    (r"EdgeRouter",         "Router",              ["ubiquiti", "edgemax", "router"]),
    (r"EdgeSwitch",         "Switch",              ["ubiquiti", "edgemax", "switch"]),
]

# Pre-compile all patterns once at import time
MODEL_RULES = [
    (re.compile(pat, re.IGNORECASE), role, tags)
    for pat, role, tags in _RULES
]

# ---------------------------------------------------------------------------
# NAPALM driver → fallback role (used when model string doesn't match any rule)
# ---------------------------------------------------------------------------
DRIVER_ROLE_FALLBACK = {
    "ios":      "Switch",
    "nxos_ssh": "Switch",
    "nxos":     "Switch",
    "eos":      "Switch",
    "junos":    "Router",
    "fortios":  "Firewall",
    "panos":    "Firewall",
}

# NAPALM driver → vendor tag (always applied)
DRIVER_VENDOR_TAG = {
    "ios":      "cisco",
    "nxos_ssh": "cisco",
    "nxos":     "cisco",
    "eos":      "arista",
    "junos":    "juniper",
    "fortios":  "fortinet",
    "panos":    "paloalto",
}

# Role → color (NetBox hex color code, no leading #)
ROLE_COLORS = {
    "Router":              "4caf50",   # green
    "Switch":              "2196f3",   # blue
    "Firewall":            "f44336",   # red
    "Wireless AP":         "ff9800",   # orange
    "Wireless Controller": "ff5722",   # deep orange
    "IP Phone":            "9c27b0",   # purple
    "SAN Switch":          "795548",   # brown
    "Load Balancer":       "00bcd4",   # cyan
    "Management":          "607d8b",   # blue-grey
    "Network Device":      "9e9e9e",   # grey (fallback)
}


def classify_device(
    model: str,
    vendor: str = "",
    driver: str = "",
) -> Dict[str, object]:
    """
    Classify a device by its model string into a role and set of tags.

    Args:
        model:  Device model string from NAPALM get_facts().
        vendor: Vendor/manufacturer name (used for vendor tag fallback).
        driver: NAPALM driver name (used for role fallback when model is unknown).

    Returns:
        {"role": str, "tags": list[str], "color": str}
    """
    role = None
    tags: List[str] = []

    # 1. Try model-based classification (first match wins)
    if model and model.lower() != "unknown":
        for pattern, rule_role, rule_tags in MODEL_RULES:
            if pattern.search(model):
                role = rule_role
                tags = list(rule_tags)
                break

    # 2. Fallback: use NAPALM driver to infer role
    if role is None and driver:
        role = DRIVER_ROLE_FALLBACK.get(driver)

    # 3. Ensure vendor tag is present
    vendor_tag = DRIVER_VENDOR_TAG.get(driver)
    if vendor_tag and vendor_tag not in tags:
        tags.insert(0, vendor_tag)
    elif vendor and not any(t for t in tags if t == vendor.lower()):
        v = re.sub(r"[^\w-]", "", vendor.lower()).strip("-")
        if v:
            tags.insert(0, v)

    # 4. Final fallback
    if role is None:
        role = "Network Device"

    color = ROLE_COLORS.get(role, ROLE_COLORS["Network Device"])

    return {"role": role, "tags": tags, "color": color}
