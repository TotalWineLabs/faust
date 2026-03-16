## ADDED Requirements

### Requirement: FK join processor registers callbacks on left table
The `FKJoinProcessor` SHALL register a key-set callback and a key-del callback on `self.left_table` during `on_start()`.

#### Scenario: Left table write triggers subscription message
- **WHEN** a key-value pair is written to the left table after the FK join processor has started
- **THEN** the FK join processor SHALL send a subscription message to the subscription topic via `send_soon()`

#### Scenario: Left table delete triggers unsubscribe message
- **WHEN** a key is deleted from the left table after the FK join processor has started
- **THEN** the FK join processor SHALL send an unsubscribe message to the subscription topic via `send_soon()`

### Requirement: FK join processor registers callbacks on right table
The `FKJoinProcessor` SHALL register a key-set callback and a key-del callback on `self.right_table` during `on_start()`.

#### Scenario: Right table write triggers response messages
- **WHEN** a key-value pair is written to the right table after the FK join processor has started
- **THEN** the FK join processor SHALL send response messages to all subscribers of that key via `send_soon()`

#### Scenario: Right table delete triggers response messages with None
- **WHEN** a key is deleted from the right table after the FK join processor has started
- **THEN** the FK join processor SHALL send response messages with `right_value=None` to all subscribers of that key via `send_soon()`

### Requirement: FK join change handlers are synchronous
The FK join table-change handlers (`_on_left_table_change`, `_on_right_table_change`) SHALL be synchronous methods that use `send_soon()` instead of `await send()`.

#### Scenario: Left change handler uses send_soon
- **WHEN** `_on_left_table_change` is invoked
- **THEN** it SHALL call `self.subscription_topic.send_soon(...)` instead of `await self.subscription_topic.send(...)`

#### Scenario: Right change handler uses send_soon
- **WHEN** `_on_right_table_change` is invoked
- **THEN** it SHALL call `self.response_topic.send_soon(...)` instead of `await self.response_topic.send(...)`

### Requirement: FK join processor unregisters callbacks on stop
The `FKJoinProcessor` SHALL unregister all its callbacks from `self.left_table` and `self.right_table` during `on_stop()`.

#### Scenario: Callbacks are removed on processor stop
- **WHEN** the FK join processor is stopped via `on_stop()`
- **THEN** the left and right tables SHALL no longer have the FK join processor's callbacks registered

#### Scenario: Table writes after processor stop do not trigger FK join logic
- **WHEN** a table is written to after the FK join processor has stopped
- **THEN** no FK join subscription or response messages SHALL be sent

### Requirement: FK left table change handles FK migration
When the left table value changes and the extracted FK differs from the previously stored FK, the handler SHALL unsubscribe from the old FK before subscribing to the new FK.

#### Scenario: FK changes from A to B
- **WHEN** a left table write changes the extracted FK from A to B
- **THEN** the handler SHALL send an UNSUBSCRIBE_ONLY for FK=A and a SUBSCRIBE_AND_RESPOND for FK=B

#### Scenario: FK stays the same
- **WHEN** a left table write is made but the extracted FK is the same as the previous FK
- **THEN** the handler SHALL send only a SUBSCRIBE_AND_RESPOND (re-subscribe) for the same FK

### Requirement: Internal send methods use send_soon
The internal `_send_subscription` method SHALL use `send_soon()` for synchronous, fire-and-forget message delivery.

#### Scenario: Subscription message sent via send_soon
- **WHEN** `_send_subscription` is called
- **THEN** it SHALL call `self.subscription_topic.send_soon(key=fk, value=msg)` synchronously
