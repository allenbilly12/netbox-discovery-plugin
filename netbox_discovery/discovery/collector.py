"""
NAPALM data collection from a connected device.

Collects:
- Facts (hostname, vendor, model, serial, os_version)
- Stack members (Cisco IOS only): position, role, serial, model via show switch + show inventory
- Interfaces (name, enabled, description, mtu, mac)
- Interface IPs (including VLAN SVIs)
- VLANs (if driver supports it)
- LLDP/CDP neighbors (for recursive discovery)
"""

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("netbox.plugins.netbox_discovery")


def collect_device_data(
    device,
    driver_name: str,
    discovery_protocol: str = "both",
    log_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Collect all available data from an open NAPALM device.

    Args:
        device: Open NAPALM driver instance.
        driver_name: The NAPALM driver name (for logging).
        discovery_protocol: 'lldp', 'cdp', or 'both'.
        log_fn: Optional progress callback (same as job log_fn).

    Returns:
        Dict with keys: facts, interfaces, interfaces_ip, vlans, neighbors, raw_errors.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)

    result = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "vlans": {},
        "neighbors": [],
        "stack_members": [],
        "raw_errors": [],
    }

    # --- Facts ---
    log_fn("    [1/5] get_facts()...")
    t0 = time.monotonic()
    try:
        result["facts"] = device.get_facts()
        log_fn(f"    [1/5] get_facts() done ({time.monotonic()-t0:.1f}s) — hostname={result['facts'].get('hostname', '?')}")
    except Exception as exc:
        msg = f"get_facts() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interfaces ---
    log_fn("    [2/5] get_interfaces()...")
    t0 = time.monotonic()
    try:
        result["interfaces"] = device.get_interfaces()
        log_fn(f"    [2/5] get_interfaces() done ({time.monotonic()-t0:.1f}s) — {len(result['interfaces'])} interfaces")
    except Exception as exc:
        msg = f"get_interfaces() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interface IPs (L3 + VLAN SVIs) ---
    log_fn("    [3/5] get_interfaces_ip()...")
    t0 = time.monotonic()
    try:
        result["interfaces_ip"] = device.get_interfaces_ip()
        log_fn(f"    [3/5] get_interfaces_ip() done ({time.monotonic()-t0:.1f}s) — {len(result['interfaces_ip'])} L3 interfaces")
    except Exception as exc:
        msg = f"get_interfaces_ip() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- VLANs ---
    log_fn("    [4/5] get_vlans()...")
    t0 = time.monotonic()
    try:
        result["vlans"] = device.get_vlans()
        log_fn(f"    [4/5] get_vlans() done ({time.monotonic()-t0:.1f}s) — {len(result['vlans'])} VLANs")
    except Exception as exc:
        # Many drivers don't support get_vlans(); treat as non-fatal
        log_fn(f"    [4/5] get_vlans() not supported ({time.monotonic()-t0:.1f}s) — skipped")
        logger.debug("get_vlans() not supported or failed: %s", exc)

    # --- Neighbors (LLDP / CDP) ---
    log_fn(f"    [5/5] neighbor discovery (protocol={discovery_protocol})...")
    t0 = time.monotonic()
    neighbors = []
    if discovery_protocol in ("lldp", "both"):
        try:
            lldp_detail = device.get_lldp_neighbors_detail()
            for local_iface, neighbor_list in lldp_detail.items():
                for n in neighbor_list:
                    neighbors.append(
                        {
                            "source": "lldp",
                            "local_interface": local_iface,
                            "remote_hostname": n.get("remote_system_name", ""),
                            "remote_interface": n.get("remote_port", ""),
                            "remote_ip": _extract_neighbor_ip(n),
                            "remote_description": n.get("remote_system_description", ""),
                        }
                    )
            log_fn(f"    [5/5] LLDP detail: {len(neighbors)} neighbors ({time.monotonic()-t0:.1f}s)")
        except Exception as exc:
            logger.debug("get_lldp_neighbors_detail() failed: %s", exc)
            # Try basic LLDP
            try:
                lldp = device.get_lldp_neighbors()
                for local_iface, neighbor_list in lldp.items():
                    for n in neighbor_list:
                        neighbors.append(
                            {
                                "source": "lldp",
                                "local_interface": local_iface,
                                "remote_hostname": n.get("hostname", ""),
                                "remote_interface": n.get("port", ""),
                                "remote_ip": "",
                                "remote_description": "",
                            }
                        )
                log_fn(f"    [5/5] LLDP basic: {len(neighbors)} neighbors ({time.monotonic()-t0:.1f}s)")
            except Exception as exc2:
                log_fn(f"    [5/5] LLDP not available ({time.monotonic()-t0:.1f}s)")
                logger.debug("get_lldp_neighbors() also failed: %s", exc2)

    if discovery_protocol in ("cdp", "both"):
        # NAPALM exposes CDP via get_lldp_neighbors on IOS; also try cli for explicit CDP
        cdp_t0 = time.monotonic()
        log_fn("    [5/5] CDP: running 'show cdp neighbors detail'...")
        try:
            cdp_data = _get_cdp_via_cli(device, driver_name)
            neighbors.extend(cdp_data)
            log_fn(f"    [5/5] CDP: {len(cdp_data)} neighbors ({time.monotonic()-cdp_t0:.1f}s)")
        except Exception as exc:
            log_fn(f"    [5/5] CDP failed ({time.monotonic()-cdp_t0:.1f}s): {exc}")
            logger.debug("CDP CLI collection failed: %s", exc)

    log_fn(f"    [5/5] neighbor discovery done — {len(neighbors)} total neighbors ({time.monotonic()-t0:.1f}s)")
    result["neighbors"] = neighbors

    # --- Cisco Stack detection (IOS only) ---
    log_fn("    [6/6] Checking for Cisco StackWise members...")
    t0 = time.monotonic()
    stack_members = _detect_cisco_stack(device, driver_name, log_fn)
    if len(stack_members) > 1:
        log_fn(
            f"    [6/6] Stack detected: {len(stack_members)} member(s) "
            f"({time.monotonic()-t0:.1f}s)"
        )
    else:
        log_fn(f"    [6/6] Not a stack or not supported ({time.monotonic()-t0:.1f}s)")
    result["stack_members"] = stack_members

    return result


def _extract_neighbor_ip(neighbor_data: Dict) -> str:
    """Extract the best IP from LLDP neighbor detail data."""
    # Try management address first
    mgmt = neighbor_data.get("remote_system_capab", {})
    # NAPALM LLDP detail provides management_ip in some drivers
    for key in ("management_ip", "remote_management_ip", "remote_port_id"):
        val = neighbor_data.get(key, "")
        if val and _looks_like_ip(val):
            return val
    return ""


def _looks_like_ip(s: str) -> bool:
    """Basic check if string looks like an IPv4 address."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _get_cdp_via_cli(device, driver_name: str) -> List[Dict]:
    """
    For Cisco IOS/NX-OS devices, parse 'show cdp neighbors detail' CLI output.
    This gives richer neighbor data including management IPs.
    """
    if driver_name not in ("ios", "nxos", "nxos_ssh"):
        return []

    neighbors = []
    try:
        output = device.cli(["show cdp neighbors detail"])
        cdp_output = output.get("show cdp neighbors detail", "")
        neighbors = _parse_cdp_neighbors(cdp_output)
    except Exception as exc:
        logger.debug("CDP CLI failed: %s", exc)

    return neighbors


def _detect_cisco_stack(device, driver_name: str, log_fn: Callable) -> List[Dict]:
    """
    For Cisco IOS/IOS-XE devices, detect StackWise stack members via CLI.

    Runs 'show switch' to get the member list (position, role, MAC, priority)
    and 'show inventory' to get the per-member serial number and model (PID).

    Returns a list of dicts — one per stack member — in position order:
        [{"position": 1, "role": "active",  "serial": "FCW001", "model": "WS-C3850-48P", ...},
         {"position": 2, "role": "member",  "serial": "FCW002", "model": "WS-C3850-48P", ...}]

    Returns an empty list if the device is not a stack or if the commands fail.
    Only attempted for the 'ios' driver.
    """
    if driver_name not in ("ios",):
        return []

    try:
        output = device.cli(["show switch", "show inventory"])
    except Exception as exc:
        log_fn(f"    [stack] CLI failed: {exc}")
        return []

    members = _parse_show_switch(output.get("show switch", ""))
    if len(members) <= 1:
        # Single-switch or unrecognised output — not a multi-member stack
        return members

    _enrich_from_inventory(members, output.get("show inventory", ""))
    return members


def _parse_show_switch(output: str) -> List[Dict]:
    """
    Parse 'show switch' output into a list of stack member dicts.

    Handles both IOS-XE and IOS formats, e.g.:
        *1   Active   0011.2233.4401   15   V04   Ready
         2   Member   0011.2233.4402    1   V04   Ready
         3   Standby  0011.2233.4403    5   V04   Ready
    """
    members = []
    # '*' prefix marks the active switch
    pattern = re.compile(
        r"^\s*\*?\s*(\d+)\s+"
        r"(Active|Standby|Stand-by|Member|Provisioning|Waiting)\s+"
        r"([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})\s+"
        r"(\d+)",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        m = pattern.match(line)
        if m:
            members.append({
                "position": int(m.group(1)),
                "role": m.group(2).lower().replace("-", ""),
                "mac": m.group(3),
                "priority": int(m.group(4)),
                "serial": "",
                "model": "",
            })
    members.sort(key=lambda x: x["position"])
    return members


def _enrich_from_inventory(members: List[Dict], output: str) -> None:
    """
    Parse 'show inventory' and populate the serial/model fields in-place.

    Relevant section looks like:
        NAME: "Switch 1", DESCR: "WS-C3850-48P-E"
        PID: WS-C3850-48P-E   , VID: V04, SN: FCW2105G00J

        NAME: "Switch 2", DESCR: "WS-C3850-48P-E"
        PID: WS-C3850-48P-E   , VID: V04, SN: FCW2048H03A
    """
    current_pos = None
    name_re = re.compile(r'NAME:\s*"Switch\s+(\d+)"', re.IGNORECASE)
    pid_sn_re = re.compile(r"PID:\s*(\S+)\s*,.*SN:\s*(\S+)", re.IGNORECASE)

    for line in output.splitlines():
        nm = name_re.search(line)
        if nm:
            current_pos = int(nm.group(1))
            continue

        if current_pos is not None:
            pm = pid_sn_re.search(line)
            if pm:
                pid, sn = pm.group(1).strip(), pm.group(2).strip()
                for member in members:
                    if member["position"] == current_pos:
                        member["model"] = pid
                        member["serial"] = sn
                        break
                current_pos = None


def _parse_cdp_neighbors(output: str) -> List[Dict]:
    """
    Parse 'show cdp neighbors detail' text output into neighbor dicts.
    Handles both IOS and NX-OS output format variations.
    """
    neighbors = []
    current = {}

    for line in output.splitlines():
        line = line.strip()

        if line.startswith("Device ID:") or line.startswith("Device ID :"):
            if current:
                neighbors.append(current)
            current = {
                "source": "cdp",
                "local_interface": "",
                "remote_hostname": line.split(":", 1)[-1].strip(),
                "remote_interface": "",
                "remote_ip": "",
                "remote_description": "",
            }
        elif line.startswith("Interface:") and current:
            # "Interface: GigabitEthernet0/1,  Port ID (outgoing port): Gi0/0"
            parts = line.split(",")
            current["local_interface"] = parts[0].split(":", 1)[-1].strip()
            if len(parts) > 1 and "Port ID" in parts[1]:
                current["remote_interface"] = parts[1].split(":")[-1].strip()
        elif line.startswith("IP address:") and current:
            current["remote_ip"] = line.split(":", 1)[-1].strip()
        elif line.startswith("IP Address:") and current:
            current["remote_ip"] = line.split(":", 1)[-1].strip()
        elif line.startswith("Platform:") and current:
            # "Platform: cisco WS-C2960, ..."
            platform_part = line.split(":", 1)[-1].split(",")[0].strip()
            current["remote_description"] = platform_part

    if current:
        neighbors.append(current)

    return neighbors
