## Why

The "Foreign Key Join" name implies a relational-database concept of referential integrity, but this feature is a general-purpose table-to-table join driven by any key extractor function. The terminology misleads users into thinking a foreign-key constraint is required; in reality, any two tables can be joined based on any key. Renaming to "Key Join" better reflects the mechanism and broadens the conceptual appeal.

## What Changes

- Rename `ForeignKeyJoin` → `KeyJoin` in the public API (`faust.joins`, `faust.__init__`)
- Rename `ForeignKeyJoinT` → `KeyJoinT` in `faust.types.joins` and `faust.types.__init__`
- Rename `ForeignKeyJoinProcessor` → `KeyJoinProcessor` in `faust/tables/fkjoin.py`; rename the file to `faust/tables/keyjoin.py`
- Rename the `.foreign_key_join()` method on `CollectionT` / `Collection` to `.key_join()`
- Update `JoinedValue` docstring and `SubscriptionInstruction` docstring to remove FK references
- Rename the example file `examples/foreign_key_join.py` → `examples/key_join.py` and update all internal references
- Rename the test file `t/unit/tables/test_fkjoin.py` → `t/unit/tables/test_keyjoin.py` and update all internal references
- Update openspec change documents (`table-to-table-foreign-key-joins`, `add-table-write-hooks-for-fk-joins`, `fix-fkjoin-subscriptions`) to reflect new naming
- Add backward-compatible aliases (`ForeignKeyJoin`, `ForeignKeyJoinT`, `foreign_key_join`) with deprecation warnings so existing user code is not broken immediately **[soft-BREAKING: aliases deprecated, removed in next major]**

## Capabilities

### New Capabilities

- `key-join-public-api`: Public API symbols (`KeyJoin`, `KeyJoinT`, `KeyJoinProcessor`, `Collection.key_join()`) and backward-compat aliases with deprecation warnings.

### Modified Capabilities

<!-- No existing openspec specs change behavioral requirements; this is a pure rename. -->

## Impact

- **Public API**: `faust.ForeignKeyJoin`, `faust.types.ForeignKeyJoinT`, `Collection.foreign_key_join()` are deprecated (aliases remain until next major version).
- **Files renamed**: `faust/tables/fkjoin.py` → `faust/tables/keyjoin.py`, `examples/foreign_key_join.py` → `examples/key_join.py`, `t/unit/tables/test_fkjoin.py` → `t/unit/tables/test_keyjoin.py`.
- **No behavioral change**: The subscription/response protocol and all runtime semantics are unchanged.
- **Dependencies**: No new dependencies required.
