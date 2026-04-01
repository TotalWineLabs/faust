## Why

The key join processor (`KeyJoinProcessor`) fully implements the KIP-213 subscription/response protocol but its table-change handlers (`_on_left_table_change`, `_on_right_table_change`) are never wired to actual table write events. Table writes currently trigger `on_key_set`/`on_key_del` for changelog and sensor dispatch, but there is no mechanism for external components (like key joins) to register callbacks on these events. The key join infrastructure is complete but inert.

> **Note (2026-03-17):** This change was originally written against the `ForeignKeyJoin` / `fkjoin` naming. The feature has since been renamed to `KeyJoin` / `keyjoin`. See change `rename-fk-join-to-key-join`.

## What Changes

- **Add a callback registration mechanism to `Table`**: Extend `on_key_set` and `on_key_del` in `Collection`/`Table` to maintain and invoke a list of registered callbacks when keys are set or deleted.
- **Convert key join change handlers to synchronous**: Replace `await self.subscription_topic.send(...)` / `await self.response_topic.send(...)` with `send_soon()` (sync fire-and-forget to producer buffer), matching the pattern already used by `send_changelog()`.
- **Wire key join processor to tables on startup**: Register the key join's left/right change handlers as callbacks on the respective tables during `KeyJoinProcessor.on_start()`.
- **Unregister callbacks on stop**: Remove callbacks from tables during `on_stop()` to prevent stale references.
- **No impact on recovery**: The recovery write path (`Store.apply_changelog_batch`) writes directly via `Store._set()`, bypassing `ManagedUserDict` and `on_key_set` entirely — no changes needed to keep recovery safe.

## Capabilities

### New Capabilities
- `table-write-callbacks`: A general-purpose callback registration system on Table, allowing external components to subscribe to key-set and key-del events dispatched through `on_key_set`/`on_key_del`.
- `fk-join-event-wiring`: Wiring the key join processor's change handlers to their respective left and right tables via the table-write-callbacks mechanism, making the subscription/response protocol active.

### Modified Capabilities

_(none — no existing specs to modify)_

## Impact

- **Code**: `faust/tables/base.py` (Collection), `faust/tables/table.py` (Table), `faust/tables/keyjoin.py` (KeyJoinProcessor)
- **APIs**: New public methods on `Collection`/`Table` for registering/unregistering write callbacks
- **Dependencies**: None — uses existing `send_soon()` and `ManagedUserDict` dispatch, both already in production
- **Risk**: Low — recovery bypasses `on_key_set` by design (verified via `Store.apply_changelog_batch` → `Store._set()`), so key join callbacks cannot fire during recovery
