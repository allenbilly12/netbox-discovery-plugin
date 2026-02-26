"""
NAPALM data collection from a connected device.

Collects:
- Facts (hostname, vendor, model, serial, os_version)
- Interfaces (name, enabled, description, mtu, mac)
- Interface IPs (including VLAN SVIs)
- VLANs (if driver supports it)
- LLDP/CDP neighbors (for recursive discovery)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("netbox.plugins.netbox_discovery")


def collect_device_data(
    device,
    driver_name: str,
    discovery_protocol: str = "both",
) -> Dict[str, Any]:
    """
    Collect all available data from an open NAPALM device.

    Args:
        device: Open NAPALM driver instance.
        driver_name: The NAPALM driver name (for logging).
        discovery_protocol: 'lldp', 'cdp', or 'both'.

    Returns:
        Dict with keys: facts, interfaces, interfaces_ip, vlans, neighbors, raw_errors.
    """
    result = {
        "facts": {},
        "interfaces": {},
        "interfaces_ip": {},
        "vlans": {},
        "neighbors": [],
        "raw_errors": [],
    }

    # --- Facts ---
    try:
        result["facts"] = device.get_facts()
        logger.debug("Got facts: hostname=%s", result["facts"].get("hostname"))
    except Exception as exc:
        msg = f"get_facts() failed: {exc}"
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interfaces ---
    try:
        result["interfaces"] = device.get_interfaces()
    except Exception as exc:
        msg = f"get_interfaces() failed: {exc}"
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interface IPs (L3 + VLAN SVIs) ---
    try:
        result["interfaces_ip"] = device.get_interfaces_ip()
    except Exception as exc:
        msg = f"get_interfaces_ip() failed: {exc}"
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- VLANs ---
    try:
        result["vlans"] = device.get_vlans()
    except Exception as exc:
        # Many drivers don't support get_vlans(); treat as non-fatal
        logger.debug("get_vlans() not supported or failed: %s", exc)

    # --- Neighbors (LLDP / CDP) ---
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
            except Exception as exc2:
                logger.debug("get_lldp_neighbors() also failed: %s", exc2)

    if discovery_protocol in ("cdp", "both"):
        # NAPALM exposes CDP via get_lldp_neighbors on IOS; also try cli for explicit CDP
        try:
            cdp_data = _get_cdp_via_cli(device, driver_name)
            neighbors.extend(cdp_data)
        except Exception as exc:
            logger.debug("CDP CLI collection failed: %s", exc)

    result["neighbors"] = neighbors
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
