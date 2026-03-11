"""
Background jobs for network discovery.

DiscoveryJob: manually-triggered or scheduled job for a single DiscoveryTarget.
discovery_scheduler: system_job that fires every N minutes and enqueues
                     DiscoveryJob for any targets that are due.
"""

import logging
import threading

from django.conf import settings
from django.utils import timezone
from netbox.jobs import JobRunner

logger = logging.getLogger("netbox.plugins.netbox_discovery")


# 1 hour — large enough for any realistic crawl
JOB_TIMEOUT = 3600


class DiscoveryJob(JobRunner):
    """
    NetBox background job that runs a full discovery cycle for one DiscoveryTarget.
    """

    class Meta:
        name = "Network Discovery"
        timeout = JOB_TIMEOUT

    def run(self, data, commit=True):
        from .models import DiscoveryRun, DiscoveryTarget
        from .discovery.scanner import scan_targets
        from .discovery.neighbor import crawl
        from .sync.netbox_sync import sync_device, sync_cables

        target_id = data.get("target_id")
        if not target_id:
            self._safe_log("No target_id in job data")
            return

        try:
            target = DiscoveryTarget.objects.get(pk=target_id)
        except DiscoveryTarget.DoesNotExist:
            self._safe_log(f"DiscoveryTarget {target_id} not found")
            return

        # Clean up any runs that were left in 'running' state by a previously
        # killed worker process (SIGKILL bypasses our finally block).
        _reap_stale_runs(target)

        # Create run record
        run = DiscoveryRun.objects.create(
            target=target,
            status="running",
            started_at=timezone.now(),
        )

        holding_site = settings.PLUGINS_CONFIG.get("netbox_discovery", {}).get(
            "holding_site_name", "Holding"
        )
        ssh_timeout = target.ssh_timeout or settings.PLUGINS_CONFIG.get(
            "netbox_discovery", {}
        ).get("ssh_timeout", 10)

        counters = {
            "hosts_scanned": 0,
            "devices_created": 0,
            "devices_updated": 0,
            "errors": 0,
        }
        log_lines = []
        device_results = []  # [{ip, hostname, status, driver, error}]
        neighbor_records = []  # [{hostname, neighbors}] — for post-crawl cable sync
        log_lock = threading.Lock()  # guards log_lines, counters, device_results, neighbor_records
        final_status = "failed"

        def log_fn(msg):
            with log_lock:
                log_lines.append(msg)
                snapshot = "\n".join(log_lines)
            logger.info("[Discovery:%s] %s", target.name, msg)
            self._safe_log(msg)
            # Flush every line to DB so the UI always shows the latest output
            try:
                run.__class__.objects.filter(pk=run.pk).update(log=snapshot)
            except Exception:
                pass

        try:
            log_fn(f"=== Discovery started: {target.name} ===")
            log_fn(f"Targets: {target.get_target_list()}")
            log_fn(f"Protocol: {target.discovery_protocol} | Max depth: {target.max_depth}")

            # Step 1: host scan
            live_ips = scan_targets(
                target.get_target_list(),
                exclusion_strings=target.get_exclusion_list(),
                log_fn=log_fn,
            )
            counters["hosts_scanned"] = len(live_ips)

            if not live_ips:
                log_fn("No live hosts found. Done.")
                final_status = "completed"
                return

            # Step 2: BFS crawl + NetBox sync
            def on_device(ip, device_data, driver_name):
                try:
                    device_name, was_created = sync_device(
                        mgmt_ip=ip,
                        data=device_data,
                        holding_site_name=holding_site,
                        log_fn=log_fn,
                    )
                    with log_lock:
                        status = "created" if was_created else "updated"
                        device_results.append({
                            "ip": ip,
                            "hostname": device_name,
                            "status": status,
                            "driver": driver_name,
                        })
                        neighbor_records.append({
                            "hostname": device_name,
                            "neighbors": device_data.get("neighbors", []),
                        })
                        if was_created:
                            counters["devices_created"] += 1
                        else:
                            counters["devices_updated"] += 1
                except Exception as exc:
                    log_fn(f"  [ERROR] Sync failed for {ip}: {exc}")
                    logger.exception("sync_device error for %s", ip)
                    with log_lock:
                        device_results.append({
                            "ip": ip,
                            "hostname": None,
                            "status": "failed",
                            "error": str(exc),
                        })
                        counters["errors"] += 1

            def on_device_failed(ip, error):
                with log_lock:
                    device_results.append({
                        "ip": ip,
                        "hostname": None,
                        "status": "failed",
                        "error": error,
                    })

            crawl_summary = crawl(
                seed_ips=live_ips,
                username=target.get_effective_username(),
                password=target.get_effective_password(),
                enable_secret=target.get_effective_enable_secret(),
                timeout=ssh_timeout,
                preferred_driver=target.napalm_driver,
                max_depth=target.max_depth,
                discovery_protocol=target.discovery_protocol,
                on_device_data=on_device,
                on_device_failed=on_device_failed,
                log_fn=log_fn,
                max_workers=target.max_workers,
            )
            counters["errors"] += crawl_summary.get("failed", 0)

            # Step 3: cable sync — wire up CDP/LLDP connections post-crawl
            cables_created = 0
            if neighbor_records:
                log_fn("--- Cable sync (CDP/LLDP neighbors) ---")
                try:
                    cables_created = sync_cables(neighbor_records, log_fn=log_fn)
                except Exception as exc:
                    log_fn(f"  [Cable] sync_cables error: {exc}")
                    logger.exception("sync_cables error for target %s", target.name)
            counters["cables_created"] = cables_created

            final_status = "partial" if counters["errors"] > 0 else "completed"
            log_fn("=" * 60)
            log_fn(
                f"=== DISCOVERY COMPLETE: {target.name} ==="
            )
            log_fn(
                f"    Hosts scanned : {counters['hosts_scanned']}"
            )
            log_fn(
                f"    Devices created: {counters['devices_created']}"
            )
            log_fn(
                f"    Devices updated: {counters['devices_updated']}"
            )
            log_fn(
                f"    Cables created : {cables_created}"
            )
            log_fn(
                f"    Errors         : {counters['errors']}"
            )
            log_fn(
                f"    Status         : {final_status.upper()}"
            )
            log_fn("=" * 60)

        except Exception as exc:
            log_fn(f"[FATAL] Job crashed: {exc}")
            logger.exception("DiscoveryJob fatal error for target %s", target.name)
            counters["errors"] += 1
            final_status = "failed"

        finally:
            # Always update the run record regardless of success/failure/early return
            _finish_run(run, counters, final_status, "\n".join(log_lines), device_results)
            _update_last_run(target)

    def _safe_log(self, msg: str):
        """Log via NetBox JobRunner methods, silently ignoring if unavailable."""
        try:
            self.log_info(msg)
        except Exception:
            try:
                self.job.log(msg)
            except Exception:
                pass  # already logged via Python logger above


def _finish_run(run, counters: dict, status: str, log_text: str, device_results: list = None):
    """Update DiscoveryRun with final status. Never raises."""
    try:
        run.status = status
        run.completed_at = timezone.now()
        run.hosts_scanned = counters.get("hosts_scanned", 0)
        run.devices_created = counters.get("devices_created", 0)
        run.devices_updated = counters.get("devices_updated", 0)
        run.errors = counters.get("errors", 0)
        run.log = log_text
        if device_results is not None:
            run.device_results = device_results
        run.save()
    except Exception as exc:
        logger.error("Failed to save DiscoveryRun %s: %s", run.pk, exc)
        # Last-ditch attempt via queryset update (bypasses model signals)
        try:
            run.__class__.objects.filter(pk=run.pk).update(
                status=status,
                completed_at=timezone.now(),
                log=log_text[:10000],
            )
        except Exception:
            pass


def _update_last_run(target):
    """Update target.last_run. Never raises."""
    try:
        target.__class__.objects.filter(pk=target.pk).update(last_run=timezone.now())
    except Exception as exc:
        logger.error("Failed to update last_run for target %s: %s", target.pk, exc)


def _reap_stale_runs(target):
    """
    Mark any DiscoveryRun for this target that is still 'running' after more
    than JOB_TIMEOUT seconds as 'failed'.  This handles the case where a
    previous worker was killed (SIGKILL) before its finally block could run.
    """
    from datetime import timedelta
    from .models import DiscoveryRun

    cutoff = timezone.now() - timedelta(seconds=JOB_TIMEOUT)
    stale = DiscoveryRun.objects.filter(
        target=target, status="running", started_at__lt=cutoff
    )
    count = stale.update(
        status="failed",
        completed_at=timezone.now(),
        log="[Job killed by RQ worker timeout — no final log available]",
    )
    if count:
        logger.warning(
            "Reaped %d stale run(s) for target '%s'", count, target.name
        )


# ---------------------------------------------------------------------------
# Periodic scheduler (system_job)
# ---------------------------------------------------------------------------

try:
    from netbox.jobs import system_job

    @system_job(interval=5)
    def discovery_scheduler(**kwargs):
        """
        Runs every 5 minutes. Enqueues DiscoveryJob for any enabled targets
        whose scan_interval has elapsed since last_run.
        """
        from .models import DiscoveryTarget

        now = timezone.now()
        for target in DiscoveryTarget.objects.filter(enabled=True, scan_interval__gt=0):
            if target.last_run is None:
                due = True
            else:
                elapsed_minutes = (now - target.last_run).total_seconds() / 60
                due = elapsed_minutes >= target.scan_interval

            if due:
                logger.info(
                    "Scheduling DiscoveryJob for '%s' (interval=%d min)",
                    target.name,
                    target.scan_interval,
                )
                DiscoveryJob.enqueue(data={"target_id": target.pk})

except ImportError:
    logger.warning(
        "netbox.jobs.system_job not available — periodic scheduling disabled."
    )
