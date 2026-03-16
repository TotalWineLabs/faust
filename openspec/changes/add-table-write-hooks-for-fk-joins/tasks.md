## 1. Callback Infrastructure on Collection

- [x] 1.1 Add `_on_key_set_callbacks` and `_on_key_del_callbacks` list attributes to `Collection.__init__` in `faust/tables/base.py`
- [x] 1.2 Add `register_on_key_set(callback)` and `unregister_on_key_set(callback)` methods to `Collection` in `faust/tables/base.py`
- [x] 1.3 Add `register_on_key_del(callback)` and `unregister_on_key_del(callback)` methods to `Collection` in `faust/tables/base.py`

## 2. Invoke Callbacks from Table

- [x] 2.1 Extend `Table.on_key_set` in `faust/tables/table.py` to iterate and invoke `_on_key_set_callbacks` after changelog send and sensor dispatch, with try/except + logging for each callback
- [x] 2.2 Extend `Table.on_key_del` in `faust/tables/table.py` to iterate and invoke `_on_key_del_callbacks` after changelog send and sensor dispatch, with try/except + logging for each callback

## 3. Convert FK Join Handlers to Synchronous

- [x] 3.1 Convert `_on_left_table_change` in `faust/tables/fkjoin.py` from async to sync, replacing `await self._send_subscription(...)` with synchronous `send_soon()` calls
- [x] 3.2 Convert `_send_subscription` in `faust/tables/fkjoin.py` from async to sync, replacing `await self.subscription_topic.send(...)` with `self.subscription_topic.send_soon(...)`
- [x] 3.3 Convert `_on_right_table_change` in `faust/tables/fkjoin.py` from async to sync, replacing `await self.response_topic.send(...)` with `self.response_topic.send_soon(...)`

## 4. Wire FK Join to Tables

- [x] 4.1 In `FKJoinProcessor.on_start()`, register `_on_left_key_set` and `_on_left_key_del` callbacks on `self.left_table`
- [x] 4.2 In `FKJoinProcessor.on_start()`, register `_on_right_key_set` and `_on_right_key_del` callbacks on `self.right_table`
- [x] 4.3 Add thin callback wrappers (`_on_left_key_set`, `_on_left_key_del`, `_on_right_key_set`, `_on_right_key_del`) that adapt the `(key, value)` / `(key,)` signatures to the FK join handler signatures

## 5. Unregister on Stop

- [x] 5.1 Add `on_stop()` to `FKJoinProcessor` that unregisters all four callbacks from `self.left_table` and `self.right_table`

## 6. Tests — Table Write Callbacks

- [x] 6.1 Test that `register_on_key_set` adds callback and it fires on `on_key_set`
- [x] 6.2 Test that `unregister_on_key_set` removes callback and it no longer fires
- [x] 6.3 Test that `unregister_on_key_set` with unregistered callback is a no-op
- [x] 6.4 Test that `register_on_key_del` adds callback and it fires on `on_key_del`
- [x] 6.5 Test that `unregister_on_key_del` removes callback and it no longer fires
- [x] 6.6 Test that a callback exception is logged and does not prevent the write from completing
- [x] 6.7 Test that subsequent callbacks still fire after a prior callback raises an exception
- [x] 6.8 Test that callbacks are initialized empty on new table

## 7. Tests — FK Join Event Wiring

- [x] 7.1 Test that `on_start()` registers callbacks on left and right tables
- [x] 7.2 Test that `on_stop()` unregisters callbacks from left and right tables
- [x] 7.3 Test that left table write triggers synchronous `_on_left_table_change` with correct arguments
- [x] 7.4 Test that left table delete triggers `_on_left_table_change` with `is_delete=True`
- [x] 7.5 Test that right table write triggers synchronous `_on_right_table_change` with correct arguments
- [x] 7.6 Test that right table delete triggers `_on_right_table_change` with `is_delete=True`
- [x] 7.7 Test that `_send_subscription` uses `send_soon()` instead of `await send()`
- [x] 7.8 Test that `_on_right_table_change` uses `send_soon()` instead of `await send()`
