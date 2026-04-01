## ADDED Requirements

### Requirement: Foreign key join method on Table
The `Collection` class SHALL expose a `foreign_key_join(right_table, extractor, *, inner=True)` method that sets up the subscription/response protocol and returns a `StreamT` whose events are `JoinedValue(left, right)` tuples representing the join result.

#### Scenario: Basic foreign key join setup
- **WHEN** `left_table.foreign_key_join(right_table, lambda v: v.merchant_id)` is called
- **THEN** the system SHALL create two internal topics (subscription-registration and subscription-response), create an internal subscription store on the right side, start background agents to process subscription and response messages, and return a joined stream

#### Scenario: Inner join mode (default)
- **WHEN** `foreign_key_join(right_table, extractor, inner=True)` is configured and a left-table record has an FK that maps to a right-table record
- **THEN** the joined stream SHALL emit `JoinedValue(left=<left_value>, right=<right_value>)`

#### Scenario: Inner join suppresses unmatched records
- **WHEN** `inner=True` and a left-table record has an FK that does not exist in the right table
- **THEN** the joined stream SHALL NOT emit a result for that record

#### Scenario: Left join mode emits None for unmatched records
- **WHEN** `foreign_key_join(right_table, extractor, inner=False)` is configured and a left-table record has an FK that does not exist in the right table
- **THEN** the joined stream SHALL emit `JoinedValue(left=<left_value>, right=None)`

### Requirement: Subscription message flow on left-table update
When a left-table record is created or updated, the system SHALL extract the FK from the new value, compute a hash of the left record, and send a subscription message keyed by the FK to the subscription-registration internal topic.

#### Scenario: New left-table record triggers subscription
- **WHEN** a new record with key `LK1` and value `{merchant_id: "M1", ...}` is written to the left table
- **THEN** the system SHALL send a subscription message to the subscription-registration topic with key `M1`, containing `{left_pk: "LK1", hash: hash(value), instruction: SUBSCRIBE_AND_RESPOND}`

#### Scenario: Left-table record FK change triggers unsubscribe and resubscribe
- **WHEN** a left-table record with key `LK1` changes its FK from `M1` to `M2`
- **THEN** the system SHALL send an unsubscribe message with key `M1` containing `{left_pk: "LK1", instruction: UNSUBSCRIBE_ONLY}` AND a subscribe message with key `M2` containing `{left_pk: "LK1", hash: hash(new_value), instruction: SUBSCRIBE_AND_RESPOND}`

#### Scenario: Left-table record deletion triggers unsubscribe
- **WHEN** a left-table record with key `LK1` (previously referencing FK `M1`) is deleted
- **THEN** the system SHALL send an unsubscribe message with key `M1` containing `{left_pk: "LK1", instruction: UNSUBSCRIBE_ONLY}`

### Requirement: Right-side subscription processing
The right-side subscription processor agent SHALL consume from the subscription-registration topic, maintain a subscription store with composite keys `(FK, LeftPK)`, and send response messages back via the subscription-response topic.

#### Scenario: SUBSCRIBE_AND_RESPOND stores subscription and sends response
- **WHEN** a subscription message with `{left_pk: "LK1", hash: H, instruction: SUBSCRIBE_AND_RESPOND}` arrives keyed by `M1`
- **THEN** the right-side agent SHALL store `(M1, LK1) -> {hash: H}` in the subscription store AND send a response message to the subscription-response topic keyed by `LK1` containing `{right_value: right_table[M1], hash: H}` (or `{right_value: None, hash: H}` if `M1` is not in the right table)

#### Scenario: UNSUBSCRIBE_ONLY removes subscription without response
- **WHEN** a subscription message with `{left_pk: "LK1", instruction: UNSUBSCRIBE_ONLY}` arrives keyed by `M1`
- **THEN** the right-side agent SHALL remove `(M1, LK1)` from the subscription store and SHALL NOT send a response

#### Scenario: SUBSCRIBE_ONLY stores subscription without response
- **WHEN** a subscription message with `{left_pk: "LK1", hash: H, instruction: SUBSCRIBE_ONLY}` arrives keyed by `M1`
- **THEN** the right-side agent SHALL store `(M1, LK1) -> {hash: H}` in the subscription store and SHALL NOT send a response

### Requirement: Right-table update propagation
When a right-table record is created or updated, the system SHALL prefix-scan the subscription store for all subscriptions referencing that right key and send a response message to each subscriber.

#### Scenario: Right-table update sends responses to all subscribers
- **WHEN** the right table record with key `M1` is updated to `new_value` and the subscription store contains entries `(M1, LK1) -> {hash: H1}` and `(M1, LK2) -> {hash: H2}`
- **THEN** the system SHALL send two response messages: one keyed by `LK1` with `{right_value: new_value, hash: H1}` and one keyed by `LK2` with `{right_value: new_value, hash: H2}`

#### Scenario: Right-table update with no subscribers sends no responses
- **WHEN** the right table record with key `M1` is updated and the subscription store contains no entries with FK prefix `M1`
- **THEN** the system SHALL NOT send any response messages

#### Scenario: Right-table record deletion sends null responses
- **WHEN** the right table record with key `M1` is deleted and there are active subscriptions for `M1`
- **THEN** the system SHALL send response messages to all subscribers with `{right_value: None, hash: <stored_hash>}`

### Requirement: Left-side response processing with hash validation
The left-side response processor SHALL consume from the subscription-response topic, validate the hash against the current left record, and emit the join result to the output stream only if the hash matches.

#### Scenario: Valid response produces join result
- **WHEN** a response message keyed by `LK1` arrives with `{right_value: V, hash: H}` and the current left record with key `LK1` has `hash(value) == H`
- **THEN** the system SHALL emit `JoinedValue(left=left_table[LK1], right=V)` to the joined output stream

#### Scenario: Stale response is discarded
- **WHEN** a response message keyed by `LK1` arrives with `{right_value: V, hash: H_old}` and the current left record with key `LK1` has `hash(value) != H_old` (the left record was updated since the subscription was sent)
- **THEN** the system SHALL discard the response and NOT emit a join result (a fresh subscription is already in flight)

#### Scenario: Response for deleted left record is discarded
- **WHEN** a response message keyed by `LK1` arrives but `LK1` no longer exists in the left table
- **THEN** the system SHALL discard the response

### Requirement: Internal topic creation
The system SHALL create two internal Kafka topics per FK join instance using `Topic.derive()` with `internal=True`.

#### Scenario: Subscription-registration topic partitioning
- **WHEN** a FK join is set up between `left_table` and `right_table`
- **THEN** the subscription-registration topic SHALL have the same number of partitions as the right table's changelog topic (so subscription messages keyed by FK are routed to the same partition as the right-table record they reference)

#### Scenario: Subscription-response topic partitioning
- **WHEN** a FK join is set up between `left_table` and `right_table`
- **THEN** the subscription-response topic SHALL have the same number of partitions as the left table's changelog topic (so response messages keyed by left PK are routed to the same partition as the left-table record they reference)

### Requirement: Subscription store lifecycle
The subscription store SHALL be a changelog-backed Faust table (internal, not user-visible) that persists subscriptions across restarts and rebalances.

#### Scenario: Subscription store survives restart
- **WHEN** a worker restarts and recovers from its subscription store's changelog topic
- **THEN** all previously registered subscriptions SHALL be restored, and no re-subscription is required from the left side

#### Scenario: Subscription store uses composite keys
- **WHEN** subscriptions are stored
- **THEN** each entry SHALL use a composite key `(FK, LeftPK)` enabling prefix-scan lookups by FK

### Requirement: JoinedValue result type
The system SHALL provide a `JoinedValue` type that holds the result of a foreign key join, exposing `left` (the source table value) and `right` (the foreign table value or `None`).

#### Scenario: JoinedValue provides typed access to both sides
- **WHEN** a foreign key join produces a result
- **THEN** the result SHALL be a `JoinedValue` with `.left` containing the left-table value and `.right` containing the right-table value

#### Scenario: JoinedValue is usable in downstream processing
- **WHEN** an agent iterates over a foreign-key-joined stream
- **THEN** each yielded value SHALL be a `JoinedValue` that can be destructured or accessed by attribute

### Requirement: Extractor callable flexibility
The `extractor` parameter to `foreign_key_join` SHALL accept any callable that takes the left table's value and returns the FK (right-table key) to join on.

#### Scenario: Lambda extractor
- **WHEN** `extractor=lambda v: v.merchant_id` is provided
- **THEN** the join SHALL use `value.merchant_id` as the FK for each left-table record

#### Scenario: Function extractor with transformation
- **WHEN** `extractor=lambda v: str(v.account_id).upper()` is provided
- **THEN** the join SHALL use the transformed value as the FK for each left-table record
