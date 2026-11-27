# 0033 — S3-backed PBS datastores

Status: Accepted (2026-11-26, rule chosen by the user: "an S3 datastore is offsite unless marked
on-site", the mark per S3 endpoint and audited). Built 2026-11-27 on `feat/s3-datastores` (on top of
ADR 0032). Shapes checked live 2026-11-27 against PBS 4.2.14 with a RustFS 1.0.7 test bucket.

## Context

PBS 4.x can keep a datastore in an S3 object store. RESURGAM treated such a datastore like any other.
The live test showed what goes wrong:

- **3-2-1:** a local sync job copying a group into an S3 datastore on the same PBS was rated "Same PBS
  only", although the data sits in an object store somewhere else.
- **Capacity:** PBS reports `used`/`avail`/`total` of an S3 datastore for the *local cache
  filesystem*. On the test PBS that is the root disk: 78.8 % used for a bucket holding 83 MB. RESURGAM
  would have raised "datastore filling up" alarms and a "full in N days" forecast for the bucket.
- **Billing:** the per-GB estimate scaled each group by `used_bytes / logical_bytes`, which for S3 is
  the cache disk.

## Facts this design relies on (checked against the vendor's own docs and a live PBS 4.2)

- `GET /config/datastore` carries `backend: "type=s3,client=<endpoint id>,bucket=<bucket>"` for an S3
  datastore; `path` is then the local cache. No `backend` = filesystem.
- `GET /config/s3` lists endpoints: `id`, `endpoint`, `port`, `region`, `path-style`, `fingerprint`,
  `access-key` (the secret is never returned). Readable with RESURGAM's audit token. Creating an
  endpoint needs `Sys.Modify` on `/system/s3-endpoint`, which RESURGAM's token does not have and does
  not need.
- `GET /status/datastore-usage` and `GET /admin/datastore` carry `backend-type` (`filesystem` | `s3`).
  `gc-status.disk-bytes` is what the last garbage collection found in the chunk store; before any GC,
  PBS reports zeros with `upid: null`.
- `GET /admin/datastore/{store}/status` has `s3-statistics` for an S3 datastore: `get`, `put`, `post`,
  `head`, `delete` request counts and `uploaded`/`downloaded` bytes.
- PBS docs (storage, "Datastores with S3 Backend"): the local cache is "used to increase performance
  and reduce the number of requests"; "64 GiB to 128 GiB are recommended", best on "a dedicated disk,
  partition or ZFS dataset with quota". Providers "might charge you for storage space and API
  requests", and "monitoring of these values and eventual costs is highly recommended". The docs do not
  say what happens when the cache filesystem fills up.
- PBS docs (notifications, "Notification Thresholds and Reset Schedule (S3 Datastores Only)"): PBS
  counts requests and traffic per S3 datastore, and the counters are reset on an optional "threshold
  reset schedule". The counters are therefore not monotonic.

## Decision

1. **Collector.**
   - Datastore discovery stores `backend_type`, `s3_endpoint_name` and `s3_bucket`.
   - A new endpoint sync keeps a `pbs_s3_endpoints` row per PBS endpoint (host, port, region,
     path-style). It runs only for instances that have an S3 datastore, and `removed_from_pbs_at` is
     set when an endpoint disappears.
   - Each usage measurement of an S3 datastore also stores the bucket's data (`s3_data_bytes` =
     `gc-status.disk-bytes`, null before the first GC) and the seven S3 counters, read with one status
     call per S3 datastore.
2. **3-2-1 (extends ADR 0032).** An S3 datastore counts as offsite unless its endpoint is marked
   on-site. An endpoint RESURGAM has not read yet also counts as offsite: the rule is "offsite unless
   marked".
   - A copy edge whose target is such a datastore is an offsite copy, also on the same PBS. Its copy
     location names the bucket.
   - A group stored directly on such a datastore gets the new status **`stored_offsite`**. There is no
     alarm, and the Recovery Evidence leg counts it as met (method `s3_datastore`). Copy edges don't
     matter for it: the backup already is offsite.
   - The mark is set with `PUT /s3-endpoints/{id}/location` (platform admins), with a required reason
     (at least 10 characters). It is stored with who and when on the endpoint, and written as audit
     event `s3_endpoint_location_changed` with the before/after value. The Collector never touches it.
     Unlike gap exceptions it is not append-only: the history is the audit log.
3. **Capacity.**
   - Usage alarms stay for S3 datastores, worded "local S3 cache filesystem N % used". A full cache
     disk is a real problem: on the test PBS it is the root filesystem.
   - The "full in N days" forecast is `not_applicable` and raises no alarm: a bucket has no fixed size,
     and the cache's growth says nothing about it.
   - The 24 h / 30 d change figures follow `s3_data_bytes` (rows without it are skipped).
   - The Capacity page shows the bucket size, the cache usage, and S3 traffic over 30 days.
4. **S3 traffic** (`s3_traffic_24h` / `_30d` on the usage endpoint) is summed from consecutive
   measurements. A counter lower than in the row before means a reset, so that step counts the new
   value. Traffic between the last measurement and a reset is not seen, so the figure is a lower bound.
5. **Billing.** For an S3 datastore the dedup ratio uses `s3_data_bytes`. Until a GC has run, its groups
   are left out and `physical_bytes_estimate_complete` is false, which is honest rather than estimated
   from the cache.
   - S3 data has its own price on the pricing plan, `price_per_s3_gb_month` (user, 2026-11-27: "an own
     price and optional, as the S3 provider could be paid otherwise"). Null = not charged per GB; it
     never falls back to `price_per_gb_month`.
   - The billing output reports the S3 share as `estimated_s3_bytes` (part of
     `estimated_physical_bytes`), so the rest is charged at the normal per-GB price. Migration 0075.
6. **Zabbix.**
   - Datastore discovery gets `{#DATASTORE_BACKEND}`. The usage payload gets `backend_type` and
     `s3_data_bytes`.
   - `days_until_full` is always -1 for S3, so the "full in" triggers never fire.
   - The usage-percent triggers keep firing for S3, consistent with the cache-disk alarm above.
   - The template itself is unchanged.

## Consequences

- RESURGAM never writes to PBS for any of this; every call is a GET its audit token may already make.
- The bucket size depends on PBS's garbage collection. A datastore without a GC schedule shows "size
  known after the first garbage collection" indefinitely. The manual recommends a GC schedule (PBS
  needs one anyway to free space in the bucket).
- Request counters are not cost figures. RESURGAM does not know provider prices and does not compute
  any.
- Not covered: PBS's own S3 notification thresholds (configured on PBS), and the S3 provider's own
  quota or bucket size (RESURGAM has no S3 credentials and does not want any).
