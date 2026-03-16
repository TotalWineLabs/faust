## Context

The FK join processor (`FKJoinProcessor` in `faust/tables/fkjoin.py`) implements the KIP-213 subscription/response protocol for foreign-key joins between two Faust tables. The protocol is architecturally complete: subscription topics, response topics, subscription stores, hash-based staleness detection, and background stream agents are all in place.

However, `_on_left_table_change` and `_on_right_table_change` — the two handlers that trigger subscription and response messages when table data changes — are never wired to actual table write events. Table writes flow through `ManagedUserDict.__setitem__` → `Table.on_key_set()`, which dispatches to the changelog producer and sensors, but has no mechanism for external callbacks.

Key architectural facts:
- **Normal write path**: `table[key] = value` → `ManagedUserDict.__setitem__` → `Table.on_key_set(key, value)` → changelog + sensors. This is the hook point.
- **Recovery write path**: `Store.apply_changelog_batch()` → `Store._set()` directly, bypassing `ManagedUserDict` entirely. Recovery MUST NOT trigger FK join callbacks.
- **`send_soon()`**: Synchronous fire-and-forget to the producer buffer, already used by `send_changelog()`. Suitable for callbacks invoked from within the synchronous `on_key_set`.

## Goals / Non-Goals

**Goals:**
- Enable `Table` to accept external write-event callbacks that fire on `on_key_set` and `on_key_del`
- Wire `FKJoinProcessor` to the left and right tables so the subscription/response protocol activates on table writes
- Maintain zero impact on the recovery write path
- Keep the callback mechanism synchronous (matching the existing `on_key_set`/`on_key_del` contract)

**Non-Goals:**
- Windowed table support for FK joins (future work)
- Async callback dispatch or queue-based decoupling (unnecessary complexity)
- Changes to `ManagedUserDict` or the `mode` library
- General pub/sub or event bus abstraction beyond simple callback lists

## Decisions

### 1. Callback lists on `Collection` base class

**Decision**: Add `_on_key_set_callbacks: List[Callable]` and `_on_key_del_callbacks: List[Callable]` to `Collection.__init__`, with `register_on_key_set(cb)` / `unregister_on_key_set(cb)` / `register_on_key_del(cb)` / `unregister_on_key_del(cb)` public methods.

**Rationale**: Placing callbacks on the `Collection` base class (rather than `Table` subclass) keeps the API available to all table types. Using simple lists with register/unregister is the minimal mechanism — no observer pattern framework or event bus needed.

**Alternatives considered**:
- *Signals/events on Table*: Over-engineered for a callback list use case.
- *Constructor dependency injection*: Too inflexible — FK join processor needs to register/unregister dynamically during its lifecycle.

### 2. Invoke callbacks from `Table.on_key_set` / `Table.on_key_del`

**Decision**: After the existing changelog send and sensor dispatch in `on_key_set`/`on_key_del`, iterate `_on_key_set_callbacks` / `_on_key_del_callbacks` and call each with `(key, value)` or `(key,)` respectively.

**Rationale**: This guarantees that the changelog entry is already produced before FK join callbacks fire. Callbacks run synchronously in the same call stack as the table write — no async machinery needed.

**Alternatives considered**:
- *Before changelog send*: Risks FK join messages arriving before the changelog entry, causing inconsistency.
- *Override `ManagedUserDict.__setitem__`*: Fragile — depends on MRO ordering and external library internals.

### 3. Convert FK join handlers to synchronous using `send_soon()`

**Decision**: Convert `_on_left_table_change` and `_on_right_table_change` from `async` to synchronous methods. Replace `await self.subscription_topic.send(...)` with `self.subscription_topic.send_soon(...)` and `await self.response_topic.send(...)` with `self.response_topic.send_soon(...)`.

**Rationale**: `on_key_set` is synchronous (called from `ManagedUserDict.__setitem__`), so callbacks must also be synchronous. `send_soon()` is the established sync fire-and-forget pattern already used by `send_changelog()`. It buffers messages in the producer and flushes asynchronously — no `await` needed.

**Alternatives considered**:
- *`asyncio.create_task()` from sync context*: Adds concurrency concerns, reordering risk, and no backpressure.
- *Queue-based dispatch (on_key_set → queue → async consumer)*: Unnecessary complexity; `send_soon()` already provides buffered async delivery.

### 4. Register callbacks in `on_start()`, unregister in `on_stop()`

**Decision**: `FKJoinProcessor.on_start()` registers its change handlers on `self.left_table` and `self.right_table`. `on_stop()` unregisters them.

**Rationale**: The FK join processor is a `Service` with a managed lifecycle. Registering in `on_start()` ensures tables are initialized. Unregistering in `on_stop()` prevents stale callback references and allows clean shutdown.

### 5. No changes to recovery path

**Decision**: No modifications needed to `Store.apply_changelog_batch()` or recovery-related code.

**Rationale**: Recovery writes go through `Store._set()` directly, completely bypassing `ManagedUserDict` and therefore `on_key_set`. FK join callbacks will never fire during recovery. This was verified by tracing `apply_changelog_batch()` → `Store._set()` in `faust/stores/base.py`.

## Risks / Trade-offs

- **[Risk] Callback exceptions break the write path** → Mitigation: Wrap callback invocations in try/except with logging. A failing FK join callback must not prevent the table write from completing.
- **[Risk] `send_soon()` backpressure** → Mitigation: `send_soon()` is already used by `send_changelog()` for every table write. FK join adds at most one additional `send_soon()` per write. The producer buffer handles backpressure the same way it does for changelog messages.
- **[Risk] Callback registration timing** → Mitigation: `FKJoinProcessor.on_start()` runs after the app starts tables. If tables aren't started yet, the callback lists are still initialized in `Collection.__init__` — registration itself doesn't require a running table, only that the Table object exists.
- **[Trade-off] Synchronous callbacks add latency to writes** → The FK join `send_soon()` call is a buffer append (microseconds), matching the existing `send_changelog()` cost. Acceptable.
