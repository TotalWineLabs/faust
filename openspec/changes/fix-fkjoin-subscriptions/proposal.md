## Why

The FK join subscription sender (`_send_subscription`) uses the bare FK as the
Kafka message key.  This is wrong for two reasons:

1. **Lost uniqueness on the compacted subscription topic.**  The subscription
   topic is compacted.  With `key=FK`, all subscriptions for the same FK
   collapse into a single record.  Only the last left-row that subscribed is
   kept — every earlier subscription is silently dropped.  The correct key is
   the composite `FK\x00PK` so each (FK, PK) pair compacts independently.

2. **Partition must be derived from FK alone.**  The composite key must not
   drive partitioning.  Subscriptions must land on the same partition as the
   right-table value for that FK so `prefix_scan` and right-side lookup work
   correctly.  The partition must therefore be computed explicitly via
   `app.producer.key_partition(changelog_topic, FK.encode()).partition` and
   supplied to `send_soon` via its `partition` kwarg.

## What Changes

- `_send_subscription` sends with composite key `f"{fk}\x00{left_pk}"` and
  explicit `partition` computed from FK alone.
- `_run_subscription_agent` / `_process_subscription` parse the composite key
  from the stream event to extract FK and PK instead of relying on a bare FK
  key and the `left_pk` field inside the message body.
- Tests are updated to verify composite keying and explicit partitioning.

## Capabilities

### New Capabilities

- `fkjoin-subscription-keying`: Composite-key subscription messages with
  FK-only partitioning for the FK join protocol.

### Modified Capabilities

_None._

## Impact

- **Code**: `faust/tables/fkjoin.py` — `_send_subscription`,
  `_process_subscription`, `_run_subscription_agent`.
- **Tests**: `t/unit/tables/test_fkjoin.py` — existing subscription-sender and
  subscription-processor tests, integration tests.
- **Wire protocol**: The subscription topic message key changes from bare FK to
  `FK\x00PK`.  This is a **rolling-upgrade-incompatible** change for any
  deployment already running the previous FK join implementation.
