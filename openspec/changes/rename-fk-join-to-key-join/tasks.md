## 1. Rename Core Implementation File

- [ ] 1.1 Rename `faust/tables/fkjoin.py` → `faust/tables/keyjoin.py` (`git mv`)
- [ ] 1.2 In `keyjoin.py`, rename class `ForeignKeyJoinProcessor` → `KeyJoinProcessor` and update `__all__`
- [ ] 1.3 In `keyjoin.py`, update the module docstring to say "Key Join" instead of "Foreign Key Join"
- [ ] 1.4 Create `faust/tables/fkjoin.py` as a deprecation shim: emit `DeprecationWarning` on import and re-export all symbols from `keyjoin.py`

## 2. Update Type Definitions

- [ ] 2.1 In `faust/types/joins.py`, rename `ForeignKeyJoinT` → `KeyJoinT` and update `__all__`
- [ ] 2.2 In `faust/types/joins.py`, add `ForeignKeyJoinT = KeyJoinT` alias after the class definition
- [ ] 2.3 Update the `JoinedValue` docstring to remove "foreign key" phrasing
- [ ] 2.4 Update `SubscriptionInstruction` docstring to remove "FK join" phrasing
- [ ] 2.5 In `faust/types/__init__.py`, add `KeyJoinT` to the import and `__all__`; keep `ForeignKeyJoinT` alias

## 3. Update CollectionT Abstract Type

- [ ] 3.1 In `faust/types/tables.py`, rename the abstract method `foreign_key_join` → `key_join`
- [ ] 3.2 Update the method docstring to use "key join" terminology

## 4. Update Collection Implementation

- [ ] 4.1 In `faust/tables/base.py`, rename the `foreign_key_join` method → `key_join`
- [ ] 4.2 Update `key_join` to import from `faust.tables.keyjoin` (not `fkjoin`)
- [ ] 4.3 Add a `foreign_key_join` method that emits `DeprecationWarning` and delegates to `key_join`

## 5. Update joins.py Public Module

- [ ] 5.1 In `faust/joins.py`, rename `ForeignKeyJoin` → `KeyJoin` and update `__all__`
- [ ] 5.2 Add `ForeignKeyJoin` alias with `DeprecationWarning` inside a compatibility wrapper

## 6. Update Top-Level faust Package

- [ ] 6.1 In `faust/__init__.py`, add `KeyJoin` to the import list and `__all__`
- [ ] 6.2 Keep `ForeignKeyJoin` in `__all__` as a deprecated re-export (points to `KeyJoin`)

## 7. Rename Example File

- [ ] 7.1 Rename `examples/foreign_key_join.py` → `examples/key_join.py` (`git mv`)
- [ ] 7.2 Update all `foreign_key_join` / `ForeignKeyJoin` references inside `key_join.py` to use new names
- [ ] 7.3 Update the module docstring title and narrative to say "Key Join"
- [ ] 7.4 Rename the app id `faust-fk-join-example` → `faust-key-join-example`

## 8. Rename Test File

- [ ] 8.1 Rename `t/unit/tables/test_fkjoin.py` → `t/unit/tables/test_keyjoin.py` (`git mv`)
- [ ] 8.2 Update the module docstring in `test_keyjoin.py`
- [ ] 8.3 Replace all imports of `ForeignKeyJoinProcessor` from `faust.tables.fkjoin` with `KeyJoinProcessor` from `faust.tables.keyjoin`
- [ ] 8.4 Replace all usages of `ForeignKeyJoinProcessor` in test code with `KeyJoinProcessor`
- [ ] 8.5 Add test cases verifying that the deprecated aliases (`ForeignKeyJoin`, `ForeignKeyJoinProcessor`, `foreign_key_join`, shim module) emit `DeprecationWarning`

## 9. Update t/unit/tables/test_table.py

- [ ] 9.1 Replace any `foreign_key_join` method call references with `key_join`
- [ ] 9.2 Replace any `ForeignKeyJoin` or `ForeignKeyJoinProcessor` imports with new names

## 10. Update OpenSpec Change Documents

- [ ] 10.1 Update `openspec/changes/table-to-table-foreign-key-joins/` docs to note the rename (add a note to `proposal.md` and `design.md`)
- [ ] 10.2 Update `openspec/changes/add-table-write-hooks-for-fk-joins/` docs similarly
- [ ] 10.3 Update `openspec/changes/fix-fkjoin-subscriptions/proposal.md` similarly

## 11. Update README and Other Docs

- [ ] 11.1 In `README.rst`, replace any "foreign key join" / `foreign_key_join` references with "key join" / `key_join`
- [ ] 11.2 Scan `docs/` for any references to `foreign_key_join` or `ForeignKeyJoin` and update them

## 12. Verification

- [ ] 12.1 Run the full test suite (`pytest t/unit/tables/test_keyjoin.py t/unit/tables/test_table.py`) and confirm all tests pass
- [ ] 12.2 Confirm `DeprecationWarning` tests pass for all deprecated aliases
- [ ] 12.3 Run `python -c "from faust import KeyJoin, ForeignKeyJoin; print('aliases ok')"` and verify output and warnings
- [ ] 12.4 Run `python -c "import faust.tables.fkjoin"` and verify it imports without error but with a `DeprecationWarning`
