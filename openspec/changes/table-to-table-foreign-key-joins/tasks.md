## 1. Types and Data Structures

- [x] 1.1 Add `JoinedValue` namedtuple to `faust/types/joins.py` with `left` and `right` fields
- [x] 1.2 Add `ForeignKeyJoinT` abstract class to `faust/types/joins.py` with `left_table`, `right_table`, `extractor`, and `inner` attributes
- [x] 1.3 Define `SubscriptionInstruction` enum in `faust/types/joins.py` with values `SUBSCRIBE_AND_RESPOND`, `SUBSCRIBE_ONLY`, `UNSUBSCRIBE_ONLY`
- [x] 1.4 Define `SubscriptionMessage` model (Record) with fields: `left_pk`, `hash` (bytes), `instruction` (SubscriptionInstruction)
- [x] 1.5 Define `ResponseMessage` model (Record) with fields: `right_value` (optional bytes/Any), `hash` (bytes)
- [x] 1.6 Export new types from `faust/types/__init__.py`

## 2. Internal Topic Creation

- [x] 2.1 Add helper method to create the subscription-registration internal topic using `Topic.derive()` — keyed by FK (right-table key space), co-partitioned with the right table's changelog topic
- [x] 2.2 Add helper method to create the subscription-response internal topic using `Topic.derive()` — keyed by left PK (left-table key space), co-partitioned with the left table's changelog topic
- [x] 2.3 Verify both topics are created with `internal=True` and appropriate cleanup policies (compaction + deletion)

## 3. Subscription Store

- [x] 3.1 Create an internal `Table` for the subscription store with composite keys `(FK, LeftPK)` and values containing `{hash: bytes}`
- [x] 3.2 Implement prefix-scan method on the subscription store — given a FK, return all entries `(FK, LeftPK) -> {hash}` matching that FK prefix
- [x] 3.3 Ensure the subscription store has a changelog topic for durability and recovery across restarts/rebalances

## 4. Hash Computation

- [x] 4.1 Implement hash function for left-table record values (128-bit hash, e.g., `hashlib.md5` of serialized value) for use in subscription messages and stale-response detection
- [x] 4.2 Ensure hash is deterministic — same value always produces the same hash regardless of serialization ordering

## 5. Left-Side Subscription Sender

- [x] 5.1 Implement left-table changelog processor that intercepts left-table updates (create/update/delete)
- [x] 5.2 On new record or FK change: extract FK via `extractor(value)`, compute hash, send `SubscriptionMessage(left_pk, hash, SUBSCRIBE_AND_RESPOND)` keyed by FK to subscription-registration topic
- [x] 5.3 On FK change (old FK differs from new FK): send `SubscriptionMessage(left_pk, instruction=UNSUBSCRIBE_ONLY)` keyed by old FK before sending subscription to new FK
- [x] 5.4 On record deletion: send `SubscriptionMessage(left_pk, instruction=UNSUBSCRIBE_ONLY)` keyed by previous FK
- [x] 5.5 Track the previous FK for each left-table key (in local state or by reading current value before update) to detect FK changes

## 6. Right-Side Subscription Processor

- [x] 6.1 Create a background agent that consumes from the subscription-registration topic
- [x] 6.2 On `SUBSCRIBE_AND_RESPOND`: store `(FK, LeftPK) -> {hash}` in subscription store, look up `right_table[FK]`, send `ResponseMessage(right_value, hash)` keyed by LeftPK to subscription-response topic
- [x] 6.3 On `UNSUBSCRIBE_ONLY`: remove `(FK, LeftPK)` from subscription store, do NOT send response
- [x] 6.4 On `SUBSCRIBE_ONLY`: store `(FK, LeftPK) -> {hash}` in subscription store, do NOT send response

## 7. Right-Table Update Propagation

- [x] 7.1 Implement right-table changelog processor that intercepts right-table updates (create/update/delete)
- [x] 7.2 On right-table update: prefix-scan subscription store for all entries with FK matching the updated key
- [x] 7.3 For each subscriber found: send `ResponseMessage(right_value=new_value, hash=stored_hash)` keyed by subscriber's LeftPK to subscription-response topic
- [x] 7.4 On right-table deletion: same as update but with `right_value=None`

## 8. Left-Side Response Handler

- [x] 8.1 Create a background agent that consumes from the subscription-response topic
- [x] 8.2 On response: look up current left record by the message key (LeftPK), compute hash of current left value, compare with response's hash
- [x] 8.3 If hash matches: emit `JoinedValue(left=left_table[LeftPK], right=response.right_value)` to the joined output stream/channel
- [x] 8.4 If hash does not match (stale response): discard silently — a fresh subscription is already in flight
- [x] 8.5 If left record no longer exists: discard the response
- [x] 8.6 For inner join: only emit if `right_value` is not None; for left join: emit with `right=None`

## 9. ForeignKeyJoin Strategy and Table Integration

- [x] 9.1 Add `ForeignKeyJoin(Join)` class to `faust/joins.py` that holds references to `left_table`, `right_table`, `extractor`, `inner`, and the internal topics/subscription store
- [x] 9.2 Implement `foreign_key_join(right_table, extractor, *, inner=True)` on `Collection` in `faust/tables/base.py` that wires up the full subscription/response protocol and returns a joined stream
- [x] 9.3 Wire up `contribute_to_stream()` and `remove_from_stream()` on `Collection` to support the joined stream lifecycle
- [x] 9.4 Add `foreign_key_join` to `CollectionT` abstract interface in `faust/types/tables.py`

## 10. Public API Exposure

- [x] 10.1 Export `JoinedValue`, `ForeignKeyJoin` from `faust/__init__.py`
- [x] 10.2 Export `SubscriptionInstruction` from `faust/__init__.py` (if user-visible) or keep internal

## 11. Tests

- [x] 11.1 Unit tests for hash computation — determinism, collision avoidance for distinct values
- [x] 11.2 Unit tests for subscription store — insert, delete, prefix scan with matching entries, prefix scan with no matches
- [x] 11.3 Unit tests for left-side subscription sender — new record sends SUBSCRIBE_AND_RESPOND, FK change sends UNSUBSCRIBE_ONLY + SUBSCRIBE_AND_RESPOND, deletion sends UNSUBSCRIBE_ONLY
- [x] 11.4 Unit tests for right-side subscription processor — SUBSCRIBE_AND_RESPOND stores and responds, UNSUBSCRIBE_ONLY removes without response, SUBSCRIBE_ONLY stores without response
- [x] 11.5 Unit tests for right-table update propagation — update sends responses to all subscribers, update with no subscribers sends nothing, deletion sends null responses
- [x] 11.6 Unit tests for left-side response handler — valid hash emits JoinedValue, stale hash discards, deleted left record discards, inner vs left join semantics
- [x] 11.7 Integration test — end-to-end FK join: left update triggers subscription → right side stores and responds → left side emits JoinedValue
- [x] 11.8 Integration test — right-table update propagation: right update triggers responses to all subscribers → each left side emits updated JoinedValue
- [x] 11.9 Integration test — race condition: rapid left updates produce stale responses that are correctly discarded
