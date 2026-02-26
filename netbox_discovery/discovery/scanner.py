"""
Host discovery using python-nmap (TCP connect, no root required) or
a pure-Python TCP probe fallback.
"""

import logging
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, List, Set

import netaddr

logger = logging.getLogger("netbox.plugins.netbox_discovery")

# Ports to probe for host-up detection (TCP connect, no root needed)
PROBE_PORTS = [22, 23, 80, 443, 8080, 8443]


def _expand_targets(targets: List[str]) -> List[str]:
    """Expand a list of IPs and CIDR strings into individual IP strings."""
    ips = []
    for target in targets:
        target = target.strip()
        if not target:
            continue
        try:
            network = netaddr.IPNetwork(target)
            if network.prefixlen == 32 or (network.version == 6 and network.prefixlen == 128):
                ips.append(str(network.ip))
            else:
                for host in network.iter_hosts():
                    ips.append(str(host))
        except netaddr.AddrFormatError:
            logger.warning("Invalid target address: %s", target)
    return ips


def _nmap_tcp_scan(ips: List[str], log_fn: Callable) -> Set[str]:
    """
    Use nmap with TCP connect scan — works without root privileges.
    --unprivileged forces non-raw-socket methods.
    """
    try:
        import nmap

        nm = nmap.PortScanner()
        live = set()
        ports = ",".join(str(p) for p in PROBE_PORTS)

        chunk_size = 256
        for i in range(0, len(ips), chunk_size):
            chunk = ips[i : i + chunk_size]
            targets_str = " ".join(chunk)
            log_fn(f"  Scanning chunk {i // chunk_size + 1}: {len(chunk)} addresses")
            try:
                # --unprivileged = no raw sockets needed (works as non-root)
                # -sT = TCP connect scan
                # -T4 = aggressive timing
                # --open = only show open ports
                nm.scan(
                    hosts=targets_str,
                    arguments=f"--unprivileged -sT -T4 -p {ports} --open --host-timeout 10s",
                )
                for host in nm.all_hosts():
                    if nm[host].state() == "up":
                        live.add(host)
                    # Also check: host has at least one open port
                    elif "tcp" in nm[host]:
                        for port_info in nm[host]["tcp"].values():
                            if port_info.get("state") == "open":
                                live.add(host)
                                break
            except Exception as exc:
                log_fn(f"  nmap chunk error: {exc} — falling back to TCP probe for this chunk")
                live |= _tcp_probe(chunk, log_fn)

        return live

    except ImportError:
        logger.warning("python-nmap not installed; using TCP probe")
        return _tcp_probe(ips, log_fn)


def _tcp_probe_single(ip: str) -> bool:
    """Try TCP connect to any of the probe ports. Returns True if host is reachable."""
    for port in PROBE_PORTS:
        try:
            with socket.create_connection((ip, port), timeout=2):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            continue
    return False


def _tcp_probe(ips: List[str], log_fn: Callable) -> Set[str]:
    """Pure-Python TCP connect probe using a thread pool for speed."""
    live = set()
    log_fn(f"  TCP probe: checking {len(ips)} addresses (ports {PROBE_PORTS})")
    with ThreadPoolExecutor(max_workers=50) as executor:
        future_to_ip = {executor.submit(_tcp_probe_single, ip): ip for ip in ips}
        for future in as_completed(future_to_ip):
            ip = future_to_ip[future]
            try:
                if future.result():
                    live.add(ip)
            except Exception:
                pass
    return live


def scan_targets(target_strings: List[str], log_fn: Callable = None) -> Set[str]:
    """
    Expand target IPs/CIDRs and scan to find live hosts.

    Individual IPs (/32) specified explicitly always bypass the scanner and
    go straight to the crawl — the user listed them intentionally and NAPALM
    will report a clean failure if they're unreachable.  CIDR ranges are
    scanned first so we don't attempt NAPALM on every address in a /24.

    Args:
        target_strings: List of IP strings or CIDR notations.
        log_fn: Optional callable for progress messages.

    Returns:
        Set of live IP address strings.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)

    # Separate explicit single IPs from CIDR ranges
    single_ips: Set[str] = set()
    range_targets: List[str] = []

    for target in target_strings:
        target = target.strip()
        if not target:
            continue
        try:
            net = netaddr.IPNetwork(target)
            if net.prefixlen >= 32 or (net.version == 6 and net.prefixlen >= 128):
                single_ips.add(str(net.ip))
            else:
                range_targets.append(target)
        except netaddr.AddrFormatError:
            logger.warning("Invalid target address: %s", target)

    if single_ips:
        log_fn(
            f"{len(single_ips)} individual IP(s) will be tried directly "
            f"(no scan needed): {sorted(single_ips)}"
        )

    live: Set[str] = set(single_ips)

    if range_targets:
        range_ips = _expand_targets(range_targets)
        log_fn(f"Scanning {len(range_ips)} IPs from {len(range_targets)} CIDR range(s)...")
        if range_ips:
            log_fn(f"Starting host discovery (TCP connect on ports {PROBE_PORTS})...")
            found = _nmap_tcp_scan(range_ips, log_fn)
            if not found:
                log_fn("nmap returned 0 hosts — retrying with pure TCP probe...")
                found = _tcp_probe(range_ips, log_fn)
            live |= found

    log_fn(f"Host discovery complete. Live hosts: {len(live)}")
    return live
