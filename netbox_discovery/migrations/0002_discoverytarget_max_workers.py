from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_discovery", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="discoverytarget",
            name="max_workers",
            field=models.PositiveIntegerField(
                default=5,
                help_text="Number of devices to crawl in parallel. Increase for faster discovery on large networks.",
            ),
        ),
    ]
