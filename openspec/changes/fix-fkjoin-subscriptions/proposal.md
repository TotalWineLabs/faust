## Why

The key join subscription sender (`_send_subscription`) uses the bare extracted key as the
Kafka message key.  This is wrong for two reasons:

1. **Lost uniqueness on the compacted subscription topic.**  The subscription
   topic is compacted.  With `key=FK`, all subscriptions for the same key
   collapse into a single record.  Only the last left-row that subscribed is
   kept — every earlier subscription is silently dropped.  The correct key is
   the composite `FK\x00PK` so each (extracted-key, PK) pair compacts independently.

2. **Partition must be derived from extracted key alone.**  The composite key must not
   drive partitioning.  Subscriptions must land on the same partition as the
   right-table value for that key so `prefix_scan` and right-side lookup work
   correctly.  The partition must therefore be computed explicitly via
   `app.producer.key_partition(changelog_topic, FK.encode()).partition` and
   supplied to `send_soon` via its `partition` kwarg.

> **Note (2026-03-17):** This change was originally written against the `ForeignKeyJoin` / `fkjoin` naming. The feature has since been renamed to `KeyJoin` / `keyjoin`. See change `rename-fk-join-to-key-join`.

## What Changes

- `_send_subscription` sends with composite key `f"{fk}\x00{left_pk}"` and
  explicit `partition` computed from extracted key alone.
- `_run_subscription_agent` / `_process_subscription` parse the composite key
  from the stream event to extract the join key and PK instead of relying on a bare key
  and the `left_pk` field inside the message body.
- Tests are updated to verify composite keying and explicit partitioning.

## Capabilities

### New Capabilities

- `fkjoin-subscription-keying`: Composite-key subscription messages with
  extracted-key-only partitioning for the key join protocol.

### Modified Capabilities

_None._

## Impact

- **Code**: `faust/tables/keyjoin.py` — `_send_subscription`,
  `_process_subscription`, `_run_subscription_agent`.
- **Tests**: `t/unit/tables/test_keyjoin.py` — existing subscription-sender and
  subscription-processor tests, integration tests.
- **Wire protocol**: The subscription topic message key changes from bare extracted key to
  `FK\x00PK`.  This is a **rolling-upgrade-incompatible** change for any
  deployment already running the previous key join implementation.
