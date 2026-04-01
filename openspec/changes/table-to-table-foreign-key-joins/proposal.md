## Why

Faust tables currently have no way to express relationships between each other. When processing stream events that reference entities in other tables (e.g., an order referencing a customer by ID), users must manually look up related table entries, handle missing keys, and stitch values together. The existing join infrastructure (`Join`, `RightJoin`, `LeftJoin`, etc.) is scaffolded but entirely unimplemented — every `process()` and `_join()` method raises `NotImplementedError`. This forces users into verbose, error-prone boilerplate for a fundamental data-enrichment pattern.

A correct key join between two differently-keyed, differently-partitioned tables is a fundamentally distributed problem. Simple local point lookups (requiring co-partitioning) are insufficient because the left table and right table have different keys and may have different partition counts — a product keyed by `ProductID` cannot be co-partitioned with a merchant keyed by `MerchantID`. The naive workaround of using a `GlobalTable` (broadcast join) does not scale to large right-side tables, and re-keying via `group_by` + primary-key join creates unbounded aggregate records and excessive recomputation.

Kafka Streams solved this in KIP-213 with a subscription/response message-passing protocol that avoids copying either table, handles differently partitioned inputs, and only recomputes the join results that actually changed. Faust should adopt the same proven algorithm.

> **Note (2026-03-17):** The feature was initially shipped under the "Foreign Key Join" name (`ForeignKeyJoin`, `foreign_key_join`, `ForeignKeyJoinProcessor`). It has since been renamed to "Key Join" (`KeyJoin`, `key_join`, `KeyJoinProcessor`) to better reflect that the join works on any key extractor, not just relational foreign keys. Deprecated aliases remain until the next major release. See change `rename-fk-join-to-key-join`.

## What Changes

- Add two internal repartition topics per key join: a **subscription-registration topic** (keyed by extracted key / right-table key) and a **subscription-response topic** (keyed by left-table primary key)
- Add a **subscription store** on the right-side task using composite keys `(FK, left-PK)` to track which left records are interested in which right records
- Implement a `key_join()` method on `Collection`/`Table` that wires the subscription/response data flow
- When a left-table record changes: extract the key, send a subscription message (containing a hash of the left value) to the subscription topic, receive a response with the right-side value, and compute the join locally
- When a right-table record changes: prefix-scan the subscription store for all interested left records, send response messages back to each
- Handle race conditions by including a hash of the left value in subscription messages, echoing it back in responses, and discarding stale responses where the hash no longer matches
- Implement `KeyJoin` strategy class that orchestrates the above protocol
- Wire up `contribute_to_stream()` and `remove_from_stream()` on `Collection` to support the joined stream lifecycle

## Capabilities

### New Capabilities
- `table-foreign-key-join`: Ability to perform distributed key joins between two differently-keyed, differently-partitioned tables using a subscription/response protocol, without requiring co-partitioning or full replication of either table

### Modified Capabilities

_(none — no existing spec-level requirements are changing)_

## Impact

- **Code**: `faust/joins.py` (new `KeyJoin` strategy), `faust/tables/base.py` (new `key_join` method, subscription store management, `contribute_to_stream`/`remove_from_stream`), `faust/tables/table.py`, `faust/types/joins.py` and `faust/types/tables.py` (new type definitions), `faust/channels.py` / `faust/topics.py` (internal topic creation for subscription and response channels)
- **APIs**: New public API surface — `Table.key_join()`, `KeyJoin`; no breaking changes to existing APIs
- **Dependencies**: None — uses only existing Faust internals (table stores, changelog topics, stream combinators, `through()`/`group_by()` repartitioning, internal topic derivation)
- **Systems**: Two new internal Kafka topics per key join instance (subscription-registration, subscription-response); one new state store (subscription store) per key join on the right-side task; no co-partitioning requirement between left and right tables
