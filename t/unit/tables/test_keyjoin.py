"""Tests for faust.tables.keyjoin — Key Join Processor."""
import asyncio

import pytest
from mode.utils.mocks import AsyncMock, Mock

from faust.tables.keyjoin import KeyJoinProcessor
from faust.types.joins import JoinedValue, SubscriptionInstruction

from t.helpers import new_event

SUBSCRIBE_AND_RESPOND = SubscriptionInstruction.SUBSCRIBE_AND_RESPOND.value
SUBSCRIBE_ONLY = SubscriptionInstruction.SUBSCRIBE_ONLY.value
UNSUBSCRIBE_ONLY = SubscriptionInstruction.UNSUBSCRIBE_ONLY.value


def _make_processor(app, *, inner=True):
    """Create a KeyJoinProcessor with mock tables."""
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

    extractor = Mock(
        name='extractor', side_effect=lambda v: v.get('product_id'))

    return KeyJoinProcessor(
        app=app,
        left_table=left_table,
        right_table=right_table,
        extractor=extractor,
        inner=inner,
    )


class _MockStore(dict):
    """Dict subclass with prefix_scan support for testing."""

    def prefix_scan(self, prefix):
        for key, value in list(self.items()):
            if str(key).startswith(prefix):
                yield key, value


def _with_topics(proc):
    proc._subscription_store = _MockStore()
    proc._previous_fk = {}
    proc._subscription_topic = Mock(
        name='subscription_topic', send=AsyncMock(), send_soon=Mock())
    proc._response_topic = Mock(
        name='response_topic', send=AsyncMock(), send_soon=Mock())
    # The output channel is real: an agent reading it is its subscriber.
    proc.subscriber = proc._output_channel.clone(is_iterator=True)
    proc.app.flow_control.resume()
    return proc


@pytest.fixture()
def processor(app):
    return _with_topics(_make_processor(app))


def _subscription_calls(proc):
    return [c.kwargs for c in proc.subscription_topic.send_soon.call_args_list]


def _response_event(app, left_pk, message, offset=1):
    """A response as the response stream hands it over: one reference."""
    event = new_event(app, key=left_pk, value=message,
                      topic='responses', offset=offset)
    event.message.incref()
    return event


async def _respond(proc, left_pk, message, offset=1):
    """Hand ``proc`` a response; return the response's event."""
    event = _response_event(proc.app, left_pk, message, offset=offset)
    await proc._process_response(left_pk, message, event)
    return event


def _emitted(proc, subscriber=None):
    """The events the output channel delivered to a subscriber."""
    queue = (subscriber or proc.subscriber).queue
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _results(proc):
    return [(e.key, e.value) for e in _emitted(proc)]


class test_compute_hash:

    def test_same_value_same_hash(self):
        value = {'name': 'widget', 'price': 9.99}
        assert (KeyJoinProcessor.compute_hash(value) ==
                KeyJoinProcessor.compute_hash(value))

    def test_different_values_different_hashes(self):
        assert (KeyJoinProcessor.compute_hash({'a': 1}) !=
                KeyJoinProcessor.compute_hash({'a': 2}))

    def test_none_is_zero(self):
        assert KeyJoinProcessor.compute_hash(None) == 0

    def test_text_and_its_bytes_hash_alike(self):
        assert (KeyJoinProcessor.compute_hash('hello') ==
                KeyJoinProcessor.compute_hash(b'hello'))


class test_subscription_store:

    def test_prefix_scan_yields_left_keys_of_one_foreign_key(self, processor):
        processor._subscription_store = _MockStore({
            'fk1\x00lpk1': {'hash': 1},
            'fk1\x00lpk2': {'hash': 2},
            'fk2\x00lpk3': {'hash': 3},
        })
        found = dict(processor.prefix_scan('fk1'))
        assert found == {'lpk1': {'hash': 1}, 'lpk2': {'hash': 2}}

    def test_prefix_scan_no_matches(self, processor):
        processor._subscription_store = _MockStore({'fk1\x00lpk1': {}})
        assert list(processor.prefix_scan('fk_missing')) == []


class test_left_table_change:

    def test_new_record_subscribes_and_asks_for_a_response(self, processor):
        value = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', value)

        [sent] = _subscription_calls(processor)
        assert sent['key'] == 'prod1'
        assert sent['value'] == {
            'left_pk': 'order1',
            'hash': KeyJoinProcessor.compute_hash(value),
            'instruction': SUBSCRIBE_AND_RESPOND,
        }
        assert processor.previous_fk_store['order1'] == 'prod1'

    def test_same_foreign_key_resubscribes_only(self, processor):
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change(
            'order1', {'product_id': 'prod1', 'qty': 6})

        [sent] = _subscription_calls(processor)
        assert sent['key'] == 'prod1'
        assert sent['value']['instruction'] == SUBSCRIBE_AND_RESPOND

    def test_foreign_key_change_unsubscribes_old_subscribes_new(
            self, processor):
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change(
            'order1', {'product_id': 'prod2', 'qty': 3})

        unsubscribe, subscribe = _subscription_calls(processor)
        assert unsubscribe['key'] == 'prod1'
        assert unsubscribe['value']['instruction'] == UNSUBSCRIBE_ONLY
        assert subscribe['key'] == 'prod2'
        assert subscribe['value']['instruction'] == SUBSCRIBE_AND_RESPOND
        assert processor.previous_fk_store['order1'] == 'prod2'

    def test_deletion_unsubscribes_and_forgets(self, processor):
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change('order1', None, is_delete=True)

        [sent] = _subscription_calls(processor)
        assert sent['key'] == 'prod1'
        assert sent['value']['instruction'] == UNSUBSCRIBE_ONLY
        assert 'order1' not in processor.previous_fk_store

    def test_deletion_of_unknown_record_sends_nothing(self, processor):
        processor._on_left_table_change('order1', None, is_delete=True)
        processor.subscription_topic.send_soon.assert_not_called()

    def test_missing_foreign_key_sends_nothing(self, processor):
        # A keyless record on the compacted subscription topic is refused
        # by the broker, together with the batch it shares.
        processor._on_left_table_change('order1', {'product_id': None})

        processor.subscription_topic.send_soon.assert_not_called()
        assert 'order1' not in processor.previous_fk_store

    def test_foreign_key_removed_unsubscribes_and_forgets(self, processor):
        processor.previous_fk_store['order1'] = 'prod1'
        processor._on_left_table_change('order1', {'product_id': None})

        [sent] = _subscription_calls(processor)
        assert sent['key'] == 'prod1'
        assert sent['value']['instruction'] == UNSUBSCRIBE_ONLY
        assert 'order1' not in processor.previous_fk_store

    def test_foreign_key_restored_subscribes_again(self, processor):
        processor._on_left_table_change('order1', {'product_id': None})
        processor._on_left_table_change('order1', {'product_id': 'prod1'})

        [sent] = _subscription_calls(processor)
        assert sent['key'] == 'prod1'
        assert sent['value']['instruction'] == SUBSCRIBE_AND_RESPOND
        assert processor.previous_fk_store['order1'] == 'prod1'


class test_partitioning:
    """The producer partitions every send by the key's bytes.

    Those are the bytes the table stores the key under, so a subscription
    lands on the partition of the right table that holds its foreign key,
    and a response on the partition of the left table that holds its
    record, with nothing asked of the record being processed.
    """

    @pytest.mark.parametrize('key_type,key', [
        (None, 'prod1'),
        (None, 42),
        (bytes, b'prod1'),
    ])
    def test_subscription_key_is_the_right_table_key(
            self, app, key_type, key):
        left = app.Table('orders', partitions=4)
        right = app.Table('products', partitions=4, key_type=key_type)
        join = KeyJoinProcessor(app, left, right, extractor=lambda v: v)

        sent, _ = join.subscription_topic.prepare_key(key, None)
        assert sent == app.serializers.dumps_key(
            right.key_type, key, serializer=right.key_serializer)

    @pytest.mark.parametrize('key_type,key', [
        (None, 'order1'),
        (None, 7),
        (bytes, b'order1'),
    ])
    def test_response_key_is_the_left_table_key(self, app, key_type, key):
        left = app.Table('orders', partitions=4, key_type=key_type)
        right = app.Table('products', partitions=4)
        join = KeyJoinProcessor(app, left, right, extractor=lambda v: v)

        sent, _ = join.response_topic.prepare_key(key, None)
        assert sent == app.serializers.dumps_key(
            left.key_type, key, serializer=left.key_serializer)

    def test_internal_topics_have_their_tables_partitions(self, app):
        left = app.Table('orders', partitions=6)
        right = app.Table('products', partitions=4)
        join = KeyJoinProcessor(app, left, right, extractor=lambda v: v)

        assert join.subscription_topic.partitions == 4
        assert join.response_topic.partitions == 6

    @pytest.mark.asyncio
    async def test_sends_name_no_partition(self, app, processor):
        processor._subscription_store = _MockStore({'prod1\x00order1': {}})
        processor.right_table.get = Mock(return_value={'name': 'Widget'})

        processor._on_left_table_change('order2', {'product_id': 'prod1'})
        await processor._process_subscription('prod1', {
            'left_pk': 'order2', 'hash': 1,
            'instruction': SUBSCRIBE_AND_RESPOND})
        processor._on_right_table_change('prod1', {'name': 'Widget'})

        for send in (processor.subscription_topic.send_soon,
                     processor.response_topic.send,
                     processor.response_topic.send_soon):
            assert send.call_args_list
            for call in send.call_args_list:
                assert set(call.kwargs) == {'key', 'value'}
        app.producer.key_partition.assert_not_called()


class test_raw_keys:
    """A table with raw keys holds bytes; the join's messages hold text."""

    def test_subscription_carries_the_left_key_as_text(self, processor):
        processor._on_left_table_change(b'order1', {'product_id': b'prod1'})

        [sent] = _subscription_calls(processor)
        assert sent['key'] == b'prod1'
        assert sent['value']['left_pk'] == 'order1'

    @pytest.mark.asyncio
    async def test_subscription_is_stored_under_text(self, processor):
        processor.right_table.get = Mock(return_value={'name': 'Widget'})
        await processor._process_subscription(b'prod1', {
            'left_pk': 'order1', 'hash': 1,
            'instruction': SUBSCRIBE_AND_RESPOND})

        assert processor.subscription_store == {
            'prod1\x00order1': {'hash': 1}}

    def test_right_change_finds_subscribers_stored_under_text(
            self, processor):
        processor._subscription_store = _MockStore({
            'prod1\x00order1': {'hash': 1}})
        processor._on_right_table_change(b'prod1', {'name': 'Widget'})

        [call] = processor.response_topic.send_soon.call_args_list
        assert call.kwargs['key'] == 'order1'

    def test_text_left_key_is_sent_as_the_raw_key(self, app):
        left = app.Table('orders', partitions=4, key_type=bytes)
        right = app.Table('products', partitions=4)
        join = KeyJoinProcessor(app, left, right, extractor=lambda v: v)

        sent, _ = join.response_topic.prepare_key('order1', None)
        assert sent == b'order1'


class test_process_subscription:

    @pytest.fixture()
    def processor(self, processor):
        processor.right_table.get = Mock(return_value={'name': 'Widget'})
        return processor

    @pytest.mark.asyncio
    async def test_subscribe_and_respond_stores_and_responds(self, processor):
        await processor._process_subscription('prod1', {
            'left_pk': 'order1', 'hash': 7,
            'instruction': SUBSCRIBE_AND_RESPOND,
        })

        assert processor.subscription_store['prod1\x00order1'] == {'hash': 7}
        processor.response_topic.send.assert_called_once()
        sent = processor.response_topic.send.call_args.kwargs
        assert sent['key'] == 'order1'
        assert sent['value'] == {'right_value': {'name': 'Widget'}, 'hash': 7}

    @pytest.mark.asyncio
    async def test_unsubscribe_only_removes_without_response(self, processor):
        processor.subscription_store['prod1\x00order1'] = {'hash': 7}
        await processor._process_subscription('prod1', {
            'left_pk': 'order1', 'hash': 0, 'instruction': UNSUBSCRIBE_ONLY,
        })

        assert 'prod1\x00order1' not in processor.subscription_store
        processor.response_topic.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_subscribe_only_stores_without_response(self, processor):
        await processor._process_subscription('prod1', {
            'left_pk': 'order1', 'hash': 7, 'instruction': SUBSCRIBE_ONLY,
        })

        assert 'prod1\x00order1' in processor.subscription_store
        processor.response_topic.send.assert_not_called()


class test_right_table_change:

    @pytest.fixture()
    def processor(self, processor):
        processor._subscription_store = _MockStore({
            'prod1\x00order1': {'hash': 1},
            'prod1\x00order2': {'hash': 2},
        })
        return processor

    def test_update_responds_to_every_subscriber(self, processor):
        processor._on_right_table_change('prod1', {'name': 'Updated'})

        sent = {c.kwargs['key']: c.kwargs['value'] for c in
                processor.response_topic.send_soon.call_args_list}
        assert sent == {
            'order1': {'right_value': {'name': 'Updated'}, 'hash': 1},
            'order2': {'right_value': {'name': 'Updated'}, 'hash': 2},
        }

    def test_update_without_subscribers_sends_nothing(self, processor):
        processor._on_right_table_change('prod_no_subs', {'name': 'Widget'})
        processor.response_topic.send_soon.assert_not_called()

    def test_deletion_responds_with_no_right_value(self, processor):
        processor._on_right_table_change('prod1', None, is_delete=True)

        calls = processor.response_topic.send_soon.call_args_list
        assert len(calls) == 2
        assert all(c.kwargs['value']['right_value'] is None for c in calls)


class test_process_response:

    LEFT = {'product_id': 'prod1', 'qty': 5}

    @pytest.fixture()
    def processor(self, processor):
        processor.left_table.get = Mock(return_value=self.LEFT)
        return processor

    def _response(self, right_value, hash=None):
        if hash is None:
            hash = KeyJoinProcessor.compute_hash(self.LEFT)
        return {'right_value': right_value, 'hash': hash}

    @pytest.mark.asyncio
    async def test_current_hash_emits_joined_value(self, processor):
        await _respond(processor, 'order1', self._response({'name': 'Widget'}))

        assert _results(processor) == [
            ('order1', JoinedValue(left=self.LEFT, right={'name': 'Widget'}))]

    @pytest.mark.asyncio
    async def test_stale_hash_discards(self, processor):
        await _respond(
            processor, 'order1', self._response({'name': 'Widget'}, hash=1))
        assert _results(processor) == []

    @pytest.mark.asyncio
    async def test_deleted_left_record_discards(self, processor):
        processor.left_table.get = Mock(return_value=None)
        await _respond(processor, 'order1', self._response({'name': 'Widget'}))
        assert _results(processor) == []

    @pytest.mark.asyncio
    async def test_inner_join_skips_missing_right_value(self, processor):
        await _respond(processor, 'order1', self._response(None))
        assert _results(processor) == []

    @pytest.mark.asyncio
    async def test_left_join_emits_missing_right_value(self, processor):
        processor.inner = False
        await _respond(processor, 'order1', self._response(None))
        assert _results(processor) == [
            ('order1', JoinedValue(left=self.LEFT, right=None))]


class test_end_to_end:

    @pytest.fixture()
    def processor(self, processor):
        processor.right_table.get = Mock(
            return_value={'name': 'Widget', 'price': 9.99})
        return processor

    @pytest.mark.asyncio
    async def test_left_change_joins_through_subscription(self, processor):
        left_value = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', left_value)
        [sent] = _subscription_calls(processor)

        await processor._process_subscription(sent['key'], sent['value'])
        assert 'prod1\x00order1' in processor.subscription_store

        response = processor.response_topic.send.call_args.kwargs
        processor.left_table.get = Mock(return_value=left_value)
        await _respond(processor, response['key'], response['value'])

        assert _results(processor) == [('order1', JoinedValue(
            left=left_value, right={'name': 'Widget', 'price': 9.99}))]

    @pytest.mark.asyncio
    async def test_right_update_reaches_every_subscriber(self, processor):
        lefts = {'order1': {'product_id': 'prod1', 'qty': 5},
                 'order2': {'product_id': 'prod1', 'qty': 3}}
        for pk, value in lefts.items():
            processor.subscription_store[f'prod1\x00{pk}'] = {
                'hash': KeyJoinProcessor.compute_hash(value)}
        processor.left_table.get = Mock(side_effect=lefts.get)

        processor._on_right_table_change('prod1', {'name': 'Updated'})
        for c in processor.response_topic.send_soon.call_args_list:
            await _respond(processor, c.kwargs['key'], c.kwargs['value'])

        assert sorted(key for key, _ in _results(processor)) == [
            'order1', 'order2']

    @pytest.mark.asyncio
    async def test_response_to_an_older_version_is_discarded(self, processor):
        first = {'product_id': 'prod1', 'qty': 5}
        processor._on_left_table_change('order1', first)
        [sent] = _subscription_calls(processor)
        await processor._process_subscription(sent['key'], sent['value'])
        response = processor.response_topic.send.call_args.kwargs

        second = {'product_id': 'prod1', 'qty': 10}
        processor._on_left_table_change('order1', second)
        processor.left_table.get = Mock(return_value=second)
        await _respond(processor, response['key'], response['value'])

        assert _results(processor) == []


class test_output_acks:
    """A response is committed only once its result is acked downstream."""

    LEFT = {'product_id': 'prod1', 'qty': 5}

    @pytest.fixture()
    def processor(self, processor):
        processor.left_table.get = Mock(return_value=self.LEFT)
        return processor

    def _response(self):
        return {'right_value': {'name': 'Widget'},
                'hash': KeyJoinProcessor.compute_hash(self.LEFT)}

    @pytest.mark.asyncio
    async def test_result_carries_the_response(self, processor):
        response = await _respond(processor, 'order1', self._response())

        [result] = _emitted(processor)
        assert result.message is response.message
        assert result.key == 'order1'

    @pytest.mark.asyncio
    async def test_response_waits_for_the_result_ack(self, processor):
        response = await _respond(processor, 'order1', self._response())
        [result] = _emitted(processor)

        response.ack()  # the response stream is done with it
        assert not response.message.acked
        result.ack()  # the agent reading the output is done with it
        assert response.message.acked

    @pytest.mark.asyncio
    async def test_every_subscriber_acks(self, processor):
        second = processor._output_channel.clone(is_iterator=True)
        response = await _respond(processor, 'order1', self._response())
        [first_result] = _emitted(processor)
        [second_result] = _emitted(processor, second)

        response.ack()
        first_result.ack()
        assert not response.message.acked
        second_result.ack()
        assert response.message.acked

    @pytest.mark.asyncio
    async def test_without_subscribers_nothing_is_held(self, processor):
        del processor.subscriber
        import gc
        gc.collect()
        response = await _respond(processor, 'order1', self._response())

        response.ack()
        assert response.message.acked

    @pytest.mark.asyncio
    async def test_discarded_response_is_not_held(self, processor):
        response = await _respond(
            processor, 'order1', {'right_value': None, 'hash': 1})

        response.ack()
        assert response.message.acked

    @pytest.mark.asyncio
    async def test_result_is_dropped_while_the_flow_is_suspended(
            self, processor):
        # A rebalance: waiting for the flow would hold the response the
        # rebalance waits for.
        processor.app.flow_control.suspend()
        response = await asyncio.wait_for(
            _respond(processor, 'order1', self._response()), timeout=1)

        assert _emitted(processor) == []
        response.ack()
        assert not response.message.acked

    @pytest.mark.asyncio
    async def test_put_cleared_by_a_rebalance_drops_the_result(self, app):
        processor = _with_topics(_make_processor(app))
        processor._output_channel = app.channel(maxsize=1)
        processor.subscriber = processor._output_channel.clone(
            is_iterator=True)
        processor.left_table.get = Mock(return_value=self.LEFT)
        await _respond(processor, 'order0', self._response(), offset=0)

        blocked = asyncio.ensure_future(
            _respond(processor, 'order1', self._response()))
        await asyncio.sleep(0.01)
        assert not blocked.done()
        app.flow_control.suspend()
        app.flow_control.clear()
        response = await asyncio.wait_for(blocked, timeout=1)

        assert _emitted(processor) == []
        response.ack()
        assert not response.message.acked

    @pytest.mark.asyncio
    async def test_stopping_cancels_a_blocked_put(self, app):
        processor = _with_topics(_make_processor(app))
        processor._output_channel = app.channel(maxsize=1)
        processor.subscriber = processor._output_channel.clone(
            is_iterator=True)
        processor.left_table.get = Mock(return_value=self.LEFT)
        await _respond(processor, 'order0', self._response(), offset=0)

        blocked = asyncio.ensure_future(
            _respond(processor, 'order1', self._response()))
        await asyncio.sleep(0.01)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert processor.subscriber.queue._putters == type(
            processor.subscriber.queue._putters)()


class test_wiring:

    @pytest.mark.asyncio
    async def test_on_start_registers_callbacks(self, processor):
        processor.add_future = Mock(
            name='add_future', side_effect=lambda coro: coro.close())
        await processor.on_start()

        processor.left_table.register_on_key_set.assert_called_once_with(
            processor._on_left_key_set)
        processor.left_table.register_on_key_del.assert_called_once_with(
            processor._on_left_key_del)
        processor.right_table.register_on_key_set.assert_called_once_with(
            processor._on_right_key_set)
        processor.right_table.register_on_key_del.assert_called_once_with(
            processor._on_right_key_del)

    @pytest.mark.asyncio
    async def test_on_stop_unregisters_callbacks(self, processor):
        await processor.on_stop()

        processor.left_table.unregister_on_key_set.assert_called_once_with(
            processor._on_left_key_set)
        processor.left_table.unregister_on_key_del.assert_called_once_with(
            processor._on_left_key_del)
        processor.right_table.unregister_on_key_set.assert_called_once_with(
            processor._on_right_key_set)
        processor.right_table.unregister_on_key_del.assert_called_once_with(
            processor._on_right_key_del)

    def test_left_key_set_delegates(self, processor):
        processor._on_left_table_change = Mock(name='_on_left_table_change')
        processor._on_left_key_set('order1', {'product_id': 'prod1'})
        processor._on_left_table_change.assert_called_once_with(
            'order1', {'product_id': 'prod1'})

    def test_left_key_del_delegates_with_is_delete(self, processor):
        processor._on_left_table_change = Mock(name='_on_left_table_change')
        processor._on_left_key_del('order1')
        processor._on_left_table_change.assert_called_once_with(
            'order1', None, is_delete=True)

    def test_right_key_set_delegates(self, processor):
        processor._on_right_table_change = Mock(name='_on_right_table_change')
        processor._on_right_key_set('prod1', {'name': 'Widget'})
        processor._on_right_table_change.assert_called_once_with(
            'prod1', {'name': 'Widget'})

    def test_right_key_del_delegates_with_is_delete(self, processor):
        processor._on_right_table_change = Mock(name='_on_right_table_change')
        processor._on_right_key_del('prod1')
        processor._on_right_table_change.assert_called_once_with(
            'prod1', None, is_delete=True)

    def test_table_callbacks_send_soon(self, processor):
        # Table callbacks are synchronous: they cannot await a send.
        processor._subscription_store = _MockStore({'prod1\x00order1': {}})
        processor._on_left_table_change('order2', {'product_id': 'prod1'})
        processor._on_right_table_change('prod1', {'name': 'Widget'})
        processor.subscription_topic.send.assert_not_called()
        processor.response_topic.send.assert_not_called()
        processor.subscription_topic.send_soon.assert_called()
        processor.response_topic.send_soon.assert_called()


class test_deprecated_aliases:

    def test_ForeignKeyJoin_warns(self):
        from faust.joins import ForeignKeyJoin, KeyJoin
        with pytest.warns(DeprecationWarning, match='KeyJoin'):
            join = ForeignKeyJoin(Mock(), Mock(), extractor=Mock())
        assert isinstance(join, KeyJoin)

    def test_foreign_key_join_method_warns(self, app):
        left = app.Table('left_dep', default=None)
        right = app.Table('right_dep', default=None)
        left.partitions = 4
        right.partitions = 4
        with pytest.warns(DeprecationWarning, match='key_join'):
            left.foreign_key_join(right, extractor=lambda v: v)
