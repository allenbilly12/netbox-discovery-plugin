"""
Remove any stored credential values from historical changelog entries.

DiscoveryTarget saves write prechange/postchange JSON into core.ObjectChange.
The credential columns are named with a leading underscore, which NetBox's
serialize_object() drops, and DiscoveryTarget.serialize_object() now strips
them explicitly as well — so current saves are clean.

This command exists for history: entries written before that guarantee was
made explicit, or by a NetBox version that serialized the fields anyway. It is
idempotent and safe to run repeatedly.

Note that where no encryption_key was configured, the value in those rows is
the plaintext SSH password.

    python manage.py nbdiscovery_scrub_changelog --dry-run
    python manage.py nbdiscovery_scrub_changelog
"""

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction

# Both the model attribute names and the underlying db_column names, since
# which one was serialized depends on the NetBox version that wrote the row.
SECRET_KEYS = (
    "_credential_password",
    "_enable_secret",
    "credential_password",
    "enable_secret",
)

# Written in place of a removed value so the redaction is visible rather than
# looking like the field never existed.
REDACTED = "[redacted by nbdiscovery_scrub_changelog]"

BATCH_SIZE = 500


class Command(BaseCommand):
    help = "Strip stored credentials from historical DiscoveryTarget changelog entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        try:
            from core.models import ObjectChange
        except ImportError:  # pragma: no cover - NetBox < 4.1 path
            from extras.models import ObjectChange

        from netbox_discovery.models import DiscoveryTarget

        target_ct = ContentType.objects.get_for_model(DiscoveryTarget)

        # changed_object_type is the field on ObjectChange that records which
        # model the entry describes.
        queryset = ObjectChange.objects.filter(changed_object_type=target_ct).order_by("pk")

        total = queryset.count()
        if not total:
            self.stdout.write("No DiscoveryTarget changelog entries found — nothing to do.")
            return

        self.stdout.write(f"Scanning {total} DiscoveryTarget changelog entr(ies)...")

        scrubbed = 0
        inspected = 0

        for entry in queryset.iterator(chunk_size=BATCH_SIZE):
            inspected += 1
            dirty = False

            for attr in ("prechange_data", "postchange_data"):
                data = getattr(entry, attr, None)
                if not isinstance(data, dict):
                    continue
                for key in SECRET_KEYS:
                    # Only rewrite when a *value* is present; an empty string
                    # reveals nothing and rewriting it would be pure churn.
                    if data.get(key):
                        data[key] = REDACTED
                        dirty = True

            if not dirty:
                continue

            scrubbed += 1
            if dry_run:
                self.stdout.write(f"  would scrub ObjectChange pk={entry.pk}")
                continue

            with transaction.atomic():
                # Update only the JSON columns. Saving the whole object would
                # touch an audit record's own metadata.
                entry.save(update_fields=["prechange_data", "postchange_data"])

        verb = "would be scrubbed" if dry_run else "scrubbed"
        self.stdout.write(
            self.style.SUCCESS(
                f"Inspected {inspected} entr(ies); {scrubbed} {verb}."
            )
        )
        if dry_run and scrubbed:
            self.stdout.write("Re-run without --dry-run to apply.")
