# netbox_discovery/discovery/neighbor.py

## Purpose

Concurrent BFS (breadth-first search) crawler. Starting from seed IPs, connects to each device, collects data, then enqueues neighbor IPs for the next depth level. Devices at the same depth level are processed in parallel.

---

## crawl(seed_ips, username, password, ...) → Dict

Main entry point. Called by `jobs.py` after host scanning.

### Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `seed_ips` | `Set[str]` | Live IPs from scanner |
| `username` / `password` | str | SSH credentials |
| `enable_secret` | str | Optional enable password |
| `timeout` | int | SSH timeout per connection |
| `preferred_driver` | str | `"auto"` or specific driver name |
| `max_depth` | int | Maximum neighbor hops (default 3) |
| `discovery_protocol` | str | `"lldp"`, `"cdp"`, or `"both"` |
| `on_device_data` | callable | `(ip, data_dict, driver_name)` — called on success |
| `on_device_failed` | callable | `(ip, error_msg)` — called on failure |
| `log_fn` | callable | Thread-safe log callback |
| `stop_flag` | callable | Returns `True` to abort early |
| `max_workers` | int | Parallel worker threads (default 5) |

### Returns

```python
{
    "visited": int,   # total unique IPs processed
    "failed": int,    # devices that failed to connect/collect
}
```

---

## Concurrency Model

Uses a `queue.Queue` (work queue) and a `threading.Lock` (guards `visited` set and `queued` set).

- **`visited`** — IPs already dequeued and processed (prevents re-processing)
- **`queued`** — IPs already in the queue (prevents queue inflation from duplicate neighbor reports)
- Worker threads pull `(ip, depth)` items from the queue. When done, they extract neighbor IPs and enqueue any not already visited/queued — but only if `depth < max_depth`.
- Poison-pill shutdown: after `work_queue.join()` (all items processed), one `None` sentinel is enqueued per worker thread to unblock their `queue.get()` call.
- Each worker closes its Django DB connection in a `finally` block so connections are returned to the pool.

---

## Per-Device Processing (worker thread)

For each `(ip, depth)` item:
1. Call `detect_and_connect()` → open NAPALM device
2. Call `collect_device_data()` → all device data
3. Call `on_device_data(ip, data, driver_name)`
4. Extract neighbor IPs from `data["neighbors"]` via `_extract_neighbor_ips()`
5. Enqueue unseen neighbors at `depth + 1` (if `depth < max_depth`)
6. On any failure: call `on_device_failed(ip, error)`

---

## How to Change

- **Change parallelism**: The `max_workers` parameter is passed from `DiscoveryTarget.max_workers` (configurable per target in the UI).
- **Add a stop condition**: Pass a `stop_flag` callable that returns `True`. Workers check this before processing each item.
- **Add per-device post-processing**: Add logic inside the worker function after `on_device_data` is called.
- **Change neighbor IP extraction**: Update `_extract_neighbor_ips()` — currently reads `nbr["remote_ip"]` from the collector output.
