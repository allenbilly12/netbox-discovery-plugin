"""
BFS (breadth-first search) recursive neighbor crawler.

Starting from seed IPs, connects to each device, collects data,
then enqueues newly discovered neighbor IPs for further processing
up to max_depth levels deep.

Devices are processed in parallel using a thread pool (max_workers).
Django ORM is thread-safe — each thread gets its own DB connection
from the pool automatically.
"""

import logging
import queue as _queue_mod
import threading
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
    on_device_failed: Optional[Callable[[str, str], None]] = None,
    log_fn: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    max_workers: int = 5,
) -> Dict[str, Any]:
    """
    Concurrent BFS crawl starting from seed_ips, following LLDP/CDP neighbors.

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
        on_device_failed: Optional callback invoked with (ip, error_message) for
                          each device that failed to connect or collect data.
        log_fn: Optional log/progress callback (must be thread-safe).
        stop_flag: Optional callable that returns True to abort early.
        max_workers: Number of devices to process in parallel.

    Returns:
        Summary dict with counts.
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)
    if stop_flag is None:
        stop_flag = lambda: False

    # Per-device data-collection timeout: enough headroom for all NAPALM calls
    collect_timeout = max(timeout * 6, 60)

    # Thread-safe work queue; each item is (ip, depth) or None (poison pill).
    work_queue: _queue_mod.Queue = _queue_mod.Queue()

    # Lock protects all shared mutable state.
    lock = threading.Lock()
    visited: Set[str] = set()
    queued: Set[str] = set(seed_ips)  # IPs ever enqueued — prevents duplicates
    summary = {
        "connected": 0,
        "failed": 0,
        "skipped": 0,
        "neighbors_queued": 0,
    }

    for ip in seed_ips:
        work_queue.put((ip, 0))

    actual_workers = min(max_workers, max(1, len(seed_ips)))
    log_fn(
        f"Crawl starting: {len(seed_ips)} seed IP(s), max_depth={max_depth}, "
        f"workers={actual_workers}, collect_timeout={collect_timeout}s"
    )

    def worker():
        # Each thread needs its own Django DB connection released on exit.
        try:
            from django.db import connection as _db_conn
        except ImportError:
            _db_conn = None

        try:
            while True:
                item = work_queue.get()

                # Poison pill — time to exit.
                if item is None:
                    work_queue.task_done()
                    break

                ip, depth = item
                try:
                    if stop_flag():
                        with lock:
                            summary["skipped"] += 1
                        continue

                    with lock:
                        if ip in visited:
                            summary["skipped"] += 1
                            continue
                        visited.add(ip)
                        n_visited = len(visited)

                    remaining = work_queue.qsize()
                    log_fn(
                        f"[depth={depth}] Connecting to {ip}... "
                        f"(queue≈{remaining}, visited={n_visited})"
                    )

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
                        with lock:
                            summary["failed"] += 1
                        if on_device_failed:
                            on_device_failed(ip, "Could not connect with any driver")
                        continue

                    try:
                        log_fn(
                            f"  [OK] Connected via '{driver_name}'. "
                            f"Collecting data (timeout={collect_timeout}s)..."
                        )
                        # Hard wall-clock deadline so a hung NAPALM call can't
                        # stall this worker thread indefinitely.
                        executor = ThreadPoolExecutor(max_workers=1)
                        future = executor.submit(
                            collect_device_data, device, driver_name, discovery_protocol, log_fn
                        )
                        try:
                            data = future.result(timeout=collect_timeout)
                        except FuturesTimeoutError:
                            log_fn(
                                f"  [ERROR] Data collection for {ip} timed out "
                                f"after {collect_timeout}s — skipping device"
                            )
                            logger.error("collect_device_data timed out for %s", ip)
                            with lock:
                                summary["failed"] += 1
                            if on_device_failed:
                                on_device_failed(ip, f"Data collection timed out after {collect_timeout}s")
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

                        with lock:
                            summary["connected"] += 1

                        # Enqueue neighbors
                        if depth < max_depth:
                            new_ips = _extract_neighbor_ips(data.get("neighbors", []))
                            for neighbor_ip in new_ips:
                                enqueue = False
                                with lock:
                                    if neighbor_ip and neighbor_ip not in queued:
                                        queued.add(neighbor_ip)
                                        enqueue = True
                                    elif neighbor_ip in queued and neighbor_ip not in visited:
                                        log_fn(
                                            f"  Neighbor {neighbor_ip} already queued — skipping duplicate"
                                        )
                                if enqueue:
                                    log_fn(f"  Queuing neighbor: {neighbor_ip} (depth={depth + 1})")
                                    work_queue.put((neighbor_ip, depth + 1))
                                    with lock:
                                        summary["neighbors_queued"] += 1

                    except Exception as exc:
                        log_fn(f"  [ERROR] Data collection failed for {ip}: {exc} — skipping device")
                        logger.exception("Collection error for %s", ip)
                        with lock:
                            summary["failed"] += 1
                        if on_device_failed:
                            on_device_failed(ip, str(exc))
                    finally:
                        try:
                            device.close()
                        except Exception:
                            pass

                except Exception as exc:
                    log_fn(f"[ERROR] Unexpected error processing {ip}: {exc} — continuing")
                    logger.exception("Unexpected crawl error for %s", ip)
                    with lock:
                        summary["failed"] += 1
                finally:
                    work_queue.task_done()

        finally:
            # Release this thread's Django DB connection back to the pool.
            if _db_conn is not None:
                try:
                    _db_conn.close()
                except Exception:
                    pass

    # Start worker threads.
    threads = [
        threading.Thread(target=worker, daemon=True, name=f"discovery-worker-{i}")
        for i in range(actual_workers)
    ]
    for t in threads:
        t.start()

    # Block until every item (including dynamically enqueued neighbors) is done.
    work_queue.join()

    # Send poison pills so workers exit their blocking get() calls.
    for _ in range(actual_workers):
        work_queue.put(None)
    for t in threads:
        t.join(timeout=5)

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
