"""
BFS (breadth-first search) recursive neighbor crawler.

Starting from seed IPs, connects to each device, collects data,
then enqueues newly discovered neighbor IPs for further processing
up to max_depth levels deep.
"""

import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .collector import collect_device_data
from .driver_detect import detect_and_connect

logger = logging.getLogger("netbox.plugins.netbox_discovery")


def crawl(
    seed_ips: Set[str],
    username: str,
    password: str,
    enable_secret: str = "",
    timeout: int = 10,
    preferred_driver: str = "auto",
    max_depth: int = 3,
    discovery_protocol: str = "both",
    on_device_data: Optional[Callable[[str, Dict[str, Any], str], None]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    BFS crawl starting from seed_ips, following LLDP/CDP neighbors.

    Args:
        seed_ips: Starting set of live IP addresses.
        username: SSH username.
        password: SSH password.
        enable_secret: Optional enable password.
        timeout: SSH timeout per connection.
        preferred_driver: 'auto' or specific NAPALM driver name.
        max_depth: Maximum neighbor recursion depth.
        on_device_data: Callback invoked with (ip, device_data_dict, driver_name)
                        for each successfully collected device. Typically writes
                        to NetBox.
        log_fn: Optional log/progress callback.
        stop_flag: Optional callable that returns True to abort early.

    Returns:
        Summary dict with counts.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)
    if stop_flag is None:
        stop_flag = lambda: False

    # Per-device data-collection timeout: enough headroom for all NAPALM calls
    collect_timeout = max(timeout * 6, 60)

    visited: Set[str] = set()
    # queued tracks every IP ever added to the queue so we never enqueue the
    # same IP twice — visited alone isn't sufficient because it's only updated
    # when an IP is *dequeued*, allowing the same IP to accumulate in the queue
    # as a neighbor of multiple devices before it's first processed.
    queued: Set[str] = set(seed_ips)
    # Queue entries: (ip, depth)
    queue: deque = deque()
    for ip in seed_ips:
        queue.append((ip, 0))

    log_fn(f"Crawl starting: {len(seed_ips)} seed IP(s), max_depth={max_depth}, collect_timeout={collect_timeout}s")

    summary = {
        "connected": 0,
        "failed": 0,
        "skipped": 0,
        "neighbors_queued": 0,
    }

    while queue and not stop_flag():
        ip, depth = queue.popleft()

        if ip in visited:
            summary["skipped"] += 1
            continue
        visited.add(ip)

        # Outer per-IP guard: any unhandled exception skips to the next device
        try:
            remaining = len(queue)
            log_fn(f"[depth={depth}] Connecting to {ip}... (queue: {remaining} remaining, {len(visited)} visited)")

            device, driver_name = detect_and_connect(
                ip=ip,
                username=username,
                password=password,
                enable_secret=enable_secret,
                timeout=timeout,
                preferred_driver=preferred_driver,
                log_fn=log_fn,
            )

            if device is None:
                log_fn(f"  [FAILED] {ip} — could not connect with any driver. Skipping.")
                summary["failed"] += 1
                continue

            try:
                log_fn(f"  [OK] Connected via '{driver_name}'. Collecting data (timeout={collect_timeout}s)...")
                # Run collection in a thread with a hard wall-clock deadline so a
                # hung NAPALM call (e.g. get_interfaces_ip on a large device) can't
                # stall the entire crawl indefinitely.
                executor = ThreadPoolExecutor(max_workers=1)
                future = executor.submit(
                    collect_device_data, device, driver_name, discovery_protocol, log_fn
                )
                try:
                    data = future.result(timeout=collect_timeout)
                except FuturesTimeoutError:
                    log_fn(f"  [ERROR] Data collection for {ip} timed out after {collect_timeout}s — skipping device")
                    logger.error("collect_device_data timed out for %s", ip)
                    summary["failed"] += 1
                    continue
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)

                facts = data.get("facts", {})
                hostname = facts.get("hostname", ip)
                log_fn(
                    f"  Hostname: {hostname} | Vendor: {facts.get('vendor', '?')} "
                    f"| Model: {facts.get('model', '?')} | Serial: {facts.get('serial_number', '?')}"
                )

                if data.get("raw_errors"):
                    for err in data["raw_errors"]:
                        log_fn(f"  [WARN] {err}")

                # NetBox sync
                if on_device_data:
                    try:
                        on_device_data(ip, data, driver_name)
                    except Exception as exc:
                        log_fn(f"  [ERROR] NetBox sync failed for {ip}: {exc} — continuing")
                        logger.exception("Sync error for %s", ip)

                summary["connected"] += 1

                # Enqueue neighbors
                if depth < max_depth:
                    new_ips = _extract_neighbor_ips(data.get("neighbors", []))
                    for neighbor_ip in new_ips:
                        if neighbor_ip and neighbor_ip not in queued:
                            log_fn(f"  Queuing neighbor: {neighbor_ip} (depth={depth + 1})")
                            queue.append((neighbor_ip, depth + 1))
                            queued.add(neighbor_ip)
                            summary["neighbors_queued"] += 1
                        elif neighbor_ip in queued and neighbor_ip not in visited:
                            log_fn(f"  Neighbor {neighbor_ip} already queued — skipping duplicate")

            except Exception as exc:
                log_fn(f"  [ERROR] Data collection failed for {ip}: {exc} — skipping device")
                logger.exception("Collection error for %s", ip)
                summary["failed"] += 1
            finally:
                try:
                    device.close()
                except Exception:
                    pass

        except Exception as exc:
            log_fn(f"[ERROR] Unexpected error processing {ip}: {exc} — continuing")
            logger.exception("Unexpected crawl error for %s", ip)
            summary["failed"] += 1

    log_fn(
        f"Crawl complete. Connected: {summary['connected']}, "
        f"Failed: {summary['failed']}, Neighbors queued: {summary['neighbors_queued']}"
    )
    return summary


def _extract_neighbor_ips(neighbors: List[Dict]) -> List[str]:
    """Extract valid IP addresses from neighbor entries."""
    ips = []
    for n in neighbors:
        ip = n.get("remote_ip", "").strip()
        if ip and _is_valid_ip(ip) and not _is_link_local(ip):
            ips.append(ip)
    return ips


def _is_valid_ip(ip: str) -> bool:
    """Check if string is a valid IPv4 address."""
    try:
        import netaddr
        netaddr.IPAddress(ip)
        return True
    except Exception:
        return False


def _is_link_local(ip: str) -> bool:
    """Skip link-local and loopback addresses."""
    try:
        import netaddr
        addr = netaddr.IPAddress(ip)
        return addr.is_link_local() or addr.is_loopback() or addr.is_private() and str(ip).startswith("127.")
    except Exception:
        return False
