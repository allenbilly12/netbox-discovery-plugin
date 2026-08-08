# netbox_discovery/forms.py

## Purpose

Django form classes for creating/editing `DiscoveryTarget` records and filtering list views.

---

## DiscoveryTargetForm

Full create/edit form for `DiscoveryTarget`. Extends `NetBoxModelForm`.

### Fieldsets (UI layout)

| Fieldset | Fields |
|----------|--------|
| Basic | name, description, targets |
| Credentials | credential_username, credential_password, enable_secret |
| Discovery Settings | napalm_driver, discovery_protocol, max_depth, ssh_timeout, max_workers |
| Scheduling | scan_interval, enabled |
| Tags | tags |

### Password Handling

`credential_password` and `enable_secret` are rendered as `PasswordInput` fields and are **never pre-populated** (security). In `save()`:
- If a new value is entered, the model property setter is called (which encrypts it).
- If the field is left blank on an existing object, the stored value is **not overwritten**.
- If the field is blank on a **new** object, an empty string is stored.

---

## DiscoveryTargetFilterForm

Filter form for the `DiscoveryTarget` list view. Provides dropdowns for `napalm_driver`, `discovery_protocol`, and a tri-state `enabled` filter.

## DiscoveryRunFilterForm

Minimal filter form for the `DiscoveryRun` list view. Only provides tag filtering (filtering by target/status is handled by `DiscoveryRunFilterSet`).

---

## How to Change

- **Add a new field to the form**: Add it to `Meta.fields`, place it in a `FieldSet`, and add a widget to `Meta.widgets` if needed.
- **Add a new fieldset**: Add a `FieldSet(...)` entry to the `fieldsets` tuple.
- **Add a new filter**: Add a `django_filters` field here and a corresponding entry in `filtersets.py`.
