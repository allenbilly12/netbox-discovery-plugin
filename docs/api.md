# netbox_discovery/api/

## Purpose

Django REST Framework (DRF) API layer. Provides CRUD endpoints for `DiscoveryTarget` and read-only endpoints for `DiscoveryRun`, all under `/api/plugins/discovery/`.

Namespace: `plugins-api:netbox_discovery-api:<name>`

---

## api/urls.py

Registers two viewsets on a `NetBoxRouter`:

| Prefix | ViewSet | Name prefix |
|--------|---------|-------------|
| `targets` | `DiscoveryTargetViewSet` | `discoverytarget` |
| `runs` | `DiscoveryRunViewSet` | `discoveryrun` |

NetBoxRouter auto-generates standard list/detail/action URLs.

---

## api/views.py

### DiscoveryTargetViewSet

Full CRUD (`NetBoxModelViewSet`). Adds a custom action:

**POST `/api/plugins/discovery/targets/{id}/run/`**

Validates the target is enabled and has credentials, then enqueues a `DiscoveryJob`. Returns `{"detail": "..."}` with 200 on success or 400/500 on failure.

### DiscoveryRunViewSet

Read-only — `http_method_names = ["get", "head", "options"]`. No create/update/delete.

---

## api/serializers.py

### DiscoveryTargetSerializer

All target fields exposed except:
- `_credential_password` and `_enable_secret` are replaced by masked `"********"` via `SerializerMethodField` — passwords are never returned by the API.

### DiscoveryRunSerializer

All fields are `read_only_fields`. `target` is a `SerializerMethodField` returning `{id, name, url}` rather than a nested object to keep the response flat.

---

## How to Change

- **Add a new API field**: Add it to `Meta.fields` in the serializer. If it should be write-only, add it to `write_only_fields` or use `extra_kwargs`.
- **Add a new custom action**: Add a method decorated with `@action(detail=True/False, methods=["post"])` to the relevant ViewSet.
- **Make runs writable**: Remove `http_method_names` restriction from `DiscoveryRunViewSet` and remove `read_only_fields` from the serializer.
- **Add filtering to the API**: Add `filterset_class = DiscoveryTargetFilterSet` to the ViewSet.
