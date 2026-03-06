"""
NAPALM driver auto-detection.

Tries each driver in priority order (Cisco IOS/NX-OS first per preference).
Returns the first driver that successfully connects and calls get_facts().

Each attempt is capped at (timeout + 2) seconds using a background thread so
that drivers with their own internal timeouts (e.g. EOS/pyeapi uses the OS
default HTTP socket timeout of 60 s) cannot stall the crawl.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Callable, Optional, Tuple

logger = logging.getLogger("netbox.plugins.netbox_discovery")

# Detection order: Cisco-first per user preference, EOS last since
# pyeapi uses HTTP and produces noisy ConnectionRefusedError on non-Arista devices
DETECTION_ORDER = ["ios", "nxos_ssh", "junos", "fortios", "eos"]

# Hostnames that indicate the wrong driver parsed the device's CLI output.
# For example, the IOS driver connecting to NX-OS returns "Kernel" from the
# Linux kernel banner that NX-OS exposes before the NX-OS prompt.
_GARBAGE_HOSTNAMES = {
    "kernel", "localhost", "linux", "ubuntu", "debian", "centos", "redhat",
    "router", "switch", "firewall",
}


def _looks_like_ip(s: str) -> bool:
    """Return True if s looks like an IPv4 address (four dot-separated octets)."""
    parts = s.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _try_driver(
    driver_name: str,
    ip: str,
    username: str,
    password: str,
    enable_secret: str,
    timeout: int,
    log_fn: Callable,
) -> Optional[object]:
    """
    Attempt to connect using the named NAPALM driver.
    Returns the open driver instance on success, None on failure.
    """
    try:
        from napalm import get_network_driver

        driver_cls = get_network_driver(driver_name)

        # optional_args that improve compatibility in lab/production environments
        optional_args = {
            # Disable SSH agent and key-file lookup so password auth is used directly
            "allow_agent": False,
            "look_for_keys": False,
            # Don't load user SSH config (avoids ProxyCommand / IdentityFile issues)
            "ssh_config_file": None,
            # Disable strict host key checking (common in lab environments)
            "ssh_strict": False,
            # Accept legacy ciphers/key-exchange algorithms (needed for older IOS)
            "disabled_algorithms": {
                "pubkeys": ["rsa-sha2-256", "rsa-sha2-512"],
            },
        }
        if enable_secret:
            optional_args["secret"] = enable_secret

        # NX-OS (especially Nexus 7000) can be slow to respond after each command
        # due to supervisor latency.  Without extra delay Netmiko's prompt detection
        # races ahead and logs "Pattern not detected: 'HOSTNAME\#' in output" for
        # every CLI call after the first.  Doubling the global delay factor and
        # disabling fast_cli gives the device enough time to send its prompt.
        if driver_name == "nxos_ssh":
            optional_args["global_delay_factor"] = 2
            optional_args["fast_cli"] = False

        device = driver_cls(
            hostname=ip,
            username=username,
            password=password,
            timeout=timeout,
            optional_args=optional_args,
        )
        device.open()
        facts = device.get_facts()

        # Validate the hostname returned by get_facts(). Some drivers (e.g. IOS)
        # successfully connect to NX-OS devices but misparse the CLI and return
        # garbage like "Kernel". Treat that as a detection failure so we fall
        # through to the correct driver (nxos_ssh).
        reported_hostname = (facts.get("hostname") or "").strip().lower()
        if reported_hostname in _GARBAGE_HOSTNAMES or reported_hostname.startswith("^") or _looks_like_ip(reported_hostname):
            log_fn(
                f"    Driver '{driver_name}' connected but returned garbage hostname "
                f"'{facts.get('hostname')}' — skipping (likely wrong driver for this OS)"
            )
            try:
                device.close()
            except Exception:
                pass
            return None

        return device

    except Exception as exc:
        # Log at INFO so errors appear in the run log, not just system logger
        log_fn(f"    Driver '{driver_name}' failed: {type(exc).__name__}: {exc}")
        return None


def _try_driver_timed(
    driver_name: str,
    ip: str,
    username: str,
    password: str,
    enable_secret: str,
    timeout: int,
    log_fn: Callable,
) -> Optional[object]:
    """
    Wrapper around _try_driver that enforces a hard wall-clock timeout.

    Some drivers (notably EOS/pyeapi) use HTTP connections whose internal
    timeout ignores the NAPALM `timeout` parameter and falls back to the OS
    default (typically 60 s).  Running the attempt in a thread and joining
    with a deadline prevents a single slow driver from stalling the entire
    discovery job.
    """
    # nxos_ssh uses global_delay_factor=2 which doubles all internal Netmiko
    # read delays, making get_facts() take roughly twice as long on slow
    # devices like Nexus 7000 (~10-12s vs ~5-6s).  Give it a proportionally
    # larger deadline so the thread is not killed before get_facts() returns.
    if driver_name == "nxos_ssh":
        deadline = timeout * 2 + 4
    else:
        deadline = timeout + 2

    # Do NOT use 'with ThreadPoolExecutor(...) as executor:' here.
    # The context manager calls shutdown(wait=True) on __exit__, which blocks
    # until the background thread finishes — exactly the 60-second pyeapi hang
    # we are trying to avoid.  Instead, call shutdown(wait=False) so we
    # abandon any stuck thread and return immediately.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _try_driver,
        driver_name, ip, username, password, enable_secret, timeout, log_fn,
    )
    try:
        return future.result(timeout=deadline)
    except FuturesTimeoutError:
        log_fn(
            f"    Driver '{driver_name}' killed after {deadline}s "
            f"(internal timeout did not fire in time)"
        )
        return None
    finally:
        # cancel_futures=True drops queued (not yet started) futures.
        # The already-running thread cannot be cancelled — it will die on its
        # own when pyeapi's socket eventually times out.
        executor.shutdown(wait=False, cancel_futures=True)


def detect_and_connect(
    ip: str,
    username: str,
    password: str,
    enable_secret: str = "",
    timeout: int = 10,
    preferred_driver: str = "auto",
    log_fn: Callable = None,
) -> Tuple[Optional[object], Optional[str]]:
    """
    Attempt to connect to a device using NAPALM.

    Returns:
        Tuple of (open NAPALM driver instance, driver_name) or (None, None).
    """
    if log_fn is None:
        log_fn = lambda msg: logger.info(msg)

    if not username:
        log_fn(f"  [SKIP] No username configured for {ip} — check credentials")
        return None, None

    if preferred_driver and preferred_driver != "auto":
        drivers_to_try = [preferred_driver]
    else:
        drivers_to_try = DETECTION_ORDER

    log_fn(f"  Trying drivers {drivers_to_try} for {ip} (user={username})")

    for driver_name in drivers_to_try:
        device = _try_driver_timed(
            driver_name, ip, username, password, enable_secret, timeout, log_fn
        )
        if device is not None:
            log_fn(f"  [OK] Connected to {ip} via '{driver_name}'")
            return device, driver_name

    log_fn(f"  [FAILED] All drivers exhausted for {ip}")
    return None, None
