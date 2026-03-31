import netaddr

from django import forms
from netbox.forms import NetBoxModelForm, NetBoxModelFilterSetForm
from utilities.forms.fields import TagFilterField
from utilities.forms.rendering import FieldSet

from .choices import (
    DiscoveryProtocolChoices,
    NapalmDriverChoices,
)
from .models import DiscoveryTarget, DiscoveryRun


class DiscoveryTargetForm(NetBoxModelForm):
    """Form for creating / editing a DiscoveryTarget."""

    # Password fields displayed as password inputs but never pre-populated
    credential_password = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text=(
            "SSH password. Leave blank to keep existing value or use the global default."
        ),
    )
    enable_secret = forms.CharField(
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="Enable / privilege-level password (Cisco). Optional.",
    )

    fieldsets = (
        FieldSet("name", "description", "targets", "exclusions", name="Basic"),
        FieldSet(
            "credential_username",
            "credential_password",
            "enable_secret",
            name="Credentials",
        ),
        FieldSet(
            "napalm_driver",
            "discovery_protocol",
            "max_depth",
            "ssh_timeout",
            "max_workers",
            name="Discovery Settings",
        ),
        FieldSet("scan_interval", "enabled", name="Scheduling"),
        FieldSet("tags", name="Tags"),
    )

    class Meta:
        model = DiscoveryTarget
        fields = (
            "name",
            "description",
            "targets",
            "exclusions",
            "credential_username",
            "credential_password",
            "enable_secret",
            "napalm_driver",
            "discovery_protocol",
            "max_depth",
            "ssh_timeout",
            "max_workers",
            "scan_interval",
            "enabled",
            "tags",
        )
        widgets = {
            "targets": forms.Textarea(attrs={"rows": 6, "placeholder": "10.0.0.1\n192.168.1.0/24"}),
            "max_depth": forms.NumberInput(attrs={"min": 0, "max": 10}),
            "ssh_timeout": forms.NumberInput(attrs={"min": 1, "max": 120}),
            "max_workers": forms.NumberInput(attrs={"min": 1, "max": 50}),
            "scan_interval": forms.NumberInput(attrs={"min": 0}),
        }

    def _validate_ip_lines(self, field_name: str) -> str:
        """Validate that every non-blank line is a valid IP address or CIDR."""
        value = self.cleaned_data.get(field_name, "")
        errors = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                netaddr.IPNetwork(line, implicit_prefix=False)
            except (netaddr.AddrFormatError, ValueError):
                errors.append(f"'{line}' is not a valid IP address or CIDR range.")
        if errors:
            raise forms.ValidationError(errors)
        return value

    def clean_targets(self):
        return self._validate_ip_lines("targets")

    def clean_exclusions(self):
        return self._validate_ip_lines("exclusions")

    def save(self, commit=True):
        instance = super().save(commit=False)

        # Handle password fields: only update if a new value was entered
        new_password = self.cleaned_data.get("credential_password")
        if new_password:
            instance.credential_password = new_password
        elif not instance.pk:
            # New object with no password — store empty
            instance.credential_password = ""

        new_enable = self.cleaned_data.get("enable_secret")
        if new_enable:
            instance.enable_secret = new_enable
        elif not instance.pk:
            instance.enable_secret = ""

        if commit:
            instance.save()
            self.save_m2m()
        return instance


class DiscoveryTargetFilterForm(NetBoxModelFilterSetForm):
    """Filter form for the DiscoveryTarget list view."""

    model = DiscoveryTarget
    tag = TagFilterField(model)

    napalm_driver = forms.ChoiceField(
        choices=[("", "---------")] + NapalmDriverChoices.choices,
        required=False,
    )
    discovery_protocol = forms.ChoiceField(
        choices=[("", "---------")] + DiscoveryProtocolChoices.choices,
        required=False,
    )
    enabled = forms.NullBooleanField(required=False)


class DiscoveryRunFilterForm(NetBoxModelFilterSetForm):
    """Filter form for the DiscoveryRun list view."""

    model = DiscoveryRun
    tag = TagFilterField(model)
