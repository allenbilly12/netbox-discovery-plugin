"""
Background jobs for network discovery.

DiscoveryJob: manually-triggered or scheduled job for a single DiscoveryTarget.
discovery_scheduler: system_job that fires every N minutes and enqueues
                     DiscoveryJob for any targets that are due.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from netbox.jobs import JobRunner

logger = logging.getLogger("netbox.plugins.netbox_discovery")


class DiscoveryJob(JobRunner):
    """
    NetBox background job that runs a full discovery cycle for one DiscoveryTarget.
    """

    class Meta:
        name = "Network Discovery"

    def run(self, data, commit=True):
        from .models import DiscoveryRun, DiscoveryTarget
        from .discovery.scanner import scan_targets
        from .discovery.neighbor import crawl
        from .sync.netbox_sync import sync_device

        target_id = data.get("target_id")
        if not target_id:
            self.log_failure("No target_id in job data")
            return

        try:
            target = DiscoveryTarget.objects.get(pk=target_id)
        except DiscoveryTarget.DoesNotExist:
            self.log_failure(f"DiscoveryTarget {target_id} not found")
            return

        # Create a DiscoveryRun record
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

        def log_fn(msg):
            self.log_info(msg)
            log_lines.append(msg)
            logger.info("[DiscoveryJob target=%s] %s", target.name, msg)

        try:
            log_fn(f"=== Discovery started for target: {target.name} ===")
            log_fn(f"Targets: {target.get_target_list()}")
            log_fn(f"Protocol: {target.discovery_protocol}, Max depth: {target.max_depth}")

            # Step 1: Ping sweep
            live_ips = scan_targets(target.get_target_list(), log_fn=log_fn)
            counters["hosts_scanned"] = len(live_ips)
            log_fn(f"Live hosts found: {len(live_ips)}")

            if not live_ips:
                log_fn("No live hosts found. Exiting.")
                _finish_run(run, counters, "completed", "\n".join(log_lines))
                _update_last_run(target)
                return

            # Step 2: BFS crawl + sync
            def on_device(ip, device_data, driver_name):
                try:
                    device_name, was_created = sync_device(
                        mgmt_ip=ip,
                        data=device_data,
                        holding_site_name=holding_site,
                        log_fn=log_fn,
                    )
                    if was_created:
                        counters["devices_created"] += 1
                    else:
                        counters["devices_updated"] += 1
                except Exception as exc:
                    log_fn(f"  [ERROR] sync_device failed for {ip}: {exc}")
                    logger.exception("sync_device error for %s", ip)
                    counters["errors"] += 1

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
                log_fn=log_fn,
            )
            counters["errors"] += crawl_summary.get("failed", 0)

            log_fn(f"=== Discovery complete ===")
            log_fn(
                f"Hosts scanned: {counters['hosts_scanned']} | "
                f"Created: {counters['devices_created']} | "
                f"Updated: {counters['devices_updated']} | "
                f"Errors: {counters['errors']}"
            )

            final_status = "partial" if counters["errors"] > 0 else "completed"
            _finish_run(run, counters, final_status, "\n".join(log_lines))
            _update_last_run(target)

        except Exception as exc:
            log_fn(f"[FATAL] Discovery job crashed: {exc}")
            logger.exception("DiscoveryJob fatal error for target %s", target.name)
            counters["errors"] += 1
            _finish_run(run, counters, "failed", "\n".join(log_lines))
            raise


def _finish_run(run, counters: dict, status: str, log_text: str):
    run.status = status
    run.completed_at = timezone.now()
    run.hosts_scanned = counters["hosts_scanned"]
    run.devices_created = counters["devices_created"]
    run.devices_updated = counters["devices_updated"]
    run.errors = counters["errors"]
    run.log = log_text
    run.save()


def _update_last_run(target):
    target.last_run = timezone.now()
    target.save(update_fields=["last_run"])


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
        due_targets = DiscoveryTarget.objects.filter(enabled=True, scan_interval__gt=0)

        for target in due_targets:
            if target.last_run is None:
                due = True
            else:
                elapsed_minutes = (now - target.last_run).total_seconds() / 60
                due = elapsed_minutes >= target.scan_interval

            if due:
                logger.info(
                    "Scheduling DiscoveryJob for target '%s' (interval=%d min)",
                    target.name,
                    target.scan_interval,
                )
                DiscoveryJob.enqueue(
                    data={"target_id": target.pk},
                    name=f"Discovery: {target.name}",
                )

except ImportError:
    # system_job not available in this NetBox version; scheduling skipped
    logger.warning(
        "netbox.jobs.system_job not available; periodic scheduling disabled. "
        "Use the 'Run Now' button or configure an external cron job instead."
    )
