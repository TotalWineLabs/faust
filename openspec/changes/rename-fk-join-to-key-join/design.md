## Context

The `fk-join` branch introduced a table-to-table join feature using a subscription/response protocol (inspired by Kafka Streams KIP-213). All public symbols and internal identifiers were named with "foreign key" / "FK" terminology: `ForeignKeyJoin`, `ForeignKeyJoinT`, `ForeignKeyJoinProcessor`, `Collection.foreign_key_join()`, `faust/tables/fkjoin.py`.

The feature is not constrained to foreign-key relationships — any two Faust tables can be joined using any key extractor function. The FK terminology misleads users and is narrower than the actual semantics. This design covers the rename to "Key Join" across all layers while preserving backward compatibility.

## Goals / Non-Goals

**Goals:**
- Rename all public API symbols from `ForeignKey*` to `Key*` and `foreign_key_join` to `key_join`.
- Rename source files: `fkjoin.py` → `keyjoin.py`, `foreign_key_join.py` → `key_join.py`, `test_fkjoin.py` → `test_keyjoin.py`.
- Provide backward-compatible deprecated aliases for all renamed public symbols so existing user code continues to work until the next major version.
- Update all docstrings, comments, and openspec change documents to use the new naming.

**Non-Goals:**
- No changes to the subscription/response protocol, wire format, or runtime behavior.
- No removal of deprecated aliases in this change (that is a separate major-version task).
- No changes to internal topic naming conventions (e.g., `-subscription-registration`, `-subscription-response`) to avoid data migration for existing deployments.

## Decisions

### 1. Keep `fkjoin.py` as a shim rather than deleting it

**Decision**: Rename the implementation file to `keyjoin.py` and keep `fkjoin.py` as a one-line re-export shim with a deprecation warning on import.

**Rationale**: Users or internal code that imports directly from `faust.tables.fkjoin` (an undocumented but reachable path) will get a clear `DeprecationWarning` rather than an `ImportError`. This is safer than a hard break.

**Alternative considered**: Hard-delete `fkjoin.py` and update all imports. Rejected because it's a breaking change with no warning.

### 2. Emit `DeprecationWarning` from aliases, not `PendingDeprecationWarning`

**Decision**: Use the standard `warnings.warn(..., DeprecationWarning, stacklevel=2)` pattern inside each alias.

**Rationale**: `DeprecationWarning` is visible by default in test suites (pytest surfaces it), ensuring users notice the rename promptly without being silent.

### 3. Single rename pass — no intermediate compatibility layer in types

**Decision**: In `faust/types/joins.py`, rename `ForeignKeyJoinT` → `KeyJoinT` directly and add `ForeignKeyJoinT = KeyJoinT` alias at module level (no class hierarchy change).

**Rationale**: The abstract type is purely structural — it carries no runtime behavior. A simple alias at module level is the least invasive approach and requires no changes to `isinstance` checks.

### 4. Do not rename internal Kafka topic suffixes

**Decision**: The internal topic names (e.g., `…-subscription-registration`, `…-subscription-response`) are not changed.

**Rationale**: Renaming topics would require a data migration plan for existing deployments. The topic names are implementation details not exposed in the public API.

## Risks / Trade-offs

- **[Risk] Import path breakage** → Mitigated by keeping `faust/tables/fkjoin.py` as a shim that re-exports from `keyjoin.py`.
- **[Risk] Users who pin the `ForeignKeyJoin` symbol in `__all__` re-exports** → Mitigated by leaving the alias in `faust.__init__` and `faust.joins.__all__` under the old name (deprecated).
- **[Risk] Test coverage gap during rename** → Mitigated by renaming the test file and updating all class/function references within it before merging.
- **[Trade-off] Two names exist simultaneously during the deprecation window** → Accepted; the deprecation period makes the transition gradual and user-friendly.

## Migration Plan

1. Rename `faust/tables/fkjoin.py` → `faust/tables/keyjoin.py`; leave `fkjoin.py` as an import shim.
2. Rename `ForeignKeyJoinProcessor` → `KeyJoinProcessor` in `keyjoin.py`.
3. In `faust/joins.py`: rename `ForeignKeyJoin` → `KeyJoin`; add `ForeignKeyJoin` alias with deprecation.
4. In `faust/types/joins.py`: rename `ForeignKeyJoinT` → `KeyJoinT`; add `ForeignKeyJoinT` alias.
5. In `faust/types/tables.py`: rename `foreign_key_join` abstract method → `key_join`.
6. In `faust/tables/base.py`: rename `foreign_key_join` implementation → `key_join`; add `foreign_key_join` alias with deprecation.
7. Update `faust/__init__.py` and `faust/types/__init__.py` to export new names + deprecated aliases.
8. Rename `examples/foreign_key_join.py` → `examples/key_join.py` and update all internal references.
9. Rename `t/unit/tables/test_fkjoin.py` → `t/unit/tables/test_keyjoin.py` and update all internal references.
10. Update openspec change docs and README references.

**Rollback**: Because no wire protocol or topic naming changes, rollback is simply reverting the source rename commits.

## Open Questions

- None outstanding; all decisions above are confirmed.
