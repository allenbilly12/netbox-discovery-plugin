"""
NAPALM data collection from a connected device.

Collects:
- Facts (hostname, vendor, model, serial, os_version)
- Stack members (Cisco IOS only): position, role, serial, model via show switch + show inventory
- Interfaces (name, enabled, description, mtu, mac)
- LAG / port-channel membership (best-effort for Cisco IOS/NX-OS)
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
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Collect all available data from an open NAPALM device.

    Args:
        device: Open NAPALM driver instance.
        driver_name: The NAPALM driver name (for logging).
        discovery_protocol: 'lldp', 'cdp', or 'both'.
        log_fn: Optional progress callback (same as job log_fn).
        options: Plugin config options (collect_vrfs, collect_inventory, etc.).

    Returns:
        Dict with keys: facts, interfaces, interfaces_ip, vlans, neighbors, raw_errors,
        and optionally vrfs, inventory_items, environment.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)
    if options is None:
        options = {}

    collect_vrfs = options.get("collect_vrfs", False)
    collect_inventory = options.get("collect_inventory", False)

    result = {
        "facts": {},
        "interfaces": {},
        "lag_members": {},
        "interfaces_ip": {},
        "vlans": {},
        "neighbors": [],
        "stack_members": [],
        "raw_errors": [],
        "step_status": {
            "facts": "pending",
            "interfaces": "pending",
            "lag": "pending",
            "interfaces_ip": "pending",
            "vlans": "pending",
            "neighbors": "pending",
            "stack": "pending",
        },
    }

    # Optional data keys
    if collect_vrfs:
        result["vrfs"] = {}
        result["step_status"]["vrfs"] = "pending"
    if collect_inventory:
        result["inventory_items"] = []
        result["step_status"]["inventory"] = "pending"
    # Dynamic step count based on enabled options
    STEPS = 7  # base: facts, interfaces, LAG, IPs, VLANs, neighbors, stack
    if collect_vrfs:
        STEPS += 1
    if collect_inventory:
        STEPS += 1

    # --- Facts ---
    log_fn(f"    [1/{STEPS}] get_facts()...")
    t0 = time.monotonic()
    try:
        result["facts"] = device.get_facts()
        result["step_status"]["facts"] = "ok"
        log_fn(f"    [1/{STEPS}] get_facts() done ({time.monotonic()-t0:.1f}s) — hostname={result['facts'].get('hostname', '?')}")
    except Exception as exc:
        result["step_status"]["facts"] = "fail"
        msg = f"get_facts() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interfaces ---
    log_fn(f"    [2/{STEPS}] get_interfaces()...")
    t0 = time.monotonic()
    try:
        result["interfaces"] = device.get_interfaces()
        result["step_status"]["interfaces"] = "ok"
        log_fn(f"    [2/{STEPS}] get_interfaces() done ({time.monotonic()-t0:.1f}s) — {len(result['interfaces'])} interfaces")
    except Exception as exc:
        result["step_status"]["interfaces"] = "fail"
        msg = f"get_interfaces() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- LAG / Port-channel members ---
    log_fn(f"    [3/{STEPS}] LAG member discovery...")
    t0 = time.monotonic()
    try:
        result["lag_members"] = _collect_lag_members(device, driver_name)
        result["step_status"]["lag"] = "ok"
        lag_count = len(result["lag_members"])
        member_count = sum(len(members) for members in result["lag_members"].values())
        if lag_count:
            log_fn(
                f"    [3/{STEPS}] LAG member discovery done ({time.monotonic()-t0:.1f}s) "
                f"— {lag_count} bundle(s), {member_count} member(s)"
            )
        else:
            log_fn(f"    [3/{STEPS}] LAG member discovery done ({time.monotonic()-t0:.1f}s) — none found")
    except Exception as exc:
        result["step_status"]["lag"] = "fail"
        msg = f"LAG member discovery failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- Interface IPs (L3 + VLAN SVIs) ---
    log_fn(f"    [4/{STEPS}] get_interfaces_ip()...")
    t0 = time.monotonic()
    try:
        result["interfaces_ip"] = device.get_interfaces_ip()
        result["step_status"]["interfaces_ip"] = "ok"
        log_fn(f"    [4/{STEPS}] get_interfaces_ip() done ({time.monotonic()-t0:.1f}s) — {len(result['interfaces_ip'])} L3 interfaces")
    except Exception as exc:
        result["step_status"]["interfaces_ip"] = "fail"
        msg = f"get_interfaces_ip() failed ({time.monotonic()-t0:.1f}s): {exc}"
        log_fn(f"    [WARN] {msg}")
        logger.warning(msg)
        result["raw_errors"].append(msg)

    # --- VLANs ---
    log_fn(f"    [5/{STEPS}] get_vlans()...")
    t0 = time.monotonic()
    try:
        result["vlans"] = device.get_vlans()
        result["step_status"]["vlans"] = "ok"
        log_fn(f"    [5/{STEPS}] get_vlans() done ({time.monotonic()-t0:.1f}s) — {len(result['vlans'])} VLANs")
    except Exception as exc:
        result["step_status"]["vlans"] = "skip"
        # Many drivers don't support get_vlans(); treat as non-fatal
        log_fn(f"    [5/{STEPS}] get_vlans() not supported ({time.monotonic()-t0:.1f}s) — skipped")
        logger.debug("get_vlans() not supported or failed: %s", exc)

    # --- Neighbors (LLDP / CDP) ---
    log_fn(f"    [6/{STEPS}] neighbor discovery (protocol={discovery_protocol})...")
    t0 = time.monotonic()
    neighbors = []
    lldp_success = discovery_protocol not in ("lldp", "both")
    cdp_success = discovery_protocol not in ("cdp", "both")
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
            lldp_success = True
            log_fn(f"    [6/{STEPS}] LLDP detail: {len(neighbors)} neighbors ({time.monotonic()-t0:.1f}s)")
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
                lldp_success = True
                log_fn(f"    [6/{STEPS}] LLDP basic: {len(neighbors)} neighbors ({time.monotonic()-t0:.1f}s)")
            except Exception as exc2:
                log_fn(f"    [6/{STEPS}] LLDP not available ({time.monotonic()-t0:.1f}s)")
                logger.debug("get_lldp_neighbors() also failed: %s", exc2)

    if discovery_protocol in ("cdp", "both"):
        # NAPALM exposes CDP via get_lldp_neighbors on IOS; also try cli for explicit CDP
        cdp_t0 = time.monotonic()
        log_fn(f"    [6/{STEPS}] CDP: running 'show cdp neighbors detail'...")
        try:
            cdp_data = _get_cdp_via_cli(device, driver_name)
            neighbors.extend(cdp_data)
            cdp_success = True
            log_fn(f"    [6/{STEPS}] CDP: {len(cdp_data)} neighbors ({time.monotonic()-cdp_t0:.1f}s)")
        except Exception as exc:
            log_fn(f"    [6/{STEPS}] CDP failed ({time.monotonic()-cdp_t0:.1f}s): {exc}")
            logger.debug("CDP CLI collection failed: %s", exc)

    # Deduplicate: when protocol="both", the same physical connection can appear
    # in both LLDP and CDP data.  Keep the first occurrence of each
    # (local_interface, remote_hostname) pair, preferring LLDP (richer data).
    seen_pairs: set = set()
    deduped = []
    for n in neighbors:
        key = (
            n.get("local_interface", "").lower(),
            n.get("remote_hostname", "").lower(),
        )
        if key not in seen_pairs:
            seen_pairs.add(key)
            deduped.append(n)
    if len(deduped) < len(neighbors):
        log_fn(
            f"    [6/{STEPS}] Deduplicated {len(neighbors) - len(deduped)} duplicate "
            f"neighbor entries (LLDP+CDP overlap)"
        )
    neighbors = deduped

    log_fn(f"    [6/{STEPS}] neighbor discovery done — {len(neighbors)} total neighbors ({time.monotonic()-t0:.1f}s)")
    result["neighbors"] = neighbors
    result["step_status"]["neighbors"] = "ok" if (lldp_success and cdp_success) else "fail"

    # --- Cisco Stack detection (IOS only) ---
    log_fn(f"    [7/{STEPS}] Checking for Cisco StackWise members...")
    t0 = time.monotonic()
    if driver_name not in ("ios",):
        result["step_status"]["stack"] = "skip"
    else:
        result["step_status"]["stack"] = "ok"
    stack_members = _detect_cisco_stack(device, driver_name, log_fn)
    if len(stack_members) > 1:
        log_fn(
            f"    [7/{STEPS}] Stack detected: {len(stack_members)} member(s) "
            f"({time.monotonic()-t0:.1f}s)"
        )
    else:
        log_fn(f"    [7/{STEPS}] Not a stack or not supported ({time.monotonic()-t0:.1f}s)")
    result["stack_members"] = stack_members

    # --- Optional steps (Tier 2) ---
    next_step = 8

    # --- VRFs via get_network_instances() (Tier 2.1) ---
    if collect_vrfs:
        log_fn(f"    [{next_step}/{STEPS}] get_network_instances()...")
        t0 = time.monotonic()
        try:
            result["vrfs"] = device.get_network_instances()
            result["step_status"]["vrfs"] = "ok"
            log_fn(f"    [{next_step}/{STEPS}] get_network_instances() done ({time.monotonic()-t0:.1f}s) — {len(result['vrfs'])} instance(s)")
        except Exception as exc:
            result["step_status"]["vrfs"] = "skip"
            log_fn(f"    [{next_step}/{STEPS}] get_network_instances() not supported ({time.monotonic()-t0:.1f}s) — skipped")
            logger.debug("get_network_instances() failed: %s", exc)
        next_step += 1

    # --- Inventory items via show inventory (Tier 2.2) ---
    if collect_inventory:
        log_fn(f"    [{next_step}/{STEPS}] Inventory collection...")
        t0 = time.monotonic()
        try:
            result["inventory_items"] = _collect_inventory(device, driver_name)
            result["step_status"]["inventory"] = "ok"
            log_fn(f"    [{next_step}/{STEPS}] Inventory done ({time.monotonic()-t0:.1f}s) — {len(result['inventory_items'])} item(s)")
        except Exception as exc:
            result["step_status"]["inventory"] = "fail"
            msg = f"Inventory collection failed ({time.monotonic()-t0:.1f}s): {exc}"
            log_fn(f"    [WARN] {msg}")
            logger.warning(msg)
            result["raw_errors"].append(msg)
        next_step += 1

    return result


def _extract_neighbor_ip(neighbor_data: Dict) -> str:
    """Extract the best IP from LLDP neighbor detail data."""
    import netaddr as _netaddr

    # NAPALM LLDP detail provides management_ip in some drivers
    for key in ("management_ip", "remote_management_ip", "remote_port_id"):
        val = (neighbor_data.get(key) or "").strip()
        if val:
            try:
                _netaddr.IPAddress(val)
                return val
            except _netaddr.AddrFormatError:
                continue
    return ""


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


def _is_valid_neighbor_ipv4(value: str) -> bool:
    """Return True when *value* is a valid IPv4 address."""
    try:
        import ipaddress

        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except Exception:
        return False


def _collect_lag_members(device, driver_name: str) -> Dict[str, List[str]]:
    """
    Best-effort collection of LAG/port-channel membership via device CLI.
    """
    if driver_name not in ("ios", "nxos", "nxos_ssh"):
        return {}

    commands = ["show etherchannel summary"]
    if driver_name in ("nxos", "nxos_ssh"):
        commands.insert(0, "show port-channel summary")

    for command in commands:
        try:
            output = device.cli([command]).get(command, "")
        except Exception as exc:
            logger.debug("LAG CLI command failed (%s): %s", command, exc)
            continue
        lag_members = _parse_lag_summary(output)
        if lag_members:
            return lag_members

    return {}


def _parse_lag_summary(output: str) -> Dict[str, List[str]]:
    """
    Parse IOS/NX-OS etherchannel summary style output.
    """
    lag_members: Dict[str, List[str]] = {}
    current_lag = None

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("flags:") or set(line) <= {"-", "+", " "}:
            continue

        match = re.match(
            r"^\d+\s+((?:po|port-channel|bundle-ether|be|ae)\S+?)(?:\([^)]+\))?\s+(.*)$",
            line,
            re.IGNORECASE,
        )
        if match:
            current_lag = match.group(1)
            lag_members.setdefault(current_lag, [])
            _extend_unique(lag_members[current_lag], _extract_lag_member_names(match.group(2)))
            continue

        if current_lag and not re.match(r"^\d+\s", line):
            _extend_unique(lag_members[current_lag], _extract_lag_member_names(line))

    return {lag: members for lag, members in lag_members.items() if members}


def _extract_lag_member_names(text: str) -> List[str]:
    members = []
    for token in re.findall(r"([A-Za-z][A-Za-z0-9./:-]+)(?:\([A-Za-z]+\))", text):
        members.append(token.rstrip(","))
    return members


def _extend_unique(items: List[str], new_items: List[str]) -> None:
    for item in new_items:
        if item not in items:
            items.append(item)


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


def _collect_inventory(device, driver_name: str) -> List[Dict]:
    """
    Collect all inventory items via CLI 'show inventory' (IOS, NX-OS, EOS)
    or 'show chassis hardware' (Junos).

    Returns a list of dicts: [{"name": ..., "pid": ..., "serial": ..., "description": ...}]
    """
    commands = []
    if driver_name in ("ios", "nxos", "nxos_ssh", "eos"):
        commands = ["show inventory"]
    elif driver_name == "junos":
        commands = ["show chassis hardware"]
    else:
        return []

    for command in commands:
        try:
            output = device.cli([command]).get(command, "")
        except Exception:
            continue
        if output:
            return _parse_inventory_output(output, driver_name)

    return []


def _parse_inventory_output(output: str, driver_name: str) -> List[Dict]:
    """
    Parse 'show inventory' style output into inventory item dicts.

    Handles the standard Cisco NAME/PID/SN format:
        NAME: "Power Supply 1", DESCR: "350W AC Power Supply"
        PID: PWR-C1-350WAC    , VID: V01, SN: LIT12345678
    """
    items = []
    name_re = re.compile(r'NAME:\s*"([^"]+)"(?:,\s*DESCR:\s*"([^"]*)")?', re.IGNORECASE)
    pid_re = re.compile(r"PID:\s*([^,]*)", re.IGNORECASE)
    sn_re = re.compile(r"SN:\s*([^,\s]*)", re.IGNORECASE)

    current_name = None
    current_descr = ""
    current_pid = ""
    current_sn = ""

    def flush_current() -> None:
        nonlocal current_name, current_descr, current_pid, current_sn
        if current_name is None:
            return

        # Keep peripheral entries even when PID/SN are blank; many PSU/fan
        # records omit one of those fields but are still useful in NetBox.
        items.append({
            "name": current_name,
            "pid": current_pid,
            "serial": current_sn,
            "description": current_descr,
        })
        current_name = None
        current_descr = ""
        current_pid = ""
        current_sn = ""

    for line in output.splitlines():
        nm = name_re.search(line)
        if nm:
            flush_current()
            current_name = nm.group(1).strip()
            current_descr = (nm.group(2) or "").strip()
            continue

        if current_name is not None:
            pid_match = pid_re.search(line)
            if pid_match:
                current_pid = pid_match.group(1).strip()

            sn_match = sn_re.search(line)
            if sn_match:
                current_sn = sn_match.group(1).strip()

    flush_current()

    return items


def _parse_cdp_neighbors(output: str) -> List[Dict]:
    """
    Parse 'show cdp neighbors detail' text output into neighbor dicts.
    Handles both IOS and NX-OS output format variations.
    """
    neighbors = []
    current = {}
    in_management_addresses = False

    def finalize(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not entry:
            return None

        management_ips = entry.pop("_management_ips", [])
        candidate_ips = entry.pop("_candidate_ips", [])

        for ip_list in (management_ips, candidate_ips):
            for candidate in ip_list:
                if _is_valid_neighbor_ipv4(candidate):
                    entry["remote_ip"] = candidate
                    return entry

        entry["remote_ip"] = ""
        return entry

    for line in output.splitlines():
        line = line.strip()

        if line.startswith("Device ID:") or line.startswith("Device ID :"):
            if current:
                finalized = finalize(current)
                if finalized:
                    neighbors.append(finalized)
            current = {
                "source": "cdp",
                "local_interface": "",
                "remote_hostname": line.split(":", 1)[-1].strip(),
                "remote_interface": "",
                "remote_ip": "",
                "remote_description": "",
                "_candidate_ips": [],
                "_management_ips": [],
            }
            in_management_addresses = False
        elif line.startswith("Entry address(es):") and current:
            in_management_addresses = False
        elif line.startswith("Management address(es):") and current:
            in_management_addresses = True
        elif line.startswith("Interface:") and current:
            # "Interface: GigabitEthernet0/1,  Port ID (outgoing port): Gi0/0"
            parts = line.split(",")
            current["local_interface"] = parts[0].split(":", 1)[-1].strip()
            if len(parts) > 1 and "Port ID" in parts[1]:
                current["remote_interface"] = parts[1].split(":")[-1].strip()
        elif current and line.lower().startswith(("ip address:", "ipv4 address:")):
            ip_value = line.split(":", 1)[-1].strip()
            if in_management_addresses:
                current["_management_ips"].append(ip_value)
            else:
                current["_candidate_ips"].append(ip_value)
        elif line.startswith("Platform:") and current:
            # "Platform: cisco WS-C2960, ..."
            platform_part = line.split(":", 1)[-1].split(",")[0].strip()
            current["remote_description"] = platform_part

    if current:
        finalized = finalize(current)
        if finalized:
            neighbors.append(finalized)

    return neighbors
