"""Message transport using :pypi:`aiokafka`."""
import asyncio
import typing
import abc
import sys
import traceback
import warnings

from collections import deque
from time import monotonic
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Type,
    cast,
    no_type_check,
)

import aiokafka
import aiokafka.abc
import opentracing
from aiokafka.consumer.group_coordinator import OffsetCommitRequest
from aiokafka.errors import (
    CommitFailedError,
    ConsumerStoppedError,
    IllegalStateError,
    KafkaError,
    NotControllerError,
    TopicAlreadyExistsError as TopicExistsError,
    for_code,
)
from aiokafka.structs import (
    OffsetAndMetadata,
    TopicPartition as _TopicPartition,
)
from aiokafka.util import parse_kafka_version
from aiokafka import TopicPartition
from aiokafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
from aiokafka.partitioner import DefaultPartitioner, murmur2
from aiokafka.protocol.admin import CreateTopicsRequest
from aiokafka.protocol.metadata import MetadataRequest_v1
from mode import Service, get_logger
from mode.utils.futures import StampedeWrapper
from mode.utils.objects import cached_property
from mode.utils import text
from mode.utils.times import Seconds, humanize_seconds_ago, want_seconds
from mode.utils.typing import Deque
from opentracing.ext import tags
from yarl import URL

from faust.auth import (
    GSSAPICredentials,
    SASLCredentials,
    SSLCredentials,
)
from faust.exceptions import (
    ConsumerNotStarted,
    ImproperlyConfigured,
    NotReady,
    ProducerSendError,
)
from faust.transport import base
from faust.transport.consumer import (
    ConsumerThread,
    RecordMap,
    ThreadDelegateConsumer,
    ensure_TPset,
)
from faust.types import ConsumerMessage, HeadersArg, RecordMetadata, TP
from faust.types.auth import CredentialsT
from faust.types.transports import (
    ConsumerT,
    PartitionerT,
    ProducerT,
)
from faust.utils.tracing import (
    noop_span,
    set_current_span,
    traced_from_parent_span,
)

from aiokafka.client import AIOKafkaClient
from aiokafka.codec import has_gzip, has_lz4, has_snappy, has_zstd
from aiokafka.errors import (
    IllegalOperation,
    MessageSizeTooLargeError,
    UnsupportedVersionError,
)

from aiokafka.record.default_records import DefaultRecordBatch
from aiokafka.record.legacy_records import LegacyRecordBatchBuilder
from aiokafka.util import (
    commit_structure_validate,
    create_task,
    get_running_loop,
)

from aiokafka.producer.message_accumulator import MessageAccumulator
from aiokafka.producer.sender import Sender
from aiokafka.producer.transaction_manager import TransactionManager
from aiokafka.producer import AIOKafkaProducer

_DEFAULT_PARTITIONER = DefaultPartitioner

_missing = object()

__all__ = ['Consumer', 'Producer', 'Transport']

logger = get_logger(__name__)

DEFAULT_GENERATION_ID = OffsetCommitRequest.DEFAULT_GENERATION_ID

TOPIC_LENGTH_MAX = 249

SLOW_PROCESSING_CAUSE_AGENT = '''
The agent processing the stream is hanging (waiting for network, I/O or \
infinite loop).
'''.strip()

SLOW_PROCESSING_CAUSE_STREAM = '''
The stream has stopped processing events for some reason.
'''.strip()


SLOW_PROCESSING_CAUSE_COMMIT = '''
The commit handler background thread has stopped working (report as bug).
'''.strip()


SLOW_PROCESSING_EXPLAINED = '''

There are multiple possible explanations for this:

1) The processing of a single event in the stream
   is taking too long.

    The timeout for this is defined by the %(setting)s setting,
    currently set to %(current_value)r.  If you expect the time
    required to process an event, to be greater than this then please
    increase the timeout.

'''

SLOW_PROCESSING_NO_FETCH_SINCE_START = '''
Aiokafka has not sent fetch request for %r since start (started %s)
'''.strip()

SLOW_PROCESSING_NO_RESPONSE_SINCE_START = '''
Aiokafka has not received fetch response for %r since start (started %s)
'''.strip()

SLOW_PROCESSING_NO_RECENT_FETCH = '''
Aiokafka stopped fetching from %r (last done %s)
'''.strip()

SLOW_PROCESSING_NO_RECENT_RESPONSE = '''
Broker stopped responding to fetch requests for %r (last responded %s)
'''.strip()

SLOW_PROCESSING_NO_HIGHWATER_SINCE_START = '''
Highwater not yet available for %r (started %s).
'''.strip()

SLOW_PROCESSING_STREAM_IDLE_SINCE_START = '''
Stream has not started processing %r (started %s).
'''.strip()

SLOW_PROCESSING_STREAM_IDLE = '''
Stream stopped processing, or is slow for %r (last inbound %s).
'''.strip()

SLOW_PROCESSING_NO_COMMIT_SINCE_START = '''
Has not committed %r at all since worker start (started %s).
'''.strip()

SLOW_PROCESSING_NO_RECENT_COMMIT = '''
Has not committed %r (last commit %s).
'''.strip()


def server_list(urls: List[URL], default_port: int) -> List[str]:
    """Convert list of urls to list of servers accepted by :pypi:`aiokafka`."""
    default_host = '127.0.0.1'
    return [f'{u.host or default_host}:{u.port or default_port}' for u in urls]


class ConsumerRebalanceListener(
        aiokafka.abc.ConsumerRebalanceListener):  # type: ignore
    # kafka's ridiculous class based callback interface makes this hacky.

    def __init__(self, thread: ConsumerThread) -> None:
        self._thread: ConsumerThread = thread

    def on_partitions_revoked(
            self, revoked: Iterable[_TopicPartition]) -> Awaitable:
        """Call when partitions are being revoked."""
        thread = self._thread
        # XXX Must call app.on_rebalance_start as early as possible.
        # we call this in the sync method, this way when we know
        # that it will be called even if await never returns to the coroutine.
        thread.app.on_rebalance_start()

        # this way we should also get a warning if the coroutine
        # is never awaited.
        return thread.on_partitions_revoked(ensure_TPset(revoked))

    async def on_partitions_assigned(
            self, assigned: Iterable[_TopicPartition]) -> None:
        """Call when partitions are being assigned."""
        await self._thread.on_partitions_assigned(ensure_TPset(assigned))


class Consumer(ThreadDelegateConsumer):
    """Kafka consumer using :pypi:`aiokafka`."""

    logger = logger

    RebalanceListener: ClassVar[Type[ConsumerRebalanceListener]]
    RebalanceListener = ConsumerRebalanceListener

    consumer_stopped_errors: ClassVar[Tuple[Type[BaseException], ...]] = (
        ConsumerStoppedError,
    )

    def _new_consumer_thread(self) -> ConsumerThread:
        return AIOKafkaConsumerThread(self, loop=self.loop, beacon=self.beacon)

    async def create_topic(self,
                           topic: str,
                           partitions: int,
                           replication: int,
                           *,
                           config: Mapping[str, Any] = None,
                           timeout: Seconds = 30.0,
                           retention: Seconds = None,
                           compacting: bool = None,
                           deleting: bool = None,
                           ensure_created: bool = False) -> None:
        """Create/declare topic on server."""
        await self._thread.create_topic(
            topic,
            partitions,
            replication,
            config=config,
            timeout=timeout,
            retention=retention,
            compacting=compacting,
            deleting=deleting,
            ensure_created=ensure_created,
        )

    def _new_topicpartition(self, topic: str, partition: int) -> TP:
        return cast(TP, _TopicPartition(topic, partition))

    def _to_message(self, tp: TP, record: Any) -> ConsumerMessage:
        timestamp: Optional[int] = record.timestamp
        timestamp_s: float = cast(float, None)
        if timestamp is not None:
            timestamp_s = timestamp / 1000.0
        return ConsumerMessage(
            record.topic,
            record.partition,
            record.offset,
            timestamp_s,
            record.timestamp_type,
            record.headers,
            record.key,
            record.value,
            record.checksum,
            record.serialized_key_size,
            record.serialized_value_size,
            tp,
        )

    async def on_stop(self) -> None:
        """Call when consumer is stopping."""
        await super().on_stop()
        transport = cast(Transport, self.transport)
        transport._topic_waiters.clear()


class AIOKafkaConsumerThread(ConsumerThread):
    _consumer: Optional[aiokafka.AIOKafkaConsumer] = None
    _pending_rebalancing_spans: Deque[opentracing.Span]

    tp_last_committed_at: MutableMapping[TP, float]
    time_started: float

    tp_fetch_request_timeout_secs: float
    tp_fetch_response_timeout_secs: float
    tp_stream_timeout_secs: float
    tp_commit_timeout_secs: float

    def __post_init__(self) -> None:
        consumer = cast(Consumer, self.consumer)
        self._partitioner: PartitionerT = (
            self.app.conf.producer_partitioner or DefaultPartitioner())
        self._rebalance_listener = consumer.RebalanceListener(self)
        self._pending_rebalancing_spans = deque()
        self.tp_last_committed_at = {}

        app = self.consumer.app

        stream_processing_timeout = app.conf.stream_processing_timeout
        self.tp_fetch_request_timeout_secs = stream_processing_timeout
        self.tp_fetch_response_timeout_secs = stream_processing_timeout
        self.tp_stream_timeout_secs = stream_processing_timeout

        commit_livelock_timeout = app.conf.broker_commit_livelock_soft_timeout
        self.tp_commit_timeout_secs = commit_livelock_timeout

    async def on_start(self) -> None:
        """Call when consumer starts."""
        self._consumer = self._create_consumer(loop=self.thread_loop)
        self.time_started = monotonic()
        await self._consumer.start()

    async def on_thread_stop(self) -> None:
        """Call when consumer thread is stopping."""
        # super stops thread method queue (QueueServiceThread.method_queue)
        await super().on_thread_stop()
        # when method queue is stopped, we can stop the consumer
        if self._consumer is not None:
            await self._consumer.stop()

    def _create_consumer(
            self,
            loop: asyncio.AbstractEventLoop) -> aiokafka.AIOKafkaConsumer:
        transport = cast(Transport, self.transport)
        if self.app.client_only:
            return self._create_client_consumer(transport, loop=loop)
        else:
            return self._create_worker_consumer(transport, loop=loop)

    def _create_worker_consumer(
            self,
            transport: 'Transport',
            loop: asyncio.AbstractEventLoop) -> aiokafka.AIOKafkaConsumer:
        isolation_level: str = 'read_uncommitted'
        conf = self.app.conf
        if self.consumer.in_transaction:
            isolation_level = 'read_committed'
        self._assignor = (
            self.app.assignor
            if self.app.conf.table_standby_replicas > 0
            else RoundRobinPartitionAssignor
        )
        auth_settings = credentials_to_aiokafka_auth(
            conf.broker_credentials, conf.ssl_context)
        max_poll_interval = conf.broker_max_poll_interval or 0

        request_timeout = conf.broker_request_timeout
        session_timeout = conf.broker_session_timeout
        rebalance_timeout = conf.broker_rebalance_timeout

        if session_timeout > request_timeout:
            raise ImproperlyConfigured(
                f'Setting broker_session_timeout={session_timeout} '
                f'cannot be greater than '
                f'broker_request_timeout={request_timeout}')

        return aiokafka.AIOKafkaConsumer(
            loop=loop,
            api_version=conf.consumer_api_version,
            client_id=conf.broker_client_id,
            group_id=conf.id,
            group_instance_id=conf.consumer_group_instance_id,
            bootstrap_servers=server_list(
                transport.url, transport.default_port),
            partition_assignment_strategy=[self._assignor],
            enable_auto_commit=False,
            auto_offset_reset=conf.consumer_auto_offset_reset,
            max_poll_records=conf.broker_max_poll_records,
            max_poll_interval_ms=int(max_poll_interval * 1000.0),
            max_partition_fetch_bytes=conf.consumer_max_fetch_size,
            fetch_max_wait_ms=1500,
            request_timeout_ms=int(request_timeout * 1000.0),
            check_crcs=conf.broker_check_crcs,
            session_timeout_ms=int(session_timeout * 1000.0),
            rebalance_timeout_ms=int(rebalance_timeout * 1000.0),
            heartbeat_interval_ms=int(conf.broker_heartbeat_interval * 1000.0),
            isolation_level=isolation_level,
            **auth_settings,
        )

    def _create_client_consumer(
            self,
            transport: 'Transport',
            loop: asyncio.AbstractEventLoop) -> aiokafka.AIOKafkaConsumer:
        conf = self.app.conf
        auth_settings = credentials_to_aiokafka_auth(
            conf.broker_credentials, conf.ssl_context)
        max_poll_interval = conf.broker_max_poll_interval or 0
        return aiokafka.AIOKafkaConsumer(
            client_id=conf.broker_client_id,
            bootstrap_servers=server_list(transport.url, transport.default_port),
            request_timeout_ms=int(conf.broker_request_timeout * 1000.0),
            enable_auto_commit=True,
            max_poll_records=conf.broker_max_poll_records,
            max_poll_interval_ms=int(max_poll_interval * 1000.0),
            auto_offset_reset=conf.consumer_auto_offset_reset,
            check_crcs=conf.broker_check_crcs,
            **auth_settings,
        )

    @cached_property
    def trace_category(self) -> str:
        return f'{self.app.conf.name}-_aiokafka'

    def start_rebalancing_span(self) -> opentracing.Span:
        return self._start_span('rebalancing', lazy=True)

    def start_coordinator_span(self) -> opentracing.Span:
        return self._start_span('coordinator')

    def _start_span(self, name: str, *,
                    lazy: bool = False) -> opentracing.Span:
        tracer = self.app.tracer
        if tracer is not None:
            span = tracer.get_tracer(self.trace_category).start_span(
                operation_name=name,
            )
            span.set_tag(tags.SAMPLING_PRIORITY, 1)
            self.app._span_add_default_tags(span)
            set_current_span(span)
            if lazy:
                self._transform_span_lazy(span)
            return span
        else:
            return noop_span()

    @no_type_check
    def _transform_span_lazy(self, span: opentracing.Span) -> None:
        # XXX slow
        consumer = self
        if typing.TYPE_CHECKING:
            # MyPy completely disallows the statements below
            # claiming it is an illegal dynamic baseclass.
            # We know mypy, but do it anyway :D
            pass
        else:
            cls = span.__class__
            class LazySpan(cls):

                def finish() -> None:
                    consumer._span_finish(span)

            span._real_finish, span.finish = span.finish, LazySpan.finish

    def _span_finish(self, span: opentracing.Span) -> None:
        assert self._consumer is not None
        if self._consumer._coordinator.generation == DEFAULT_GENERATION_ID:
            self._on_span_generation_pending(span)
        else:
            self._on_span_generation_known(span)

    def _on_span_generation_pending(self, span: opentracing.Span) -> None:
        self._pending_rebalancing_spans.append(span)

    def _on_span_generation_known(self, span: opentracing.Span) -> None:
        if self._consumer:
            coordinator = self._consumer._coordinator
            coordinator_id = coordinator.coordinator_id
            app_id = self.app.conf.id
            generation = coordinator.generation
            member_id = coordinator.member_id

            try:
                op_name = span.operation_name
                set_tag = span.set_tag
            except AttributeError:  # pragma: no cover
                pass  # not a real span
            else:
                trace_id_str = f'reb-{app_id}-{generation}'
                trace_id = murmur2(trace_id_str.encode())

                span.context.trace_id = trace_id
                if op_name.endswith('.REPLACE_WITH_MEMBER_ID'):
                    span.set_operation_name(f'rebalancing node {member_id}')
                set_tag('kafka_generation', generation)
                set_tag('kafka_member_id', member_id)
                set_tag('kafka_coordinator_id', coordinator_id)
                self.app._span_add_default_tags(span)
                span._real_finish()

    def _on_span_cancelled_early(self, span: opentracing.Span) -> None:
        try:
            op_name = span.operation_name
        except AttributeError:
            return
        else:
            span.set_operation_name(f'{op_name} (CANCELLED)')
            span._real_finish()

    def traced_from_parent_span(self,
                                parent_span: opentracing.Span,
                                lazy: bool = False,
                                **extra_context: Any) -> Callable:
        return traced_from_parent_span(
            parent_span,
            callback=self._transform_span_lazy if lazy else None,
            **extra_context)

    def flush_spans(self) -> None:
        while self._pending_rebalancing_spans:
            span = self._pending_rebalancing_spans.popleft()
            self._on_span_cancelled_early(span)

    def on_generation_id_known(self) -> None:
        while self._pending_rebalancing_spans:
            span = self._pending_rebalancing_spans.popleft()
            self._on_span_generation_known(span)

    def close(self) -> None:
        """Close consumer for graceful shutdown."""
        if self._consumer is not None:
            self._consumer._closed = True
            asyncio.run_coroutine_threadsafe(
                self._consumer._client.close(), self.app.loop
            )
            asyncio.run_coroutine_threadsafe(
                self._consumer._coordinator.close(), self.app.loop
            )            

    async def subscribe(self, topics: Iterable[str]) -> None:
        """Reset subscription (requires rebalance)."""
        # XXX pattern does not work :/
        await self.call_thread(
            self._ensure_consumer().subscribe,
            topics=set(topics),
            listener=self._rebalance_listener,
        )

    async def seek_to_committed(self) -> Mapping[TP, int]:
        """Seek partitions to the last committed offset."""
        return await self.call_thread(
            self._ensure_consumer().seek_to_committed)

    async def commit(self, offsets: Mapping[TP, int]) -> bool:
        """Commit topic offsets."""
        return await self.call_thread(self._commit, offsets)

    async def _commit(self, offsets: Mapping[TP, int]) -> bool:
        consumer = self._ensure_consumer()
        now = monotonic()
        try:
            aiokafka_offsets = {
                tp: OffsetAndMetadata(offset, '')
                for tp, offset in offsets.items()
            }
            self.tp_last_committed_at.update({
                tp: now
                for tp in offsets
            })
            await consumer.commit(aiokafka_offsets)
        except CommitFailedError as exc:
            if 'already rebalanced' in str(exc):
                return False
            self.log.exception('Committing raised exception: %r', exc)
            await self.crash(exc)
            return False
        except IllegalStateError as exc:
            self.log.exception(
                'Got exception: %r\nCurrent assignment: %r',
                exc, self.assignment())
            await self.crash(exc)
            return False
        return True

    def verify_event_path(self, now: float, tp: TP) -> None:
        # long function ahead, but not difficult to test
        # as it always returns as soon as some condition is met.
        if self._verify_aiokafka_event_path(now, tp):
            # already logged error.
            return None
        parent = cast(Consumer, self.consumer)
        app = parent.app
        monitor = app.monitor
        acks_enabled_for = app.topics.acks_enabled_for
        secs_since_started = now - self.time_started

        if monitor is not None:  # need for .stream_inbound_time
            highwater = self.highwater(tp)
            committed_offset = parent._committed_offset.get(tp)
            has_acks = acks_enabled_for(tp.topic)
            if highwater is None:
                if secs_since_started >= self.tp_stream_timeout_secs:
                    # AIOKAFKA HAS NOT UPDATED HIGHWATER SINCE STARTING
                    self.log.error(
                        SLOW_PROCESSING_NO_HIGHWATER_SINCE_START,
                        tp, humanize_seconds_ago(secs_since_started),
                    )
                return None

            if has_acks and committed_offset is not None:
                if highwater > committed_offset:
                    inbound = monitor.stream_inbound_time.get(tp)
                    if inbound is None:
                        if secs_since_started >= self.tp_stream_timeout_secs:
                            # AIOKAFKA IS FETCHING BUT STREAM IS NOT
                            # PROCESSING EVENTS (no events at all since
                            # start).
                            self._log_slow_processing_stream(
                                SLOW_PROCESSING_STREAM_IDLE_SINCE_START,
                                tp, humanize_seconds_ago(secs_since_started),
                            )
                        return None

                    secs_since_stream = now - inbound
                    if secs_since_stream >= self.tp_stream_timeout_secs:
                        # AIOKAFKA IS FETCHING, AND STREAM WAS WORKING
                        # BEFORE BUT NOW HAS STOPPED PROCESSING
                        # (or processing of an event in the stream takes
                        #  longer than tp_stream_timeout_secs).
                        self._log_slow_processing_stream(
                            SLOW_PROCESSING_STREAM_IDLE,
                            tp, humanize_seconds_ago(secs_since_stream),
                        )
                        return None

                    last_commit = self.tp_last_committed_at.get(tp)
                    if last_commit is None:
                        if secs_since_started >= self.tp_commit_timeout_secs:
                            # AIOKAFKA IS FETCHING AND STREAM IS PROCESSING
                            # BUT WE HAVE NOT COMMITTED ANYTHING SINCE WORKER
                            # START.
                            self._log_slow_processing_commit(
                                SLOW_PROCESSING_NO_COMMIT_SINCE_START,
                                tp, humanize_seconds_ago(secs_since_started),
                            )
                            return None
                    else:
                        secs_since_commit = now - last_commit
                        if secs_since_commit >= self.tp_commit_timeout_secs:
                            # AIOKAFKA IS FETCHING AND STREAM IS PROCESSING
                            # BUT WE HAVE NOT COMITTED ANYTHING IN A WHILE
                            # (commit offset is not advancing).
                            self._log_slow_processing_commit(
                                SLOW_PROCESSING_NO_RECENT_COMMIT,
                                tp, humanize_seconds_ago(secs_since_commit),
                            )
                            return None

    def verify_recovery_event_path(self, now: float, tp: TP) -> None:
        self._verify_aiokafka_event_path(now, tp)

    def _verify_aiokafka_event_path(self, now: float, tp: TP) -> bool:
        """Verify that :pypi:`aiokafka` event path is working.

        Returns :const:`True` if any error was logged.
        """
        consumer = self._ensure_consumer()
        secs_since_started = now - self.time_started
        aiotp = TopicPartition(tp.topic, tp.partition)
        assignment = consumer._fetcher._subscriptions.subscription.assignment
        if not assignment or not assignment.active:
            self.log.error(f"No active partitions for {tp}")
            return True
        poll_at = None
        aiotp_state = assignment.state_value(aiotp)
        if aiotp_state and aiotp_state.timestamp:
            poll_at = aiotp_state.timestamp / 1000  # milliseconds
        if poll_at is None:
            if secs_since_started >= self.tp_fetch_request_timeout_secs:
                # NO FETCH REQUEST SENT AT ALL SINCE WORKER START
                self.log.error(
                    SLOW_PROCESSING_NO_FETCH_SINCE_START,
                    tp,
                    humanize_seconds_ago(secs_since_started),
                )
            return True

        secs_since_request = now - poll_at
        if secs_since_request >= self.tp_fetch_request_timeout_secs:
            # NO REQUEST SENT BY AIOKAFKA IN THE LAST n SECONDS
            self.log.error(
                SLOW_PROCESSING_NO_RECENT_FETCH,
                tp,
                humanize_seconds_ago(secs_since_request),
            )
            return True

        return False

    def _log_slow_processing_stream(self, msg: str, *args: Any) -> None:
        app = self.consumer.app
        self._log_slow_processing(
            msg, *args,
            causes=[
                SLOW_PROCESSING_CAUSE_STREAM,
                SLOW_PROCESSING_CAUSE_AGENT,
            ],
            setting='stream_processing_timeout',
            current_value=app.conf.stream_processing_timeout,
        )

    def _log_slow_processing_commit(self, msg: str, *args: Any) -> None:
        app = self.consumer.app
        self._log_slow_processing(
            msg, *args,
            causes=[SLOW_PROCESSING_CAUSE_COMMIT],
            setting='broker_commit_livelock_soft_timeout',
            current_value=app.conf.broker_commit_livelock_soft_timeout,
        )

    def _make_slow_processing_error(self,
                                    msg: str,
                                    causes: Iterable[str]) -> str:
        return ' '.join([
            msg,
            SLOW_PROCESSING_EXPLAINED,
            text.enumeration(causes, start=2, sep='\n\n'),
        ])

    def _log_slow_processing(self, msg: str, *args: Any,
                             causes: Iterable[str],
                             setting: str,
                             current_value: float) -> None:
        return self.log.error(
            self._make_slow_processing_error(msg, causes),
            *args,
            setting=setting,
            current_value=current_value,
        )

    async def position(self, tp: TP) -> Optional[int]:
        """Return the current position for topic partition."""
        return await self.call_thread(
            self._ensure_consumer().position, tp)

    async def seek_to_beginning(self, *partitions: _TopicPartition) -> None:
        """Seek list of offsets to the first available offset."""
        await self.call_thread(
            self._ensure_consumer().seek_to_beginning, *partitions)

    async def seek_wait(self, partitions: Mapping[TP, int]) -> None:
        """Seek partitions to specific offset and wait for operation."""
        consumer = self._ensure_consumer()
        await self.call_thread(self._seek_wait, consumer, partitions)

    async def _seek_wait(self,
                         consumer: Consumer,
                         partitions: Mapping[TP, int]) -> None:
        for tp, offset in partitions.items():
            self.log.dev('SEEK %r -> %r', tp, offset)
            consumer.seek(tp, offset)
            if offset > 0:
                self.consumer._read_offset[tp] = offset
        await asyncio.gather(*[
            consumer.position(tp) for tp in partitions
        ])

    def seek(self, partition: TP, offset: int) -> None:
        """Seek partition to specific offset."""
        self._ensure_consumer().seek(partition, offset)

    def assignment(self) -> Set[TP]:
        """Return the current assignment."""
        return ensure_TPset(self._ensure_consumer().assignment())

    def highwater(self, tp: TP) -> int:
        """Return the last offset in a specific partition."""
        if self.consumer.in_transaction:
            return self._ensure_consumer().last_stable_offset(tp)
        else:
            return self._ensure_consumer().highwater(tp)

    def topic_partitions(self, topic: str) -> Optional[int]:
        """Return the number of partitions configured for topic by name."""
        if self._consumer is not None:
            return self._consumer._coordinator._metadata_snapshot.get(topic)
        return None

    async def earliest_offsets(self,
                               *partitions: TP) -> Mapping[TP, int]:
        """Return the earliest offsets for a list of partitions."""
        return await self.call_thread(
            self._ensure_consumer().beginning_offsets, partitions)

    async def highwaters(self, *partitions: TP) -> Mapping[TP, int]:
        """Return the last offsets for a list of partitions."""
        return await self.call_thread(self._highwaters, partitions)

    async def _highwaters(self, partitions: List[TP]) -> Mapping[TP, int]:
        consumer = self._ensure_consumer()
        if self.consumer.in_transaction:
            return {
                tp: consumer.last_stable_offset(tp)
                for tp in partitions
            }
        else:
            return cast(Mapping[TP, int],
                        await consumer.end_offsets(partitions))

    def _ensure_consumer(self) -> aiokafka.AIOKafkaConsumer:
        if self._consumer is None:
            raise ConsumerNotStarted('Consumer thread not yet started')
        return self._consumer

    async def getmany(self,
                      active_partitions: Optional[Set[TP]],
                      timeout: float) -> RecordMap:
        """Fetch batch of messages from server."""
        # Implementation for the Fetcher service.
        _consumer = self._ensure_consumer()
        # NOTE: Since we are enqueing the fetch request,
        # we need to check when dequeued that we are not in a rebalancing
        # state at that point to return early, or we
        # will create a deadlock (fetch request starts after flow stopped)
        return await self.call_thread(
            self._fetch_records,
            _consumer,
            active_partitions,
            timeout=timeout,
            max_records=_consumer._max_poll_records,
        )

    async def _fetch_records(self,
                             consumer: aiokafka.AIOKafkaConsumer,
                             active_partitions: Set[TP],
                             timeout: float = None,
                             max_records: int = None) -> RecordMap:
        if not self.consumer.flow_active:
            return {}
        fetcher = consumer._fetcher
        if consumer._closed or fetcher._closed:
            raise ConsumerStoppedError()
        with fetcher._subscriptions.fetch_context():
            return await fetcher.fetched_records(
                active_partitions,
                timeout=timeout,
                max_records=max_records,
            )      

    async def create_topic(self,
                           topic: str,
                           partitions: int,
                           replication: int,
                           *,
                           config: Mapping[str, Any] = None,
                           timeout: Seconds = 30.0,
                           retention: Seconds = None,
                           compacting: bool = None,
                           deleting: bool = None,
                           ensure_created: bool = False) -> None:
        """Create/declare topic on server."""
        transport = cast(Transport, self.consumer.transport)
        _consumer = self._ensure_consumer()
        _retention = (int(want_seconds(retention) * 1000.0)
                      if retention else None)
        if len(topic) > TOPIC_LENGTH_MAX:
            raise ValueError(
                f'Topic name {topic!r} is too long (max={TOPIC_LENGTH_MAX})')
        await self.call_thread(
            transport._create_topic,
            self,
            _consumer._client,
            topic,
            partitions,
            replication,
            config=config,
            timeout=int(want_seconds(timeout) * 1000.0),
            retention=_retention,
            compacting=compacting,
            deleting=deleting,
            ensure_created=ensure_created,
        )

    def key_partition(self,
                      topic: str,
                      key: Optional[bytes],
                      partition: int = None) -> Optional[int]:
        """Hash key to determine partition destination."""
        consumer = self._ensure_consumer()
        metadata = consumer._client.cluster
        partitions_for_topic = metadata.partitions_for_topic(topic)
        if partitions_for_topic is None:
            return None
        if partition is not None:
            assert partition >= 0
            assert partition in partitions_for_topic, \
                'Unrecognized partition'
            return partition

        all_partitions = list(partitions_for_topic)
        available = list(metadata.available_partitions_for_topic(topic))
        return self._partitioner(key, all_partitions, available)


class BaseProducer(abc.ABC):

    log = logger

    _PRODUCER_CLIENT_ID_SEQUENCE = 0

    _COMPRESSORS = {
        "gzip": (has_gzip, DefaultRecordBatch.CODEC_GZIP),
        "snappy": (has_snappy, DefaultRecordBatch.CODEC_SNAPPY),
        "lz4": (has_lz4, DefaultRecordBatch.CODEC_LZ4),
        "zstd": (has_zstd, DefaultRecordBatch.CODEC_ZSTD),
    }

    _closed = None  # Serves as an uninitialized flag for __del__
    _source_traceback = None

    def __init__(self, 
                 *, 
                 loop, 
                 bootstrap_servers='localhost',
                 client_id=None,
                 metadata_max_age_ms=300000, 
                 request_timeout_ms=40000,
                 api_version='auto', 
                 acks=_missing,
                 key_serializer=None, 
                 value_serializer=None,
                 compression_type=None, 
                 max_batch_size=16384,
                 partitioner=_DEFAULT_PARTITIONER, 
                 max_request_size=1048576,
                 linger_ms=0, 
                 retry_backoff_ms=100, 
                 security_protocol="PLAINTEXT",
                 ssl_context=None, 
                 connections_max_idle_ms=540000,
                 enable_idempotence=False, 
                 transactional_id=None,
                 transaction_timeout_ms=60000, 
                 sasl_mechanism="PLAIN",
                 sasl_plain_password=None, 
                 sasl_plain_username=None,
                 sasl_kerberos_service_name='kafka',
                 sasl_kerberos_domain_name=None,
                 sasl_oauth_token_provider=None
    ):
        
        if loop is None:
            loop = get_running_loop()
        else:
            warnings.warn(
                "The loop argument is deprecated since 0.7.1, "
                "and scheduled for removal in 0.9.0",
                DeprecationWarning,
                stacklevel=2,
            )
        if loop.get_debug():
            self._source_traceback = traceback.extract_stack(sys._getframe(1))
        self._loop = loop
                
        if acks not in (0, 1, -1, 'all', _missing):
            raise ValueError("Invalid ACKS parameter")
        if compression_type not in ('gzip', 'snappy', 'lz4', None):
            raise ValueError("Invalid compression type!")
        if compression_type:
            checker, compression_attrs = self._COMPRESSORS[compression_type]
            if not checker():
                raise RuntimeError("Compression library for {} not found"
                                   .format(compression_type))
        else:
            compression_attrs = 0
        self._compression_attrs = compression_attrs

        if acks is _missing:
            acks = 1
        elif acks == 'all':
            acks = -1

        AIOKafkaProducer._PRODUCER_CLIENT_ID_SEQUENCE += 1
        if client_id is None:
            client_id = 'aiokafka-producer-%s' % \
                AIOKafkaProducer._PRODUCER_CLIENT_ID_SEQUENCE
            
        self._bootstrap_servers = bootstrap_servers
        self._client_id = client_id
        self._metadata_max_age_ms = metadata_max_age_ms
        self._request_timeout_ms = request_timeout_ms
        self._api_version = api_version
        self._acks = acks
        self._key_serializer = key_serializer
        self._value_serializer = value_serializer
        self._compression_type = compression_type
        self._max_batch_size = max_batch_size
        self._partitioner = partitioner
        self._max_request_size = max_request_size
        self._linger_ms = linger_ms
        self._retry_backoff_ms = retry_backoff_ms
        self._security_protocol = security_protocol
        self._ssl_context = ssl_context
        self._connections_max_idle_ms = connections_max_idle_ms
        self._transaction_timeout_ms = transaction_timeout_ms
        self._transaction_timeout_ms = transaction_timeout_ms
        self._sasl_mechanism = sasl_mechanism
        self._sasl_plain_username = sasl_plain_username
        self._sasl_plain_password = sasl_plain_password
        self._sasl_kerberos_service_name = sasl_kerberos_service_name
        self._sasl_kerberos_domain_name = sasl_kerberos_domain_name

        self.client = AIOKafkaClient(
            loop=loop, 
            bootstrap_servers=bootstrap_servers,
            client_id=client_id, 
            metadata_max_age_ms=metadata_max_age_ms,
            request_timeout_ms=request_timeout_ms,
            retry_backoff_ms=retry_backoff_ms,
            api_version=api_version, 
            security_protocol=security_protocol,
            ssl_context=ssl_context,
            connections_max_idle_ms=connections_max_idle_ms,
            sasl_mechanism=sasl_mechanism,
            sasl_plain_username=sasl_plain_username,
            sasl_plain_password=sasl_plain_password,
            sasl_kerberos_service_name=sasl_kerberos_service_name,
            sasl_kerberos_domain_name=sasl_kerberos_domain_name,
            sasl_oauth_token_provider=sasl_oauth_token_provider,
        )
        self._metadata = self.client.cluster
        self._closed = False

    # Warn if producer was not closed properly
    # We don't attempt to close the Consumer, as __del__ is synchronous
    def __del__(self, _warnings=warnings):
        if self._closed is False:
            _warnings.warn(
                f"Unclosed AIOKafkaProducer {self!r}",
                ResourceWarning,
                source=self,
            )
            context = {
                "producer": self,
                "message": "Unclosed AIOKafkaProducer",
            }
            if self._source_traceback is not None:
                context["source_traceback"] = self._source_traceback
            self._loop.call_exception_handler(context)

    @abc.abstractmethod
    def _on_set_api_version(self, api_version):
        ...

    @abc.abstractmethod
    def _message_accumulator_for(self, transactional_id, tp):
        ...

    @abc.abstractmethod
    def _transactional_id_or_default(self, transactional_id):
        ...

    @abc.abstractmethod
    def _verify_txn_started(self, transactional_id):
        ...

    @abc.abstractmethod
    def _wait_for_sender(self):
        ...

    @abc.abstractmethod
    def _ensure_transactional(self):
        ...

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, type, value, traceback):
        await self.stop()

    async def start(self):
        """Connect to Kafka cluster and check server version"""
        self.log.debug("Starting the Kafka producer")  # trace
        await self.client.bootstrap()
        if self._closed:
            return
        api_version = self.client.api_version

        self._verify_api_version(api_version)
        await self._start_sender()
        self._on_set_api_version(api_version)

        self._producer_magic = 0 if api_version < (0, 10) else 1
        self.log.debug("Kafka producer started")

    def _verify_api_version(self, api_version):
        if self._compression_type == "lz4":
            assert self.client.api_version >= (0, 8, 2), (
                "LZ4 Requires >= Kafka 0.8.2 Brokers"
            )  # fmt: skip
        elif self._compression_type == "zstd":
            assert self.client.api_version >= (2, 1, 0), (
                "Zstd Requires >= Kafka 2.1.0 Brokers"
            )  # fmt: skip

    async def stop(self):
        """Flush all pending data and close all connections to kafka cluster"""
        if self._closed:
            return
        self._closed = True

        await self._wait_for_sender()

        await self.client.close()
        self.log.debug("The Kafka producer has closed.")

    async def partitions_for(self, topic):
        """Returns set of all known partitions for the topic."""
        return await self.client._wait_on_metadata(topic)

    def _serialize(self, topic, key, value):
        if self._key_serializer:
            serialized_key = self._key_serializer(key)
        else:
            serialized_key = key
        if self._value_serializer:
            serialized_value = self._value_serializer(value)
        else:
            serialized_value = value

        message_size = LegacyRecordBatchBuilder.record_overhead(
            self._producer_magic)
        if serialized_key is not None:
            message_size += len(serialized_key)
        if serialized_value is not None:
            message_size += len(serialized_value)
        if message_size > self._max_request_size:
            raise MessageSizeTooLargeError(
                "The message is %d bytes when serialized which is larger than"
                " the maximum request size you have configured with the"
                " max_request_size configuration" % message_size)

        return serialized_key, serialized_value

    def _partition(self, topic, partition, key, value,
                   serialized_key, serialized_value):
        if partition is not None:
            assert partition >= 0
            assert partition in self._metadata.partitions_for_topic(topic), \
                'Unrecognized partition'
            return partition

        all_partitions = list(self._metadata.partitions_for_topic(topic))
        available = list(self._metadata.available_partitions_for_topic(topic))
        return self._partitioner(
            serialized_key, all_partitions, available)

    async def send(
        self, topic, value=None, key=None, partition=None,
        timestamp_ms=None, headers=None, transactional_id=None
    ):
        """Publish a message to a topic.

        Arguments:
            topic (str): topic where the message will be published
            value (optional): message value. Must be type bytes, or be
                serializable to bytes via configured value_serializer. If value
                is None, key is required and message acts as a 'delete'.
                See kafka compaction documentation for more details:
                http://kafka.apache.org/documentation.html#compaction
                (compaction requires kafka >= 0.8.1)
            partition (int, optional): optionally specify a partition. If not
                set, the partition will be selected using the configured
                'partitioner'.
            key (optional): a key to associate with the message. Can be used to
                determine which partition to send the message to. If partition
                is None (and producer's partitioner config is left as default),
                then messages with the same key will be delivered to the same
                partition (but if key is None, partition is chosen randomly).
                Must be type bytes, or be serializable to bytes via configured
                key_serializer.
            timestamp_ms (int, optional): epoch milliseconds (from Jan 1 1970
                UTC) to use as the message timestamp. Defaults to current time.

        Returns:
            asyncio.Future: object that will be set when message is
            processed

        Raises:
            kafka.KafkaTimeoutError: if we can't schedule this record (
                pending buffer is full) in up to `request_timeout_ms`
                milliseconds.

        Note:
            The returned future will wait based on `request_timeout_ms`
            setting. Cancelling the returned future **will not** stop event
            from being sent, but cancelling the ``send`` coroutine itself
            **will**.
        """
        assert value is not None or self.client.api_version >= (0, 8, 1), (
            'Null messages require kafka >= 0.8.1')
        assert not (value is None and key is None), \
            'Need at least one: key or value'
        transactional_id = self._transactional_id_or_default(transactional_id)

        # first make sure the metadata for the topic is available
        await self.client._wait_on_metadata(topic)

        # Ensure transaction is started and not committing
        self._verify_txn_started(transactional_id)

        if headers is not None:
            if self.client.api_version < (0, 11):
                raise UnsupportedVersionError(
                    "Headers not supported before Kafka 0.11")
        else:
            # Record parser/builder support only list type, no explicit None
            headers = []

        key_bytes, value_bytes = self._serialize(topic, key, value)
        partition = self._partition(topic, partition, key, value,
                                    key_bytes, value_bytes)

        tp = TopicPartition(topic, partition)
        self.log.debug("Sending (key=%s value=%s) to %s", key, value, tp)

        message_accumulator = self._message_accumulator_for(
            transactional_id, tp)
        fut = await message_accumulator.add_message(
            tp, key_bytes, value_bytes, self._request_timeout_ms / 1000,
            timestamp_ms=timestamp_ms, headers=headers)
        return fut

    async def send_and_wait(self, topic, value=None, key=None, partition=None,
                      timestamp_ms=None):
        """Publish a message to a topic and wait the result"""
        future = await self.send(
            topic, value, key, partition, timestamp_ms)
        return (await future)
    
class MultiTXNProducer(BaseProducer):
    """Kafka producer using :pypi:`aiokafka` with support for multi-transactions."""

    _transactions: MutableMapping[str, TransactionManager]
    _accumulators: MutableMapping[str, MessageAccumulator]
    _senders: MutableMapping[str, Sender]
    _received_api_version: str = None

    def __init__(self, *,
                 loop,
                 bootstrap_servers='localhost',
                 acks=_missing,
                 enable_idempotence=False,
                 transactional_id=None,
                 transaction_timeout_ms=60000,
                 **kwargs):
        if acks is _missing:
            acks = -1
        elif acks not in ('all', -1):
            raise ValueError(
                "acks={} not supported with MultiTXNProducer"
                .format(acks))

        super().__init__(
            loop=loop,
            bootstrap_servers=bootstrap_servers,
            acks=acks,
            transaction_timeout_ms=transaction_timeout_ms,
            **kwargs,
        )
        self._transactions = {}
        self._accumulators = {}
        self._senders = {}

        self._message_accumulator = MessageAccumulator(
            self._metadata, 
            self._max_batch_size, 
            self._compression_attrs,
            self._request_timeout_ms / 1000,
            txn_manager=None,
            loop=self._loop)

        self._sender = Sender(
            self.client,
            txn_manager=None,
            acks=self._acks,
            retry_backoff_ms=self._retry_backoff_ms,
            linger_ms=self._linger_ms,
            message_accumulator=self._message_accumulator,
            request_timeout_ms=self._request_timeout_ms,
        )

    async def commit(
        self,
        tid_to_offset_map: Mapping[str, Mapping[TopicPartition, int]],
        group_id: str,
        start_new_transaction: bool = True
    ) -> None:
        for transactional_id, offsets in tid_to_offset_map.items():
            self.log.debug('+COMMIT %r %r' % (transactional_id, offsets))
            try:
                await self._commit(
                    transactional_id, offsets, group_id,
                    start_new_transaction=start_new_transaction,
                )
                self.log.debug('-COMMIT %r %r' % (transactional_id, offsets))
            except Exception as exc:
                self.log.error(
                    f"Failed to commit transaction {transactional_id}: {exc}")
                raise

    async def _commit(
        self, transactional_id, offsets: Mapping[TopicPartition, int],
        group_id: str,
        start_new_transaction: bool = True
    ) -> None:
        self.log.debug('+send offsets to transaction %r' % (transactional_id,))
        await self.send_offsets_to_transaction(
            transactional_id, offsets, group_id)
        self.log.debug('-send offsets to transaction %r' % (transactional_id,))
        self.log.debug('+commit transaction %r' % (transactional_id,))
        await self.commit_transaction(transactional_id)
        self.log.debug('-commit transaction %r' % (transactional_id,))
        if start_new_transaction:
            self.log.debug('+start new transaction %r' % (transactional_id,))
            await self.begin_transaction(transactional_id)
            self.log.debug('-start new transaction %r' % (transactional_id,))

    def _on_set_api_version(self, api_version):
        self._received_api_version = api_version
        for accumulator in self._accumulators.values():
            accumulator.set_api_version(api_version)
        self._message_accumulator.set_api_version(api_version)

    def _message_accumulator_for(self, transactional_id, tp):
        if transactional_id is None:
            return self._message_accumulator
        return self._accumulators[transactional_id]

    def _transactional_id_or_default(self, transactional_id):
        return None

    def _verify_txn_started(self, transactional_id):
        try:
            txn_manager = self._transactions[transactional_id]
        except KeyError:
            pass
        else:
            assert txn_manager.transactional_id == transactional_id
            if not txn_manager.is_in_transaction():
                raise IllegalOperation(
                    "Can't send messages while not in transaction")

    def _verify_api_version(self, api_version):
        super()._verify_api_version(api_version)
        if self.client.api_version < (0, 11):
            raise UnsupportedVersionError(
                "MultiTXNProducer available only for Broker version 0.11"
                " and above")

    async def flush(self):
        """Wait untill all batches are Delivered and futures resolved"""
        await asyncio.gather(
            self._message_accumulator.flush(),
            *[acc.flush() for acc in self._accumulators.values()],
        )

    async def _wait_for_sender(self):
        senders = self._senders
        accumulators = self._accumulators
        await asyncio.gather(
            self._wait_for_sender1(self._sender, self._message_accumulator),
            *[self._wait_for_sender1(senders[transactional_id], accumulator)
              for transactional_id, accumulator in accumulators.items()],
        )

    async def _wait_for_sender1(self, sender, accumulator):
        # If the sender task is down there is no way for accumulator to flush
        if sender is not None and sender.sender_task is not None:
            await asyncio.wait(
                [
                    create_task(accumulator.close()),
                    sender.sender_task,
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )

            await sender.close()

    def _ensure_transactional(self):
        ...

    async def _start_sender(self):
        await self._sender.start()

    async def _init_transaction(self, tid):
        txn_manager = self._transactions[tid] = TransactionManager(
            tid, self._transaction_timeout_ms,
        )
        accumulator = self._accumulators[tid] = MessageAccumulator(
            self._metadata,
            self._max_batch_size,
            self._compression_attrs,
            self._request_timeout_ms / 1000,
            txn_manager=txn_manager,
            loop=self._loop,
        )
        accumulator.set_api_version(self._received_api_version)
        sender = self._senders[tid] = Sender(
            self.client,
            acks=self._acks,
            txn_manager=txn_manager,
            retry_backoff_ms=self._retry_backoff_ms,
            linger_ms=self._linger_ms,
            message_accumulator=accumulator,
            request_timeout_ms=self._request_timeout_ms,
        )
        await sender.start()
        return txn_manager

    async def begin_transaction(self, transactional_id):
        self.log.debug(
            "Beginning a new transaction for id %s", transactional_id)
        txn_manager = self._transactions.get(transactional_id)
        if txn_manager is None:
            txn_manager = await self._init_transaction(transactional_id)

        await asyncio.shield(
            txn_manager.wait_for_pid(),
            loop=self._loop,
        )
        txn_manager.begin_transaction()

    async def commit_transaction(self, transactional_id):
        self.log.debug(
            "Committing transaction for id %s", transactional_id)
        txn_manager = self._transactions[transactional_id]
        txn_manager.committing_transaction()
        await asyncio.shield(
            txn_manager.wait_for_transaction_end(),
            loop=self._loop,
        )

    async def abort_transaction(self, transactional_id):
        self.log.debug(
            "Aborting transaction for id %s", transactional_id)
        txn_manager = self._transactions[transactional_id]
        txn_manager.aborting_transaction()
        await asyncio.shield(
            txn_manager.wait_for_transaction_end(),
            loop=self._loop,
        )

    async def stop_transaction(self, transactional_id):
        txn_manager = self._transactions.pop(transactional_id, None)
        accumulator = self._accumulators.pop(transactional_id, None)
        sender = self._senders.pop(transactional_id, None)
        if txn_manager is not None:
            if txn_manager.is_in_transaction():
                txn_manager.aborting_transaction()
                await asyncio.shield(
                    txn_manager.wait_for_transaction_end(),
                    loop=self._loop,
                )
        await self._wait_for_sender1(sender, accumulator)

    async def maybe_begin_transaction(self, transactional_id):
        txn_manager = self._transactions.get(transactional_id)
        if txn_manager is None:
            txn_manager = await self._init_transaction(transactional_id)
        else:
            if txn_manager.is_in_transaction():
                return
        self.log.debug(
            "Beginning a new transaction for id %s", transactional_id)
        await asyncio.shield(
            txn_manager.wait_for_pid(),
            loop=self._loop,
        )
        txn_manager.begin_transaction()

    async def send_offsets_to_transaction(
        self, transactional_id, offsets, group_id
    ):
        txn_manager = self._transactions[transactional_id]
        if not txn_manager.is_in_transaction():
            raise IllegalOperation("Not in the middle of a transaction")

        if not group_id or not isinstance(group_id, str):
            raise ValueError(group_id)

        # validate `offsets` structure
        formatted_offsets = commit_structure_validate(offsets)

        self.log.debug(
            "Begin adding offsets %s for consumer group %s to transaction",
            formatted_offsets, group_id)
        fut = txn_manager.add_offsets_to_txn(formatted_offsets, group_id)
        self.log.debug('+WAIT FOR RESPONSE OR ERROR %r' % (fut,))
        await asyncio.shield(fut, loop=self._loop)
        self.log.debug('-WAIT FOR RESPONSE OR ERROR %r' % (fut,))


class Producer(base.Producer):
    """Kafka producer using :pypi:`aiokafka`."""

    logger = logger

    allow_headers: bool = True
    _producer: Optional[aiokafka.AIOKafkaProducer] = None

    def __post_init__(self) -> None:
        self._send_on_produce_message = self.app.on_produce_message.send
        if self.partitioner is None:
            self.partitioner = DefaultPartitioner()
        if self._api_version != 'auto':
            wanted_api_version = parse_kafka_version(self._api_version)
            if wanted_api_version < (0, 11):
                self.allow_headers = False

    def _settings_default(self) -> Mapping[str, Any]:
        transport = cast(Transport, self.transport)
        return {
            'bootstrap_servers': server_list(
                transport.url, transport.default_port),
            'client_id': self.client_id,
            'acks': self.acks,
            'linger_ms': self.linger_ms,
            'max_batch_size': self.max_batch_size,
            'max_request_size': self.max_request_size,
            'compression_type': self.compression_type,
            'security_protocol': 'SSL' if self.ssl_context else 'PLAINTEXT',
            'partitioner': self.partitioner,
            'request_timeout_ms': int(self.request_timeout * 1000),
            'api_version': self._api_version,
        }

    def _settings_auth(self) -> Mapping[str, Any]:
        return credentials_to_aiokafka_auth(
            self.credentials, self.ssl_context)

    async def begin_transaction(self, transactional_id: str) -> None:
        """Begin transaction by id."""
        await self._ensure_producer().begin_transaction(transactional_id)

    async def commit_transaction(self, transactional_id: str) -> None:
        """Commit transaction by id."""
        await self._ensure_producer().commit_transaction(transactional_id)

    async def abort_transaction(self, transactional_id: str) -> None:
        """Abort and rollback transaction by id."""
        await self._ensure_producer().abort_transaction(transactional_id)

    async def stop_transaction(self, transactional_id: str) -> None:
        """Stop transaction by id."""
        await self._ensure_producer().stop_transaction(transactional_id)

    async def maybe_begin_transaction(self, transactional_id: str) -> None:
        """Begin transaction (if one does not already exist)."""
        await self._ensure_producer().maybe_begin_transaction(transactional_id)

    async def commit_transactions(
            self,
            tid_to_offset_map: Mapping[str, Mapping[TP, int]],
            group_id: str,
            start_new_transaction: bool = True) -> None:
        """Commit transactions."""
        await self._ensure_producer().commit(
            tid_to_offset_map, group_id,
            start_new_transaction=start_new_transaction,
        )

    def _settings_extra(self) -> Mapping[str, Any]:
        if self.app.in_transaction:
            return {'acks': 'all'}
        return {}

    def _new_producer(self) -> aiokafka.AIOKafkaProducer:
        return self._producer_type(
            loop=self.loop,
            **{**self._settings_default(),
               **self._settings_auth(),
               **self._settings_extra()},
        )

    @property
    def _producer_type(self) -> Type[BaseProducer]:
        if self.app.in_transaction:
            return MultiTXNProducer
        return aiokafka.AIOKafkaProducer

    async def create_topic(self,
                           topic: str,
                           partitions: int,
                           replication: int,
                           *,
                           config: Mapping[str, Any] = None,
                           timeout: Seconds = 20.0,
                           retention: Seconds = None,
                           compacting: bool = None,
                           deleting: bool = None,
                           ensure_created: bool = False) -> None:
        """Create/declare topic on server."""
        _retention = (int(want_seconds(retention) * 1000.0)
                      if retention else None)
        producer = self._ensure_producer()
        await cast(Transport, self.transport)._create_topic(
            self,
            producer.client,
            topic,
            partitions,
            replication,
            config=config,
            timeout=int(want_seconds(timeout) * 1000.0),
            retention=_retention,
            compacting=compacting,
            deleting=deleting,
            ensure_created=ensure_created,
        )
        await producer.client.force_metadata_update()  # Fixes #499

    def _ensure_producer(self) -> BaseProducer:
        if self._producer is None:
            raise NotReady('Producer service not yet started')
        return self._producer

    async def on_start(self) -> None:
        """Call when producer starts."""
        await super().on_start()
        producer = self._producer = self._new_producer()
        self.beacon.add(producer)
        await producer.start()

    async def on_stop(self) -> None:
        """Call when producer stops."""
        await super().on_stop()
        cast(Transport, self.transport)._topic_waiters.clear()
        producer, self._producer = self._producer, None
        if producer is not None:
            await producer.stop()

    async def send(self, topic: str, key: Optional[bytes],
                   value: Optional[bytes],
                   partition: Optional[int],
                   timestamp: Optional[float],
                   headers: Optional[HeadersArg],
                   *,
                   transactional_id: str = None) -> Awaitable[RecordMetadata]:
        """Schedule message to be transmitted by producer."""
        producer = self._ensure_producer()
        if headers is not None:
            if isinstance(headers, Mapping):
                headers = list(headers.items())
        self._send_on_produce_message(
            key=key, value=value,
            partition=partition,
            timestamp=timestamp,
            headers=headers,
        )
        if headers is not None and not self.allow_headers:
            headers = None
        timestamp_ms = int(timestamp * 1000.0) if timestamp else timestamp
        try:
            return cast(Awaitable[RecordMetadata], await producer.send(
                topic, value,
                key=key,
                partition=partition,
                timestamp_ms=timestamp_ms,
                headers=headers,
                transactional_id=transactional_id,
            ))
        except KafkaError as exc:
            raise ProducerSendError(f'Error while sending: {exc!r}') from exc

    async def send_and_wait(self, topic: str, key: Optional[bytes],
                            value: Optional[bytes],
                            partition: Optional[int],
                            timestamp: Optional[float],
                            headers: Optional[HeadersArg],
                            *,
                            transactional_id: str = None) -> RecordMetadata:
        """Send message and wait for it to be transmitted."""
        fut = await self.send(
            topic,
            key=key,
            value=value,
            partition=partition,
            timestamp=timestamp,
            headers=headers,
            transactional_id=transactional_id,
        )
        return await fut

    async def flush(self) -> None:
        """Wait for producer to finish transmitting all buffered messages."""
        await self.buffer.flush()
        if self._producer is not None:
            await self._producer.flush()

    def key_partition(self, topic: str, key: bytes) -> TP:
        """Hash key to determine partition destination."""
        producer = self._ensure_producer()
        partition = producer._partition(
            topic,
            partition=None,
            key=None,
            value=None,
            serialized_key=key,
            serialized_value=None,
        )
        return TP(topic, partition)

    def supports_headers(self) -> bool:
        """Return :const:`True` if message headers are supported."""
        producer = self._ensure_producer()
        client = producer.client
        if client is None:
            raise NotReady('Producer client not yet connected')
        return client.api_version >= (0, 11)


class Transport(base.Transport):
    """Kafka transport using :pypi:`aiokafka`."""

    Consumer: ClassVar[Type[ConsumerT]]
    Consumer = Consumer
    Producer: ClassVar[Type[ProducerT]]
    Producer = Producer

    default_port = 9092
    driver_version = f'aiokafka={aiokafka.__version__}'

    _topic_waiters: MutableMapping[str, StampedeWrapper]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._topic_waiters = {}

    def _topic_config(self,
                      retention: int = None,
                      compacting: bool = None,
                      deleting: bool = None) -> MutableMapping[str, Any]:
        config: MutableMapping[str, Any] = {}
        cleanup_flags: Set[str] = set()
        if compacting:
            cleanup_flags |= {'compact'}
        if deleting:
            cleanup_flags |= {'delete'}
        if cleanup_flags:
            config['cleanup.policy'] = ','.join(sorted(cleanup_flags))
        if retention:
            config['retention.ms'] = retention
        return config

    async def _create_topic(self,
                            owner: Service,
                            client: aiokafka.AIOKafkaClient,
                            topic: str,
                            partitions: int,
                            replication: int,
                            **kwargs: Any) -> None:
        assert topic is not None
        try:
            wrap = self._topic_waiters[topic]
        except KeyError:
            wrap = self._topic_waiters[topic] = StampedeWrapper(
                self._really_create_topic,
                owner,
                client,
                topic,
                partitions,
                replication,
                loop=asyncio.get_event_loop(), **kwargs)
        try:
            await wrap()
        except Exception:
            self._topic_waiters.pop(topic, None)
            raise

    async def _get_controller_node(
            self,
            owner: Service,
            client: aiokafka.AIOKafkaClient,
            timeout: int = 30000) -> Optional[int]:  # pragma: no cover
        nodes = [broker.nodeId for broker in client.cluster.brokers()]
        for node_id in nodes:
            if node_id is None:
                raise NotReady('Not connected to Kafka Broker')
            request = MetadataRequest_v1([])
            wait_result = await owner.wait(
                client.send(node_id, request),
                timeout=timeout,
            )
            if wait_result.stopped:
                owner.log.info('Shutting down - skipping creation.')
                return None
            response = wait_result.result
            return response.controller_id
        raise Exception('Controller node not found')

    async def _really_create_topic(
            self,
            owner: Service,
            client: aiokafka.AIOKafkaClient,
            topic: str,
            partitions: int,
            replication: int,
            *,
            config: Mapping[str, Any] = None,
            timeout: int = 30000,
            retention: int = None,
            compacting: bool = None,
            deleting: bool = None,
            ensure_created: bool = False) -> None:  # pragma: no cover
        owner.log.info('Creating topic %r', topic)

        if topic in client.cluster.topics():
            owner.log.debug('Topic %r exists, skipping creation.', topic)
            return

        protocol_version = 1
        extra_configs = config or {}
        config = self._topic_config(retention, compacting, deleting)
        config.update(extra_configs)

        controller_node = await self._get_controller_node(owner, client,
                                                          timeout=timeout)
        owner.log.debug('Found controller: %r', controller_node)

        if controller_node is None:
            if owner.should_stop:
                owner.log.info('Shutting down hence controller not found')
                return
            else:
                raise Exception('Controller node is None')

        request = CreateTopicsRequest[protocol_version](
            [(topic, partitions, replication, [], list(config.items()))],
            timeout,
            False,
        )
        wait_result = await owner.wait(
            client.send(controller_node, request),
            timeout=timeout,
        )
        if wait_result.stopped:
            owner.log.debug('Shutting down - skipping creation.')
            return
        response = wait_result.result

        assert len(response.topic_error_codes), 'single topic'

        _, code, reason = response.topic_error_codes[0]

        if code != 0:
            if not ensure_created and code == TopicExistsError.errno:
                owner.log.debug(
                    'Topic %r exists, skipping creation.', topic)
                return
            elif code == NotControllerError.errno:
                raise RuntimeError(f'Invalid controller: {controller_node}')
            else:
                raise for_code(code)(
                    f'Cannot create topic: {topic} ({code}): {reason}')
        else:
            owner.log.info('Topic %r created.', topic)
            return


def credentials_to_aiokafka_auth(credentials: CredentialsT = None,
                                 ssl_context: Any = None) -> Mapping:
    if credentials is not None:
        if isinstance(credentials, SSLCredentials):
            return {
                'security_protocol': credentials.protocol.value,
                'ssl_context': credentials.context,
            }
        elif isinstance(credentials, SASLCredentials):
            return {
                'security_protocol': credentials.protocol.value,
                'sasl_mechanism': credentials.mechanism.value,
                'sasl_plain_username': credentials.username,
                'sasl_plain_password': credentials.password,
                'ssl_context': credentials.ssl_context,
            }
        elif isinstance(credentials, GSSAPICredentials):
            return {
                'security_protocol': credentials.protocol.value,
                'sasl_mechanism': credentials.mechanism.value,
                'sasl_kerberos_service_name':
                    credentials.kerberos_service_name,
                'sasl_kerberos_domain_name':
                    credentials.kerberos_domain_name,
                'ssl_context': credentials.ssl_context,
            }
        else:
            raise ImproperlyConfigured(
                f'aiokafka does not support {credentials}')
    elif ssl_context is not None:
        return {
            'security_protocol': 'SSL',
            'ssl_context': ssl_context,
        }
    else:
        return {'security_protocol': 'PLAINTEXT'}
