## ADDED Requirements

### Requirement: Table supports registering key-set callbacks
The `Collection` base class SHALL maintain a list of callbacks that are invoked whenever `on_key_set` is called on the table. Callbacks SHALL be called with `(key, value)` arguments.

#### Scenario: Register a key-set callback
- **WHEN** a callback is registered via `table.register_on_key_set(callback)`
- **THEN** the callback is added to the table's internal key-set callback list

#### Scenario: Key-set callback is invoked on table write
- **WHEN** a value is written to the table via `table[key] = value`
- **THEN** all registered key-set callbacks SHALL be called with `(key, value)`

#### Scenario: Multiple key-set callbacks are invoked in order
- **WHEN** two callbacks A and B are registered (A first, then B) and a key is set
- **THEN** callback A SHALL be invoked before callback B

#### Scenario: Key-set callbacks fire after changelog send
- **WHEN** a value is written to the table
- **THEN** the changelog entry SHALL be sent before any registered key-set callbacks are invoked

### Requirement: Table supports unregistering key-set callbacks
The `Collection` base class SHALL allow removal of previously registered key-set callbacks.

#### Scenario: Unregister a key-set callback
- **WHEN** a previously registered callback is unregistered via `table.unregister_on_key_set(callback)`
- **THEN** the callback SHALL no longer be invoked on subsequent table writes

#### Scenario: Unregister a callback that was never registered
- **WHEN** `unregister_on_key_set` is called with a callback that was never registered
- **THEN** the operation SHALL be a no-op (no error raised)

### Requirement: Table supports registering key-del callbacks
The `Collection` base class SHALL maintain a list of callbacks that are invoked whenever `on_key_del` is called on the table. Callbacks SHALL be called with `(key,)` argument.

#### Scenario: Register a key-del callback
- **WHEN** a callback is registered via `table.register_on_key_del(callback)`
- **THEN** the callback is added to the table's internal key-del callback list

#### Scenario: Key-del callback is invoked on table delete
- **WHEN** a key is deleted from the table via `del table[key]`
- **THEN** all registered key-del callbacks SHALL be called with `(key,)`

#### Scenario: Key-del callbacks fire after changelog send
- **WHEN** a key is deleted from the table
- **THEN** the changelog tombstone SHALL be sent before any registered key-del callbacks are invoked

### Requirement: Table supports unregistering key-del callbacks
The `Collection` base class SHALL allow removal of previously registered key-del callbacks.

#### Scenario: Unregister a key-del callback
- **WHEN** a previously registered callback is unregistered via `table.unregister_on_key_del(callback)`
- **THEN** the callback SHALL no longer be invoked on subsequent table deletes

### Requirement: Callback exceptions do not break table writes
Exceptions raised by registered callbacks SHALL NOT prevent the table write or delete operation from completing.

#### Scenario: Callback raises an exception during key-set
- **WHEN** a registered key-set callback raises an exception during a table write
- **THEN** the table write SHALL complete successfully and the exception SHALL be logged

#### Scenario: Callback raises an exception during key-del
- **WHEN** a registered key-del callback raises an exception during a table delete
- **THEN** the table delete SHALL complete successfully and the exception SHALL be logged

#### Scenario: Subsequent callbacks still fire after a prior callback fails
- **WHEN** callback A raises an exception and callback B is also registered
- **THEN** callback B SHALL still be invoked

### Requirement: Recovery writes do not trigger callbacks
Writes applied via the recovery path (`Store.apply_changelog_batch`) SHALL NOT invoke registered key-set or key-del callbacks.

#### Scenario: Recovery batch application does not fire callbacks
- **WHEN** `apply_changelog_batch` is called during recovery
- **THEN** no registered key-set or key-del callbacks SHALL be invoked

### Requirement: Callback lists are initialized empty
When a table is created, the key-set and key-del callback lists SHALL be empty.

#### Scenario: New table has no callbacks
- **WHEN** a new Table is instantiated
- **THEN** `_on_key_set_callbacks` and `_on_key_del_callbacks` SHALL be empty lists
