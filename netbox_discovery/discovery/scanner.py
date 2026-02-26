"""
Ping sweep using python-nmap to find live hosts in a set of targets.
Falls back to TCP port-22 probe if nmap is not available.
"""

import logging
import socket
from typing import Callable, List, Set

import netaddr

logger = logging.getLogger("netbox.plugins.netbox_discovery")


def _expand_targets(targets: List[str]) -> List[str]:
    """Expand a list of IPs and CIDR strings into individual IP strings."""
    ips = []
    for target in targets:
        target = target.strip()
        if not target:
            continue
        try:
            network = netaddr.IPNetwork(target)
            if network.prefixlen == 32 or network.version == 6 and network.prefixlen == 128:
                ips.append(str(network.ip))
            else:
                # For /32 equiv host addresses in a range, include all usable hosts
                for host in network.iter_hosts():
                    ips.append(str(host))
        except netaddr.AddrFormatError:
            logger.warning("Invalid target address: %s", target)
    return ips


def _nmap_ping_sweep(ips: List[str], log_fn: Callable) -> Set[str]:
    """Use python-nmap to do a fast ICMP ping sweep."""
    try:
        import nmap

        nm = nmap.PortScanner()
        live = set()

        # Process in chunks to avoid extremely long nmap command lines
        chunk_size = 256
        for i in range(0, len(ips), chunk_size):
            chunk = ips[i : i + chunk_size]
            targets_str = " ".join(chunk)
            log_fn(f"  Ping sweep chunk {i//chunk_size + 1}: {len(chunk)} addresses")
            try:
                nm.scan(hosts=targets_str, arguments="-sn -T4 --host-timeout 5s")
                for host in nm.all_hosts():
                    if nm[host].state() == "up":
                        live.add(host)
            except Exception as exc:
                log_fn(f"  nmap chunk error: {exc}")

        return live

    except ImportError:
        logger.warning("python-nmap not installed; falling back to TCP port-22 probe")
        return _tcp_probe(ips, log_fn)


def _tcp_probe(ips: List[str], log_fn: Callable) -> Set[str]:
    """Fallback: attempt TCP connection to port 22 to detect live SSH-capable hosts."""
    live = set()
    for ip in ips:
        try:
            with socket.create_connection((ip, 22), timeout=3):
                live.add(ip)
        except (socket.timeout, ConnectionRefusedError, OSError):
            pass
    return live


def scan_targets(target_strings: List[str], log_fn: Callable = None) -> Set[str]:
    """
    Expand target IPs/CIDRs and ping-sweep to find live hosts.

    Args:
        target_strings: List of IP strings or CIDR notations.
        log_fn: Optional callable for progress messages.

    Returns:
        Set of live IP address strings.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.debug(msg)

    log_fn(f"Expanding {len(target_strings)} target(s)...")
    all_ips = _expand_targets(target_strings)
    log_fn(f"Total individual IPs to scan: {len(all_ips)}")

    if not all_ips:
        return set()

    log_fn("Starting ping sweep...")
    live = _nmap_ping_sweep(all_ips, log_fn)
    log_fn(f"Ping sweep complete. Live hosts: {len(live)}")
    return live
