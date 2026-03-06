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

        device = driver_cls(
            hostname=ip,
            username=username,
            password=password,
            timeout=timeout,
            optional_args=optional_args,
        )
        device.open()
        device.get_facts()
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
    deadline = timeout + 2  # a little headroom over the NAPALM timeout

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
