## Context

Faust tables are changelog-backed key/value stores that run inside stream-processing agents. Today, each table is an isolated key-space: there is no declarative way to join two tables by a foreign key relationship. Users who need enrichment (e.g., look up merchant details when processing a product) resort to manual `merchants_table[product.merchant_id]` calls scattered throughout agent code, with ad-hoc error handling for missing keys and no reactivity when the referenced table changes.

The join infrastructure (`Join`, `RightJoin`, `LeftJoin`, `InnerJoin`, `OuterJoin` in `faust/joins.py`) exists as a class hierarchy but every `process()` method raises `NotImplementedError`. The `Collection._join()` and `Collection.combine()` methods are similarly stubbed. Streams have a working `_join` that clones with a strategy, and an `on_merge` hook, but the strategies themselves do nothing.

The fundamental challenge of a foreign key join between two tables is that they have **different keys and are partitioned independently**. A naïve co-partitioned point lookup is insufficient — products keyed by `ProductID` and merchants keyed by `MerchantID` are distributed across partitions by their respective keys, so a product and the merchant it references will generally live on different partitions (and different worker instances). `GlobalTable` (full replication) solves this at small scale but does not scale to large right-side tables.

Kafka Streams solved this problem correctly in KIP-213 with a **subscription/response message-passing protocol**. The algorithm uses two internal repartition topics and a subscription store to coordinate between differently-partitioned left and right table tasks — without copying either table and without requiring co-partitioning. Faust already has the building blocks for this: `stream.through()` and `stream.group_by()` create internal repartition topics, `Topic.derive()` creates derived internal topics, and tables provide persistent key-value state stores.

## Goals / Non-Goals

**Goals:**
- Implement distributed foreign key joins between two differently-keyed, differently-partitioned tables using a subscription/response protocol
- Create internal repartition topics (subscription-registration, subscription-response) to coordinate between left and right table tasks
- Maintain a subscription store on the right side that tracks which left records are interested in which right records
- Correctly propagate join updates in both directions: left-table changes and right-table changes both produce updated join results
- Handle race conditions with hash-based versioning to discard stale responses
- Support inner and left join semantics
- Provide a `Table.foreign_key_join()` method that wires the protocol and returns a joined stream

**Non-Goals:**
- Implementing the existing `RightJoin`/`LeftJoin`/`InnerJoin`/`OuterJoin` stream-to-stream join strategies (separate effort)
- Supporting multi-column or composite foreign keys
- Providing SQL-like join syntax or a query language
- Windowed foreign key joins (FK lookups return the current value, not windowed history)
- Right join or outer join semantics (results use left-side keys; right/outer require emitting on right-side keys, deferred to future work)

## Decisions

### 1. Subscription/response message-passing protocol (not local point lookups)

**Decision**: Implement the Kafka Streams KIP-213 algorithm adapted for Faust. When a left-table record is updated, extract its FK and send a **subscription message** (keyed by the FK) to a subscription-registration internal topic. The right-side task that owns that FK partition stores the subscription in a composite-keyed subscription store `(FK, LeftPK)`, then sends a **response message** (keyed by the left PK) containing the current right-side value back via a subscription-response internal topic. The left-side task receives the response and computes the join result locally with its own left record.

When a right-table record is updated, the right-side task prefix-scans the subscription store for all entries matching that right key, and sends a response to each subscriber. Each left-side task then recomputes the join result.

**Rationale**: This is the only correct approach for distributed FK joins without co-partitioning or full replication. Point lookups against a non-global table fail because the referenced record is generally on a different partition/worker. The subscription/response protocol routes messages to the correct partitions using Kafka's own partitioning, and only small subscription/response messages flow — no full data copying.

**Alternative considered**: Co-partitioning enforcement with local point lookups. Rejected because two tables with different key spaces cannot be meaningfully co-partitioned — requiring the same partition count is necessary but not sufficient; the partitioning function maps different keys to different partitions regardless of count.

### 2. Two internal repartition topics per FK join

**Decision**: Create two internal topics per FK join instance using `Topic.derive()`:
- `{left_table}-{right_table}-subscription-registration` — keyed by the FK (right-table key), carries subscription messages from left-side tasks to right-side tasks
- `{left_table}-{right_table}-subscription-response` — keyed by the left PK (left-table key), carries response messages from right-side tasks back to left-side tasks

Both topics are internal (`internal=True`), use the same partitioning as their key's respective table, and use compaction + deletion cleanup.

**Rationale**: Mirrors the Kafka Streams approach. Using `Topic.derive()` and Faust's existing internal topic machinery ensures correct partition counts and lifecycle management. The subscription topic must be co-partitioned with the right table (same key space), and the response topic must be co-partitioned with the left table (same key space).

### 3. Subscription store with composite keys

**Decision**: Create a Faust `Table` (internal, not user-facing) on the right-side task to store subscriptions. Keys are composite `(FK, LeftPK)` tuples. Values contain a 128-bit hash of the left record at subscription time plus an instruction field.

**Rationale**: Composite keys enable efficient prefix scanning — when a right-table record changes, the right-side task needs to find all left records that reference it. A composite key `(FK, LeftPK)` allows prefix-scanning by FK to find all subscribers. Faust tables backed by RocksDB support ordered key iteration which enables prefix scanning; in-memory dict tables can use linear scan filtered by FK prefix.

**Alternative considered**: A separate subscription store implementation outside of Faust's Table abstraction. Rejected because reusing Table provides changelog-backed durability, recovery, and standby replication for free.

### 4. Hash-based race condition handling

**Decision**: Include a 128-bit hash (using `hashlib.md5` or similar fast hash) of the left record's serialized value in every subscription message. The right-side task stores this hash in the subscription store and echoes it back in every response message. When the left-side task receives a response, it compares the echoed hash against the hash of its current left record. If they differ, the response is stale (the left record changed since the subscription was sent) and is discarded — a fresh subscription will already be in flight.

**Rationale**: Without hash-based validation, race conditions arise when a left record is updated rapidly. The old subscription may arrive at the right side after a newer subscription; the response to the old subscription would carry incorrect data. The hash acts as a version check without requiring a full versioning system. This matches the Kafka Streams approach exactly.

### 5. Instruction field for subscription lifecycle optimization

**Decision**: Subscription messages carry an instruction field with three possible values:
- `SUBSCRIBE_AND_RESPOND`: Normal subscription — store the subscription and send a response with the current right value (used for new FK or FK change)
- `SUBSCRIBE_ONLY`: Store the subscription but do not send a response (used during rebalance recovery)
- `UNSUBSCRIBE_ONLY`: Remove the subscription from the store, do not send a response (used when FK changes — first unsubscribe from old FK, then subscribe to new FK)

**Rationale**: When a left record's FK changes from `A` to `B`, the left side must unsubscribe from `A` and subscribe to `B`. Without the instruction field, the unsubscription from `A` would trigger an unnecessary response. The optimization reduces traffic and avoids spurious join recomputations.

### 6. Join via explicit method with extractor callable

**Decision**: Implement `Table.foreign_key_join(right_table, extractor, *, inner=True)` where `extractor` is a callable `(value) -> key` that extracts the foreign key from the left table's value. Returns a joined stream that emits `(left_value, right_value)` tuples (or filters out unmatched records in inner mode).

**Rationale**: A method-based approach requires no changes to the model metaclass machinery and mirrors the existing `join()`/`left_join()` pattern. The extractor callable is flexible (supports computed keys, nested fields, type conversions) without coupling to model internals. `inner=True` default is safest (no `None` surprises); `inner=False` provides left-join semantics.

### 7. Merge strategy — JoinedValue namedtuple

**Decision**: The joined result is a `JoinedValue` namedtuple with `(left, right)` fields. For inner joins, both are always non-None. For left joins, `right` may be `None` when no match exists.

**Alternative considered**: Mutating the source Record to inject foreign fields. Rejected because it violates the immutability semantics of Records and creates naming collisions.

## Risks / Trade-offs

- **[Two extra topics per join]** → Each FK join creates two internal Kafka topics and a subscription store. This is inherent to the algorithm and cannot be avoided without co-partitioning (which is impossible) or GlobalTable (which doesn't scale). The topics carry only small subscription/response messages, not full records, so storage overhead is modest.
- **[Subscription store memory]** → The subscription store holds one entry per unique `(FK, LeftPK)` pair, where the value is just a hash + instruction (~20 bytes). For a left table with N records, the subscription store has at most N entries. This is comparable to the left table's own size in key count, but each entry is very small.
- **[Prefix scan performance]** → When a right-table record changes, we prefix-scan the subscription store for all subscribers of that FK. With RocksDB, this is an efficient ordered-range scan. With in-memory dict stores, this requires filtering all entries — acceptable for development but should use sorted data structures for production scale.
- **[Latency]** → The subscription/response protocol adds latency compared to a local point lookup: a left-table update must round-trip through the subscription topic to the right side and back through the response topic before the join result is emitted. This is the cost of correctness in a distributed system. The latency is bounded by Kafka produce/consume latency for the two hops.
- **[Race conditions]** → Hash-based validation handles the common case of rapid left-record updates. Edge cases during rebalancing require careful handling: subscriptions in flight when a partition is reassigned may arrive at a new right-side task that lacks the subscription store state. Recovery from changelog topics resolves this, but there is a window during rebalance where responses may be delayed.
- **[Breaking the existing stub]** → `Collection._join()` currently raises `NotImplementedError`. The FK join is implemented as a separate `foreign_key_join()` method and a new `ForeignKeyJoin` strategy, avoiding disturbing the existing `_join` stub until stream-to-stream joins are ready.
