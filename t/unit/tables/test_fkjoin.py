"""Tests for faust.tables.fkjoin — Foreign Key Join Processor."""
import os

import faust
import pytest
from faust.transport.producer import Producer
from faust.utils.tracing import set_current_span
from mode.utils.mocks import AsyncMock, Mock

from faust.joins import ResponseMessage, SubscriptionMessage
from faust.tables.fkjoin import ForeignKeyJoinProcessor
from faust.types.joins import (
    JoinedValue,
    SubscriptionInstruction,
)


@pytest.fixture()
def app(event_loop):
    os.environ.pop('F_DATADIR', None)
    os.environ.pop('FAUST_DATADIR', None)
    os.environ.pop('F_WORKDIR', None)
    os.environ.pop('FAUST_WORKDIR', None)
    instance = faust.App('testid')
    instance.producer = Mock(
        name='producer',
        autospec=Producer,
        maybe_start=AsyncMock(),
        start=AsyncMock(),
        send=AsyncMock(),
        send_and_wait=AsyncMock(),
    )
    instance.finalize()
    set_current_span(None)
    return instance


def _make_processor(app, *, inner=True):
    """Create a ForeignKeyJoinProcessor with mock tables."""
    left_table = Mock(name='left_table')
    left_table.name = 'orders'
    left_table.key_type = str
    left_table.key_serializer = 'raw'
    left_table.partitions = 8

    right_table = Mock(name='right_table')
    right_table.name = 'products'
    right_table.key_type = str
    right_table.key_serializer = 'raw'
    right_table.partitions = 8

    extractor = Mock(name='extractor', side_effect=lambda v: v.get('product_id'))

    processor = ForeignKeyJoinProcessor(
        app=app,
        left_table=left_table,
        right_table=right_table,
        extractor=extractor,
        inner=inner,
    )
    return processor


class _MockStore(dict):
    """Dict subclass with prefix_scan support for testing."""

    def prefix_scan(self, prefix):
        for key, value in self.items():
            if str(key).startswith(prefix):
                yield key, value


class TestHashComputation:
    """11.1 — hash computation determinism and collision avoidance."""

    def test_same_value_same_hash(self):
        val = {'name': 'widget', 'price': 9.99}
        h1 = ForeignKeyJoinProcessor.compute_hash(val)
        h2 = ForeignKeyJoinProcessor.compute_hash(val)
        assert h1 == h2

    def test_hash_is_16_bytes(self):
        h = ForeignKeyJoinProcessor.compute_hash({'a': 1})
        assert len(h) == 16

    def test_different_values_different_hashes(self):
        h1 = ForeignKeyJoinProcessor.compute_hash({'a': 1})
        h2 = ForeignKeyJoinProcessor.compute_hash({'a': 2})
        assert h1 != h2

    def test_key_order_irrelevant(self):
        """sort_keys=True ensures deterministic serialization."""
        h1 = ForeignKeyJoinProcessor.compute_hash({'b': 2, 'a': 1})
        h2 = ForeignKeyJoinProcessor.compute_hash({'a': 1, 'b': 2})
        assert h1 == h2

    def test_none_returns_empty_bytes(self):
        assert ForeignKeyJoinProcessor.compute_hash(None) == b''

    def test_bytes_input(self):
        h = ForeignKeyJoinProcessor.compute_hash(b'hello')
        assert len(h) == 16

    def test_string_input(self):
        h = ForeignKeyJoinProcessor.compute_hash('hello')
        assert len(h) == 16


class TestSubscriptionStore:
    """11.2 — subscription store insert, delete, prefix scan."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        # Use a mock store with prefix_scan support for testing
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        return proc

    def test_insert(self, processor):
        key = 'fk1\x00lpk1'
        processor.subscription_store[key] = {'hash': b'\x01' * 16}
        assert key in processor.subscription_store

    def test_delete(self, processor):
        key = 'fk1\x00lpk1'
        processor.subscription_store[key] = {'hash': b'\x01' * 16}
        processor.subscription_store.pop(key, None)
        assert key not in processor.subscription_store

    def test_prefix_scan_matching(self, processor):
        processor._subscription_store = _MockStore({
            'fk1\x00lpk1': {'hash': b'\x01' * 16},
            'fk1\x00lpk2': {'hash': b'\x02' * 16},
            'fk2\x00lpk3': {'hash': b'\x03' * 16},
        })
        results = processor.prefix_scan('fk1')
        assert len(results) == 2
        left_pks = {r[0] for r in results}
        assert left_pks == {'lpk1', 'lpk2'}

    def test_prefix_scan_no_matches(self, processor):
        processor._subscription_store = _MockStore({
            'fk1\x00lpk1': {'hash': b'\x01' * 16},
        })
        results = processor.prefix_scan('fk_missing')
        assert results == []


class TestLeftSideSubscriptionSender:
    """11.3 — left-side subscription sender."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        proc._subscription_topic = Mock(
            name='subscription_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        proc._response_topic = Mock(
            name='response_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        return proc

    def test_new_record_sends_subscribe_and_respond(self, processor):
        value = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', value)

        processor.subscription_topic.send_soon.assert_called_once()
        call_kwargs = processor.subscription_topic.send_soon.call_args[1]
        assert call_kwargs['key'] == 'prod1'
        msg = call_kwargs['value']
        assert msg.left_pk == 'order1'
        assert msg.instruction == SubscriptionInstruction.SUBSCRIBE_AND_RESPOND.value

    def test_fk_change_unsubscribes_old_subscribes_new(self, processor):
        # First insert
        value1 = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', value1)
        processor.subscription_topic.send_soon.reset_mock()

        # FK change
        processor.previous_fk_store['order1'] = 'prod1'
        value2 = {'product_id': 'prod2', 'qty': 3}
        processor._on_left_table_change('order1', value2)

        assert processor.subscription_topic.send_soon.call_count == 2
        calls = processor.subscription_topic.send_soon.call_args_list

        # First call: UNSUBSCRIBE from old FK
        unsub_kwargs = calls[0][1]
        assert unsub_kwargs['key'] == 'prod1'
        assert unsub_kwargs['value'].instruction == SubscriptionInstruction.UNSUBSCRIBE_ONLY.value

        # Second call: SUBSCRIBE_AND_RESPOND to new FK
        sub_kwargs = calls[1][1]
        assert sub_kwargs['key'] == 'prod2'
        assert sub_kwargs['value'].instruction == SubscriptionInstruction.SUBSCRIBE_AND_RESPOND.value

    def test_deletion_sends_unsubscribe_only(self, processor):
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change('order1', None, is_delete=True)

        processor.subscription_topic.send_soon.assert_called_once()
        call_kwargs = processor.subscription_topic.send_soon.call_args[1]
        assert call_kwargs['key'] == 'prod1'
        assert call_kwargs['value'].instruction == SubscriptionInstruction.UNSUBSCRIBE_ONLY.value


class TestRightSideSubscriptionProcessor:
    """11.4 — right-side subscription processor."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        proc._response_topic = Mock(
            name='response_topic',
            send=AsyncMock(),
        )
        proc.right_table = Mock(name='right_table')
        proc.right_table.name = 'products'
        proc.right_table.get = Mock(return_value={'name': 'Widget'})
        return proc

    @pytest.mark.asyncio
    async def test_subscribe_and_respond_stores_and_sends(self, processor):
        msg = SubscriptionMessage(
            left_pk='order1',
            hash=b'\x01' * 16,
            instruction=SubscriptionInstruction.SUBSCRIBE_AND_RESPOND.value,
        )
        await processor._process_subscription('prod1', msg)

        # Check store
        assert 'prod1\x00order1' in processor.subscription_store
        assert processor.subscription_store['prod1\x00order1']['hash'] == b'\x01' * 16

        # Check response sent
        processor.response_topic.send.assert_called_once()
        resp_kwargs = processor.response_topic.send.call_args[1]
        assert resp_kwargs['key'] == 'order1'
        assert resp_kwargs['value'].right_value == {'name': 'Widget'}

    @pytest.mark.asyncio
    async def test_unsubscribe_only_removes_without_response(self, processor):
        processor.subscription_store['prod1\x00order1'] = {'hash': b'\x01' * 16}

        msg = SubscriptionMessage(
            left_pk='order1',
            hash=b'',
            instruction=SubscriptionInstruction.UNSUBSCRIBE_ONLY.value,
        )
        await processor._process_subscription('prod1', msg)

        assert 'prod1\x00order1' not in processor.subscription_store
        processor.response_topic.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribe_only_stores_without_response(self, processor):
        msg = SubscriptionMessage(
            left_pk='order1',
            hash=b'\x01' * 16,
            instruction=SubscriptionInstruction.SUBSCRIBE_ONLY.value,
        )
        await processor._process_subscription('prod1', msg)

        assert 'prod1\x00order1' in processor.subscription_store
        processor.response_topic.send.assert_not_called()


class TestRightTableUpdatePropagation:
    """11.5 — right-table update propagation."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        proc._subscription_store = _MockStore({
            'prod1\x00order1': {'hash': b'\x01' * 16},
            'prod1\x00order2': {'hash': b'\x02' * 16},
        })
        proc._previous_fk = {}
        proc._response_topic = Mock(
            name='response_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        return proc

    def test_update_sends_responses_to_all_subscribers(self, processor):
        processor._on_right_table_change('prod1', {'name': 'Updated Widget'})

        assert processor.response_topic.send_soon.call_count == 2
        calls = processor.response_topic.send_soon.call_args_list
        keys = {c[1]['key'] for c in calls}
        assert keys == {'order1', 'order2'}
        for c in calls:
            assert c[1]['value'].right_value == {'name': 'Updated Widget'}

    def test_update_no_subscribers_sends_nothing(self, processor):
        processor._subscription_store = _MockStore()
        processor._on_right_table_change('prod_no_subs', {'name': 'Widget'})
        processor.response_topic.send_soon.assert_not_called()

    def test_deletion_sends_null_responses(self, processor):
        processor._on_right_table_change('prod1', None, is_delete=True)

        assert processor.response_topic.send_soon.call_count == 2
        for c in processor.response_topic.send_soon.call_args_list:
            assert c[1]['value'].right_value is None


class TestLeftSideResponseHandler:
    """11.6 — left-side response handler."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app, inner=True)
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        proc._output_channel = Mock(
            name='output_channel',
            send=AsyncMock(),
        )
        return proc

    @pytest.mark.asyncio
    async def test_valid_hash_emits_joined_value(self, processor):
        left_value = {'product_id': 'prod1', 'qty': 5}
        expected_hash = ForeignKeyJoinProcessor.compute_hash(left_value)
        processor.left_table.get = Mock(return_value=left_value)

        msg = ResponseMessage(right_value={'name': 'Widget'}, hash=expected_hash)
        await processor._process_response('order1', msg)

        processor._output_channel.send.assert_called_once()
        joined = processor._output_channel.send.call_args[1]['value']
        assert isinstance(joined, JoinedValue)
        assert joined.left == left_value
        assert joined.right == {'name': 'Widget'}

    @pytest.mark.asyncio
    async def test_stale_hash_discards(self, processor):
        left_value = {'product_id': 'prod1', 'qty': 5}
        processor.left_table.get = Mock(return_value=left_value)

        msg = ResponseMessage(right_value={'name': 'Widget'}, hash=b'\xff' * 16)
        await processor._process_response('order1', msg)

        processor._output_channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_deleted_left_record_discards(self, processor):
        processor.left_table.get = Mock(return_value=None)

        msg = ResponseMessage(right_value={'name': 'Widget'}, hash=b'\x01' * 16)
        await processor._process_response('order1', msg)

        processor._output_channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_inner_join_skips_null_right(self, processor):
        left_value = {'product_id': 'prod1', 'qty': 5}
        expected_hash = ForeignKeyJoinProcessor.compute_hash(left_value)
        processor.left_table.get = Mock(return_value=left_value)

        msg = ResponseMessage(right_value=None, hash=expected_hash)
        await processor._process_response('order1', msg)

        processor._output_channel.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_left_join_emits_null_right(self, processor):
        processor.inner = False
        left_value = {'product_id': 'prod1', 'qty': 5}
        expected_hash = ForeignKeyJoinProcessor.compute_hash(left_value)
        processor.left_table.get = Mock(return_value=left_value)

        msg = ResponseMessage(right_value=None, hash=expected_hash)
        await processor._process_response('order1', msg)

        processor._output_channel.send.assert_called_once()
        joined = processor._output_channel.send.call_args[1]['value']
        assert joined.left == left_value
        assert joined.right is None


class TestForeignKeyJoinIntegration:
    """11.7–11.9 — integration tests for end-to-end flows."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        proc._subscription_topic = Mock(
            name='subscription_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        proc._response_topic = Mock(
            name='response_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        proc._output_channel = Mock(
            name='output_channel',
            send=AsyncMock(),
        )
        proc.right_table.get = Mock(return_value={'name': 'Widget', 'price': 9.99})
        return proc

    @pytest.mark.asyncio
    async def test_end_to_end_left_triggers_join(self, processor):
        """11.7 — left update → subscription → right responds → join emitted."""
        left_value = {'product_id': 'prod1', 'qty': 5}
        left_hash = ForeignKeyJoinProcessor.compute_hash(left_value)

        # Step 1: Left-table change triggers subscription (sync)
        processor._on_left_table_change('order1', left_value)

        sub_call = processor.subscription_topic.send_soon.call_args[1]
        assert sub_call['key'] == 'prod1'
        sub_msg = sub_call['value']

        # Step 2: Right-side processes subscription (async)
        await processor._process_subscription('prod1', sub_msg)

        # Verify subscription stored
        assert 'prod1\x00order1' in processor.subscription_store

        # Step 3: Response was sent — simulate left side receiving it
        resp_call = processor.response_topic.send.call_args[1]
        resp_msg = resp_call['value']

        processor.left_table.get = Mock(return_value=left_value)
        await processor._process_response('order1', resp_msg)

        # Verify joined value emitted
        processor._output_channel.send.assert_called_once()
        joined = processor._output_channel.send.call_args[1]['value']
        assert joined.left == left_value
        assert joined.right == {'name': 'Widget', 'price': 9.99}

    @pytest.mark.asyncio
    async def test_right_update_propagates_to_subscribers(self, processor):
        """11.8 — right update triggers responses to all subscribers."""
        # Set up two subscribers
        left_val1 = {'product_id': 'prod1', 'qty': 5}
        left_val2 = {'product_id': 'prod1', 'qty': 3}
        hash1 = ForeignKeyJoinProcessor.compute_hash(left_val1)
        hash2 = ForeignKeyJoinProcessor.compute_hash(left_val2)

        processor.subscription_store['prod1\x00order1'] = {'hash': hash1}
        processor.subscription_store['prod1\x00order2'] = {'hash': hash2}

        # Right-table update (sync)
        new_right = {'name': 'Updated Widget', 'price': 12.99}
        processor._on_right_table_change('prod1', new_right)

        assert processor.response_topic.send_soon.call_count == 2

        # Process each response on left side
        def get_left(pk):
            return left_val1 if pk == 'order1' else left_val2

        processor.left_table.get = Mock(side_effect=get_left)

        for c in processor.response_topic.send_soon.call_args_list:
            left_pk = c[1]['key']
            resp_msg = c[1]['value']
            await processor._process_response(left_pk, resp_msg)

        assert processor._output_channel.send.call_count == 2

    @pytest.mark.asyncio
    async def test_rapid_updates_stale_responses_discarded(self, processor):
        """11.9 — rapid left updates produce stale responses that are discarded."""
        # First update (sync)
        val1 = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', val1)
        sub_call_1 = processor.subscription_topic.send_soon.call_args[1]
        sub_msg_1 = sub_call_1['value']

        # Process subscription for first update (async)
        await processor._process_subscription('prod1', sub_msg_1)
        resp_call_1 = processor.response_topic.send.call_args[1]
        resp_msg_1 = resp_call_1['value']

        # Second update (rapid) — changes the left value
        processor.subscription_topic.send_soon.reset_mock()
        processor.response_topic.send.reset_mock()
        val2 = {'product_id': 'prod1', 'qty': 10}
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change('order1', val2)

        # The left record is now val2, so the response from val1 is stale
        processor.left_table.get = Mock(return_value=val2)
        await processor._process_response('order1', resp_msg_1)

        # Stale — should NOT emit
        processor._output_channel.send.assert_not_called()


class TestFKJoinEventWiring:
    """Group 7 — FK join callback registration / wiring tests."""

    @pytest.fixture
    def processor(self, app):
        proc = _make_processor(app)
        proc._subscription_store = _MockStore()
        proc._previous_fk = {}
        proc._subscription_topic = Mock(
            name='subscription_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        proc._response_topic = Mock(
            name='response_topic',
            send=AsyncMock(),
            send_soon=Mock(),
        )
        proc._output_channel = Mock(
            name='output_channel',
            send=AsyncMock(),
        )
        return proc

    # 7.1 — on_start registers callbacks on left and right tables
    @pytest.mark.asyncio
    async def test_on_start_registers_callbacks(self, processor):
        processor.app.channel = Mock(name='channel')
        processor.add_future = Mock(name='add_future')

        await processor.on_start()

        processor.left_table.register_on_key_set.assert_called_once_with(
            processor._on_left_key_set,
        )
        processor.left_table.register_on_key_del.assert_called_once_with(
            processor._on_left_key_del,
        )
        processor.right_table.register_on_key_set.assert_called_once_with(
            processor._on_right_key_set,
        )
        processor.right_table.register_on_key_del.assert_called_once_with(
            processor._on_right_key_del,
        )

    # 7.2 — on_stop unregisters callbacks from both tables
    @pytest.mark.asyncio
    async def test_on_stop_unregisters_callbacks(self, processor):
        await processor.on_stop()

        processor.left_table.unregister_on_key_set.assert_called_once_with(
            processor._on_left_key_set,
        )
        processor.left_table.unregister_on_key_del.assert_called_once_with(
            processor._on_left_key_del,
        )
        processor.right_table.unregister_on_key_set.assert_called_once_with(
            processor._on_right_key_set,
        )
        processor.right_table.unregister_on_key_del.assert_called_once_with(
            processor._on_right_key_del,
        )

    # 7.3 — left table write triggers _on_left_table_change with correct args
    def test_left_key_set_delegates_to_on_left_table_change(self, processor):
        processor._on_left_table_change = Mock(name='_on_left_table_change')
        processor._on_left_key_set('order1', {'product_id': 'prod1'})
        processor._on_left_table_change.assert_called_once_with(
            'order1', {'product_id': 'prod1'},
        )

    # 7.4 — left table delete triggers _on_left_table_change with is_delete
    def test_left_key_del_delegates_with_is_delete(self, processor):
        processor._on_left_table_change = Mock(name='_on_left_table_change')
        processor._on_left_key_del('order1')
        processor._on_left_table_change.assert_called_once_with(
            'order1', None, is_delete=True,
        )

    # 7.5 — right table write triggers _on_right_table_change with correct args
    def test_right_key_set_delegates_to_on_right_table_change(self, processor):
        processor._on_right_table_change = Mock(name='_on_right_table_change')
        processor._on_right_key_set('prod1', {'name': 'Widget'})
        processor._on_right_table_change.assert_called_once_with(
            'prod1', {'name': 'Widget'},
        )

    # 7.6 — right table delete triggers _on_right_table_change with is_delete
    def test_right_key_del_delegates_with_is_delete(self, processor):
        processor._on_right_table_change = Mock(name='_on_right_table_change')
        processor._on_right_key_del('prod1')
        processor._on_right_table_change.assert_called_once_with(
            'prod1', None, is_delete=True,
        )

    # 7.7 — _send_subscription uses send_soon() not await send()
    def test_send_subscription_uses_send_soon(self, processor):
        processor._send_subscription(
            'prod1', 'order1', b'\x01' * 16,
            SubscriptionInstruction.SUBSCRIBE_AND_RESPOND,
        )
        processor.subscription_topic.send_soon.assert_called_once()
        processor.subscription_topic.send.assert_not_called()

    # 7.8 — _on_right_table_change uses send_soon() not await send()
    def test_on_right_table_change_uses_send_soon(self, processor):
        processor._subscription_store = _MockStore({
            'prod1\x00order1': {'hash': b'\x01' * 16},
        })
        processor._on_right_table_change('prod1', {'name': 'Widget'})
        processor.response_topic.send_soon.assert_called_once()
        processor.response_topic.send.assert_not_called()
