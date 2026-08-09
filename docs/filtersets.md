# netbox_discovery/filtersets.py

## Purpose

`django-filter` FilterSet classes that power URL query-string filtering for the list views and the REST API.

---

## DiscoveryTargetFilterSet

Filters for the Discovery Targets list and API endpoint.

| Filter | Type | Description |
|--------|------|-------------|
| `name` | search (icontains) | Free-text search via `search()` override |
| `napalm_driver` | MultipleChoiceFilter | Filter by one or more driver values |
| `discovery_protocol` | MultipleChoiceFilter | Filter by protocol |
| `enabled` | BooleanFilter | Filter enabled/disabled targets |

## DiscoveryRunFilterSet

Filters for the Run History list and API endpoint.

| Filter | Type | Description |
|--------|------|-------------|
| `target_id` | ModelMultipleChoiceFilter | Filter by one or more targets |
| `status` | MultipleChoiceFilter | Filter by run status |

---

## How to Change

- **Add a new filter**: Add a `django_filters.*Filter` field here and add the field name to `Meta.fields`.
- **Add a search filter**: Override `search(self, queryset, name, value)` and add `"q"` to `Meta.fields`.
- **Wire to the list view**: The filterset is referenced in the list view via `filterset = DiscoveryTargetFilterSet`. The filter form is in `forms.py`.
