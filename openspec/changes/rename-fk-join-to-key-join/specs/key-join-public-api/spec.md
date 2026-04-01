## ADDED Requirements

### Requirement: KeyJoin public API symbols
The system SHALL expose `KeyJoin`, `KeyJoinT`, and `KeyJoinProcessor` as the canonical public names for the table-to-table join feature.

#### Scenario: Import KeyJoin from faust top-level
- **WHEN** a user imports `from faust import KeyJoin`
- **THEN** the import SHALL succeed and return the `KeyJoin` class

#### Scenario: Import KeyJoinT from faust.types
- **WHEN** a user imports `from faust.types import KeyJoinT`
- **THEN** the import SHALL succeed and return the `KeyJoinT` abstract class

#### Scenario: Import KeyJoinProcessor from faust.tables.keyjoin
- **WHEN** a user imports `from faust.tables.keyjoin import KeyJoinProcessor`
- **THEN** the import SHALL succeed and return the `KeyJoinProcessor` class

### Requirement: key_join method on Collection
The `Collection` class (and its `CollectionT` abstract type) SHALL expose a `key_join(right_table, extractor, *, inner=True)` method that creates and registers a `KeyJoinProcessor` and returns the output channel.

#### Scenario: Call key_join on a table
- **WHEN** a user calls `left_table.key_join(right_table, extractor=lambda v: v.fk_id)`
- **THEN** it SHALL return a channel that emits `JoinedValue(left, right)` on changes to either table

#### Scenario: key_join registers processor as dependency
- **WHEN** `key_join` is called
- **THEN** the resulting `KeyJoinProcessor` SHALL be added to the table's beacon and dependency tree so it starts and stops with the table

### Requirement: Backward-compatible deprecated aliases
The system SHALL retain the old `ForeignKeyJoin`, `ForeignKeyJoinT`, and `foreign_key_join` names as deprecated aliases so that existing user code continues to function without modification until the next major version.

#### Scenario: Import ForeignKeyJoin (deprecated)
- **WHEN** a user imports `from faust import ForeignKeyJoin`
- **THEN** the import SHALL succeed AND emit a `DeprecationWarning` indicating `ForeignKeyJoin` is deprecated in favour of `KeyJoin`

#### Scenario: Call foreign_key_join (deprecated)
- **WHEN** a user calls `left_table.foreign_key_join(right_table, extractor=...)`
- **THEN** the call SHALL succeed, emit a `DeprecationWarning`, and behave identically to `key_join`

#### Scenario: Import from deprecated fkjoin module
- **WHEN** a user imports `from faust.tables.fkjoin import ForeignKeyJoinProcessor`
- **THEN** the import SHALL succeed AND emit a `DeprecationWarning` indicating the module has been renamed to `faust.tables.keyjoin`

### Requirement: Implementation file rename
The implementation SHALL reside in `faust/tables/keyjoin.py`. The old `faust/tables/fkjoin.py` SHALL be retained as a shim module that re-exports all symbols from `keyjoin.py` with a deprecation warning.

#### Scenario: fkjoin shim import triggers warning
- **WHEN** `faust.tables.fkjoin` is imported for the first time
- **THEN** Python SHALL emit a `DeprecationWarning` with a message referencing `faust.tables.keyjoin`

#### Scenario: fkjoin shim symbols are identical
- **WHEN** `from faust.tables.fkjoin import KeyJoinProcessor` (via alias)
- **THEN** the returned object SHALL be identical (`is`) to `faust.tables.keyjoin.KeyJoinProcessor`

## REMOVED Requirements

### Requirement: ForeignKeyJoinProcessor as primary implementation class
**Reason**: Replaced by `KeyJoinProcessor` in `faust/tables/keyjoin.py`. The `ForeignKeyJoinProcessor` name no longer represents the canonical class.
**Migration**: Replace `ForeignKeyJoinProcessor` with `KeyJoinProcessor` and update import paths from `faust.tables.fkjoin` to `faust.tables.keyjoin`.
