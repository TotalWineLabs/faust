import abc
import inspect
import logging
import socket
import ssl
import typing
import warnings
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Type,
    Union,
    cast,
)
from uuid import uuid4

from mode import Seconds, SupervisorStrategyT, want_seconds
from mode.utils.imports import SymbolArg, symbol_by_name
from mode.utils.logging import Severity
from yarl import URL

from faust.exceptions import AlreadyConfiguredWarning, ImproperlyConfigured
from faust.utils.urls import urllist

from ._env import DATADIR, WEB_BIND, WEB_PORT, WEB_TRANSPORT
from .agents import AgentT
from .assignor import LeaderAssignorT, PartitionAssignorT
from .auth import CredentialsArg, CredentialsT, to_credentials
from .codecs import CodecArg
from .enums import ProcessingGuarantee
from .events import EventT
from .router import RouterT
from .sensors import SensorT
from .serializers import RegistryT, SchemaT
from .streams import StreamT
from .transports import PartitionerT, SchedulingStrategyT
from .tables import GlobalTableT, TableManagerT, TableT
from .topics import TopicT
from .web import HttpClientT, ResourceOptions

if typing.TYPE_CHECKING:
    from .worker import Worker as _WorkerT
else:
    class _WorkerT: ...      # noqa

__all__ = ['AutodiscoverArg', 'Settings']

W_ALREADY_CONFIGURED = '''\
App is already configured.

Reconfiguring late may mean parts of your program are still
using the old configuration.

Code such as:

app.conf.config_from_object('my.config')

Should appear before calling app.topic/@app.agent/etc.
'''

W_ALREADY_CONFIGURED_KEY = '''\
Setting new value for configuration key {key!r} that was already used.

Reconfiguring late may mean parts of your program are still
using the old value of {old_value!r}.

Code such as:

app.conf.{key} = {value!r}

Should appear before calling app.topic/@app.agent/etc.
'''


# XXX mypy borks if we do `from faust import __version__`
faust_version: str = symbol_by_name('faust:__version__')

TIMEZONE = timezone.utc

#: Broker URL, used as default for :setting:`broker`.
BROKER_URL = 'kafka://localhost:9092'

#: Default transport used when no scheme specified.
DEFAULT_BROKER_SCHEME = 'kafka'

#: Table storage URL, used as default for :setting:`store`.
STORE_URL = 'memory://'

#: Cache storage URL, used as default for :setting:`cache`.
CACHE_URL = 'memory://'

#: Web driver URL, used as default for :setting:`web`.
WEB_URL = 'aiohttp://'

PROCESSING_GUARANTEE = ProcessingGuarantee.AT_LEAST_ONCE

#: Table state directory path used as default for :setting:`tabledir`.
#: This path will be treated as relative to datadir, unless the provided
#: poth is absolute.
TABLEDIR = 'tables'

#: Path to agent class, used as default for :setting:`Agent`.
AGENT_TYPE = 'faust.Agent'

#: Default agent supervisor type, used as default for
#: :setting:`agent_supervisor`.
AGENT_SUPERVISOR_TYPE = 'mode.OneForOneSupervisor'

CONSUMER_SCHEDULER_TYPE = 'faust.transport.utils.DefaultSchedulingStrategy'

#: Default event class type, used as default for :setting:`Event`.
EVENT_TYPE = 'faust.Event'

#: Default schema class type, used as default for :setting:`Schema`.
SCHEMA_TYPE = 'faust.Schema'

#: Path to stream class, used as default for :setting:`Stream`.
STREAM_TYPE = 'faust.Stream'

#: Path to table manager class, used as default for :setting:`TableManager`.
TABLE_MANAGER_TYPE = 'faust.tables.TableManager'

#: Path to table class, used as default for :setting:`Table`.
TABLE_TYPE = 'faust.Table'

#: Tables keep a cache of key to partition number. This value
#: configures the maximum size of that cache.
TABLE_KEY_INDEX_SIZE = 1000

#: Path to "table of sets" class, used as default for :setting:`SetTable`.
SET_TABLE_TYPE = 'faust.SetTable'

#: Path to global table class, used as default for :setting:`GlobalTable`.
GLOBAL_TABLE_TYPE = 'faust.GlobalTable'

#: Path to "global table of sets" class,
#: used as default for :setting:`SetGlobalTable`.
SET_GLOBAL_TABLE_TYPE = 'faust.SetGlobalTable'

#: Path to serializer registry class, used as the default for
#: :setting:`Serializers`.
REGISTRY_TYPE = 'faust.serializers.Registry'

#: Path to worker class, providing the default for :setting:`Worker`.
WORKER_TYPE = 'faust.worker.Worker'

#: Path to partition assignor class, providing the default for
#: :setting:`PartitionAssignor`.
PARTITION_ASSIGNOR_TYPE = 'faust.assignor:PartitionAssignor'

#: Path to leader assignor class, providing the default for
#: :setting:`LeaderAssignor`.
LEADER_ASSIGNOR_TYPE = 'faust.assignor:LeaderAssignor'

#: Path to router class, providing the default for :setting:`Router`.
ROUTER_TYPE = 'faust.app.router:Router'

#: Path to topic class, providing the default for :setting:`Topic`.
TOPIC_TYPE = 'faust:Topic'

#: Path to HTTP client class, providing the default for :setting:`HttpClient`.
HTTP_CLIENT_TYPE = 'aiohttp.client:ClientSession'

#: Path to Monitor sensor class, providing the default for :setting:`Monitor`.
MONITOR_TYPE = 'faust.sensors:Monitor'

#: Default broker API version.
#: Used as default for
#:     + :setting:`broker_api_version`,
#:     + :setting:`consumer_api_version`,
#:     + :setting:`producer_api_version',
BROKER_API_VERSION = 'auto'

#: Default Kafka Client ID.
BROKER_CLIENT_ID = f'faust-{faust_version}'

#: Kafka consumer request timeout (``request_timeout_ms``).
BROKER_REQUEST_TIMEOUT = 90.0

#: How often we commit acknowledged messages: every n messages.
#: Used as the default value for :setting:`broker_commit_every`.
BROKER_COMMIT_EVERY = 10_000

#: How often we commit acknowledged messages on a timer.
#: Used as the default value for :setting:`broker_commit_interval`.
BROKER_COMMIT_INTERVAL = 2.8

#: Kafka consumer session timeout (``session_timeout_ms`` in seconds).
BROKER_SESSION_TIMEOUT = 60.0

#: Kafka consumer heartbeat timeout (``rebalance_timeout_ms`` in seconds).
BROKER_REBALANCE_TIMEOUT = 60.0

#: Kafka consumer heartbeat (``heartbeat_interval_ms``).
BROKER_HEARTBEAT_INTERVAL = 3.0

#: How long time it takes before we warn that the commit offset has
#: not advanced.
BROKER_LIVELOCK_SOFT = want_seconds(timedelta(minutes=5))

#: The maximum number of records returned in a single call to poll().
#: If you find that your application needs more time to process messages
#: you may want to adjust max_poll_records to tune the number of records
#: that must be handled on every loop iteration.
BROKER_MAX_POLL_RECORDS = None

#: Maximum allowed time between calls to consume messages
#: (e.g., consumer.getmany()).If this interval is exceeded the consumer
#: is considered failed and the group will rebalance in order to reassign
#: the partitions to another consumer group member. If API methods block
#: waiting for messages, that time does not count against this timeout.
#: See KIP-62 for more information.
BROKER_MAX_POLL_INTERVAL = 1000.0

#: How often we clean up expired items in windowed tables.
#: Used as the default value for :setting:`table_cleanup_interval`.
TABLE_CLEANUP_INTERVAL = 30.0

#: Prefix used for reply topics.
REPLY_TO_PREFIX = 'f-reply-'

#: Default expiry time for replies, in seconds (float).
REPLY_EXPIRES = want_seconds(timedelta(days=1))

#: Max number of messages channels/streams/topics can "prefetch".
STREAM_BUFFER_MAXSIZE = 4096

#: Number of seconds to sleep before continuing after rebalance.
#: We wait for a bit to allow for more nodes to join/leave before
#: starting to reprocess, to minimize the chance of errors and rebalancing
#: loops.
STREAM_RECOVERY_DELAY = 0.0

#: We buffer up sending messages until the
#: source topic offset related to that processing is committed.
#: This means when we do commit, we may have buffered up a LOT of messages
#: so commit frequently.
#:
#: This setting is deprecated and will be removed once transaction support
#: is added in a later version.
STREAM_PUBLISH_ON_COMMIT = False

#: Timeout (in seconds) for processing events in the stream.
#: If processing of a single event exceeds this time we log an error,
#: but do not stop processing.
#:
#: If you are seeing a warning like this you should either
#: 1) increase this timeout to allow events to take longer, or
#: 2) add a timeout to the operation so that stream processing
#:    always completed before the timeout.
#:
#: The latter is preferred for network operations such as web requests.
#: If a network service you depend on is temporarily offline you should
#: consider doing retries (sent to a separate topic).
#:
#:
#: main_topic = app.topic('main')
#: deadletter_topic = app.topic('main_deadletter')
#:
#: async def send_request(value, timeout: float = None) -> None:
#:     await app.http_client.get('http://foo.com', timeout=timeout)
#:
#: @app.agent(main_topic)
#: async def main(stream):
#:    async for value in stream:
#:        try:
#:            await send_request(value, timeout=5)
#:        except asyncio.TimeoutError:
#:            await deadletter_topic.send(value)
#:
#:
#: @app.agent(deadletter_topic)
#: async def main_deadletter(stream):
#:     async for value in stream:
#:         # wait for 30 seconds before retrying.
#:         await stream.sleep(30)
#:         await send_request(value)
STREAM_PROCESSING_TIMEOUT = 5 * 60.0

#: Maximum size of a request in bytes in the consumer.
#: Used as the default value for :setting:`max_fetch_size`.
#: Note: This is PER PARTITION, so a limit of 1Mb when your
#: workers consume from 10 topics having 100 partitions each
#: means a fetch request can be 10 * 100 * 1Mb.
#: This limit being effectively too high will cause
#: rebalancing issues, so keep it low enough to account
#: for many partitions.
CONSUMER_MAX_FETCH_SIZE = 1024 ** 2

#: Where the consumer should start reading offsets when there is no initial
#: offset, or the stored offset no longer exists, e.g. when starting a new
#: consumer for the first time. Options include 'earliest', 'latest', 'none'.
#: Used as default value for :setting:`consumer_auto_offset_reset`.
CONSUMER_AUTO_OFFSET_RESET = 'earliest'

#: Minimum time to batch before sending out messages from the producer.
#: Used as the default value for :setting:`linger_ms`.
PRODUCER_LINGER_MS = 0

#: Maximum size of buffered data per partition in bytes in the producer.
#: Used as the default value for :setting:`max_batch_size`.
PRODUCER_MAX_BATCH_SIZE = 16384

#: Maximum size of a request in bytes in the producer.
#: Used as the default value for :setting:`max_request_size`.
PRODUCER_MAX_REQUEST_SIZE = 1_000_000

#: The compression type for all data generated by
#: the producer. Valid values are 'gzip', 'snappy', 'lz4', or None.
#: Compression is of full batches of data, so the efficacy of batching
#: will also impact the compression ratio (more batching means better
#: compression). Default: None.
PRODUCER_COMPRESSION_TYPE: Optional[str] = None

#: Producer request timeout is the number of seconds before we give
#: up sending message batches.
PRODUCER_REQUEST_TIMEOUT: float = 1200.0  # 20 minutes.

#: The number of acknowledgments the producer requires the leader to have
#: received before considering a request complete. This controls the
#: durability of records that are sent. The following settings are common:
#:     0: Producer will not wait for any acknowledgment from the server
#:         at all. The message will immediately be considered sent.
#:         (Not recommended)
#:     1: The broker leader will write the record to its local log but
#:         will respond without awaiting full acknowledgment from all
#:         followers. In this case should the leader fail immediately
#:         after acknowledging the record but before the followers have
#:         replicated it then the record will be lost.
#:     -1: The broker leader will wait for the full set of in-sync
#:         replicas to acknowledge the record. This guarantees that the
#:         record will not be lost as long as at least one in-sync replica
#:         remains alive. This is the strongest available guarantee.
PRODUCER_ACKS = -1

#: Set of settings added for backwards compatibility
SETTINGS_COMPAT: Set[str] = {
    'stream_ack_exceptions',
    'stream_ack_cancelled_tasks',
    'url',
}

#: Set of :func:`inspect.signature` parameter types to ignore
#: when building the list of supported seting names.
SETTINGS_SKIP: Set[inspect._ParameterKind] = {
    inspect.Parameter.VAR_KEYWORD,
}

#: Memory utilization threshold that triggers a faust worker to
#: shutdown. Applicable only when faust is deployed using a `Worker`.
#:
#: An app will abruptly terminate if it exceeds it's allocated memory limit.
#: This may have unintendent consequences, especially when sateful Tables are
#: involved. This settings allows for graceful shutdown when high memory utilization
#: is detected. This setting is disabled by default. To enable, set percent value
#: between >0.0 and <=1.0
WORKER_SHUTDOWN_MEMORY_UTILIZATION_PERCENT = 0.0

#: Memory utilization (bytes) threshold that triggers a faust worker to
#: shutdown. Applicable only when faust is deployed using a `Worker`.
WORKER_SHUTDOWN_MEMORY_UTILIZATION_BYTES = 0.0

AutodiscoverArg = Union[
    bool,
    Iterable[str],
    Callable[[], Iterable[str]],
]


class Settings(abc.ABC):
    debug: bool = False
    autodiscover: AutodiscoverArg = False
    broker_client_id: str = BROKER_CLIENT_ID
    broker_api_version: str = BROKER_API_VERSION
    broker_commit_every: int = BROKER_COMMIT_EVERY
    broker_check_crcs: bool = True
    broker_max_poll_interval: float = BROKER_MAX_POLL_INTERVAL
    _broker_credentials: Optional[CredentialsT] = None
    id_format: str = '{id}-v{self.version}'
    key_serializer: CodecArg = 'raw'
    value_serializer: CodecArg = 'json'
    reply_to: str
    reply_to_prefix: str = REPLY_TO_PREFIX
    reply_create_topic: bool = False
    stream_buffer_maxsize: int = STREAM_BUFFER_MAXSIZE
    stream_wait_empty: bool = True
    stream_ack_cancelled_tasks: bool = True
    stream_ack_exceptions: bool = True
    stream_publish_on_commit: bool = STREAM_PUBLISH_ON_COMMIT
    ssl_context: Optional[ssl.SSLContext] = None
    table_standby_replicas: int = 1
    table_key_index_size: int = TABLE_KEY_INDEX_SIZE
    topic_replication_factor: int = 1
    topic_partitions: int = 8  # noqa: E704
    topic_allow_declare: bool = True
    topic_disable_leader: bool = False
    logging_config: Optional[Dict] = None
    loghandlers: List[logging.Handler]
    producer_linger_ms: int = PRODUCER_LINGER_MS
    producer_max_batch_size: int = PRODUCER_MAX_BATCH_SIZE
    producer_acks: int = PRODUCER_ACKS
    producer_max_request_size: int = PRODUCER_MAX_REQUEST_SIZE
    consumer_group_instance_id: Optional[str] = None
    consumer_max_fetch_size: int = CONSUMER_MAX_FETCH_SIZE
    consumer_auto_offset_reset: str = CONSUMER_AUTO_OFFSET_RESET
    producer_compression_type: Optional[str] = PRODUCER_COMPRESSION_TYPE
    timezone: tzinfo = TIMEZONE
    web_enabled: bool
    web_bind: str = WEB_BIND
    web_port: int = WEB_PORT
    web_host: str = socket.gethostname()
    web_in_thread: bool = False
    web_cors_options: Optional[Mapping[str, ResourceOptions]] = None
    worker_redirect_stdouts: bool = True
    worker_redirect_stdouts_level: Severity = 'WARN'
    worker_shutdown_memory_utilization_percent: float = WORKER_SHUTDOWN_MEMORY_UTILIZATION_PERCENT
    worker_shutdown_memory_utilization_bytes: float = WORKER_SHUTDOWN_MEMORY_UTILIZATION_BYTES

    _id: str
    _origin: Optional[str] = None
    _name: str
    _version: int = 1
    _broker: List[URL]
    _broker_consumer: Optional[List[URL]] = None
    _broker_producer: Optional[List[URL]] = None
    _store: URL
    _cache: URL
    _web: URL
    _canonical_url: URL = cast(URL, None)
    _datadir: Path
    _tabledir: Path
    _processing_guarantee: ProcessingGuarantee = PROCESSING_GUARANTEE
    _agent_supervisor: Type[SupervisorStrategyT]
    _broker_request_timeout: float = BROKER_REQUEST_TIMEOUT
    _broker_session_timeout: float = BROKER_SESSION_TIMEOUT
    _broker_rebalance_timeout: float = BROKER_REBALANCE_TIMEOUT
    _broker_heartbeat_interval: float = BROKER_HEARTBEAT_INTERVAL
    _broker_commit_interval: float = BROKER_COMMIT_INTERVAL
    _broker_commit_livelock_soft_timeout: float = BROKER_LIVELOCK_SOFT
    _broker_max_poll_records: Optional[int] = BROKER_MAX_POLL_RECORDS
    _consumer_api_version: Optional[str] = None
    _producer_api_version: Optional[str] = None
    _producer_partitioner: Optional[PartitionerT] = None
    _producer_request_timeout: float = PRODUCER_REQUEST_TIMEOUT
    _stream_recovery_delay: float = STREAM_RECOVERY_DELAY
    _stream_processing_timeout: float = STREAM_PROCESSING_TIMEOUT
    _table_cleanup_interval: float = TABLE_CLEANUP_INTERVAL
    _reply_expires: float = REPLY_EXPIRES
    _web_transport: URL = WEB_TRANSPORT
    _Agent: Type[AgentT]
    _ConsumerScheduler: Type[SchedulingStrategyT]
    _Event: Type[EventT]
    _Schema: Type[SchemaT]
    _Stream: Type[StreamT]
    _Table: Type[TableT]
    _SetTable: Type[TableT]
    _GlobalTable: Type[GlobalTableT]
    _SetGlobalTable: Type[GlobalTableT]
    _TableManager: Type[TableManagerT]
    _Serializers: Type[RegistryT]
    _Worker: Type[_WorkerT]
    _PartitionAssignor: Type[PartitionAssignorT]
    _LeaderAssignor: Type[LeaderAssignorT]
    _Router: Type[RouterT]
    _Topic: Type[TopicT]
    _HttpClient: Type[HttpClientT]
    _Monitor: Type[SensorT]

    _initializing: bool = True
    _accessed: Set[str] = cast(Set[str], None)

    @classmethod
    def setting_names(cls) -> Set[str]:
        return {
            k for k, v in inspect.signature(cls).parameters.items()
            if v.kind not in SETTINGS_SKIP
        } - SETTINGS_COMPAT

    @classmethod
    def _warn_already_configured(cls) -> None:
        warnings.warn(AlreadyConfiguredWarning(W_ALREADY_CONFIGURED),
                      stacklevel=3)

    @classmethod
    def _warn_already_configured_key(cls,
                                     key: str,
                                     value: Any,
                                     old_value: Any) -> None:
        warnings.warn(
            AlreadyConfiguredWarning(W_ALREADY_CONFIGURED_KEY.format(
                key=key,
                value=value,
                old_value=old_value,
            )),
            stacklevel=3,
        )

    def __init__(  # noqa: C901
            self,
            id: str,
            *,
            debug: bool = None,
            version: int = None,
            broker: Union[str, URL, List[URL]] = None,
            broker_api_version: str = None,
            broker_client_id: str = None,
            broker_request_timeout: Seconds = None,
            broker_credentials: CredentialsArg = None,
            broker_commit_every: int = None,
            broker_commit_interval: Seconds = None,
            broker_commit_livelock_soft_timeout: Seconds = None,
            broker_session_timeout: Seconds = None,
            broker_rebalance_timeout: Seconds = None,
            broker_heartbeat_interval: Seconds = None,
            broker_check_crcs: bool = None,
            broker_max_poll_records: int = None,
            broker_max_poll_interval: int = None,
            broker_consumer: Union[str, URL, List[URL]] = None,
            broker_producer: Union[str, URL, List[URL]] = None,
            agent_supervisor: SymbolArg[Type[SupervisorStrategyT]] = None,
            store: Union[str, URL] = None,
            cache: Union[str, URL] = None,
            web: Union[str, URL] = None,
            web_enabled: bool = True,
            processing_guarantee: Union[str, ProcessingGuarantee] = None,
            timezone: tzinfo = None,
            autodiscover: AutodiscoverArg = None,
            origin: str = None,
            canonical_url: Union[str, URL] = None,
            datadir: Union[Path, str] = None,
            tabledir: Union[Path, str] = None,
            key_serializer: CodecArg = None,
            value_serializer: CodecArg = None,
            logging_config: Dict = None,
            loghandlers: List[logging.Handler] = None,
            table_cleanup_interval: Seconds = None,
            table_standby_replicas: int = None,
            table_key_index_size: int = None,
            topic_replication_factor: int = None,
            topic_partitions: int = None,
            topic_allow_declare: bool = None,
            topic_disable_leader: bool = None,
            id_format: str = None,
            reply_to: str = None,
            reply_to_prefix: str = None,
            reply_create_topic: bool = None,
            reply_expires: Seconds = None,
            ssl_context: ssl.SSLContext = None,
            stream_buffer_maxsize: int = None,
            stream_wait_empty: bool = None,
            stream_ack_cancelled_tasks: bool = None,
            stream_ack_exceptions: bool = None,
            stream_publish_on_commit: bool = None,
            stream_recovery_delay: Seconds = None,
            stream_processing_timeout: Seconds = None,
            producer_linger_ms: int = None,
            producer_max_batch_size: int = None,
            producer_acks: int = None,
            producer_max_request_size: int = None,
            producer_compression_type: str = None,
            producer_partitioner: SymbolArg[PartitionerT] = None,
            producer_request_timeout: Seconds = None,
            producer_api_version: str = None,
            consumer_group_instance_id: str = None,
            consumer_api_version: str = None,
            consumer_max_fetch_size: int = None,
            consumer_auto_offset_reset: str = None,
            web_bind: str = None,
            web_port: int = None,
            web_host: str = None,
            web_transport: Union[str, URL] = None,
            web_in_thread: bool = None,
            web_cors_options: Mapping[str, ResourceOptions] = None,
            worker_redirect_stdouts: bool = None,
            worker_redirect_stdouts_level: Severity = None,
            worker_shutdown_memory_utilization_percent: float = None,
            worker_shutdown_memory_utilization_bytes: float = None,
            Agent: SymbolArg[Type[AgentT]] = None,
            ConsumerScheduler: SymbolArg[Type[SchedulingStrategyT]] = None,
            Event: SymbolArg[Type[EventT]] = None,
            Schema: SymbolArg[Type[SchemaT]] = None,
            Stream: SymbolArg[Type[StreamT]] = None,
            Table: SymbolArg[Type[TableT]] = None,
            SetTable: SymbolArg[Type[TableT]] = None,
            GlobalTable: SymbolArg[Type[GlobalTableT]] = None,
            SetGlobalTable: SymbolArg[Type[GlobalTableT]] = None,
            TableManager: SymbolArg[Type[TableManagerT]] = None,
            Serializers: SymbolArg[Type[RegistryT]] = None,
            Worker: SymbolArg[Type[_WorkerT]] = None,
            PartitionAssignor: SymbolArg[Type[PartitionAssignorT]] = None,
            LeaderAssignor: SymbolArg[Type[LeaderAssignorT]] = None,
            Router: SymbolArg[Type[RouterT]] = None,
            Topic: SymbolArg[Type[TopicT]] = None,
            HttpClient: SymbolArg[Type[HttpClientT]] = None,
            Monitor: SymbolArg[Type[SensorT]] = None,
            # XXX backward compat (remove for Faust 1.0)
            url: Union[str, URL] = None,
            **kwargs: Any) -> None:
        object.__setattr__(self, '_accessed', None)
        self.version = version if version is not None else self._version
        self.id_format = id_format if id_format is not None else self.id_format
        if origin is not None:
            self.origin = origin
        self.id = id
        self.broker = self._first_not_none(url, broker, BROKER_URL)
        if debug is not None:
            self.debug = debug
        if broker_consumer is not None:
            # XXX have to cast to silence mypy bug
            self.broker_consumer = cast(List[URL], broker_consumer)
        if broker_producer is not None:
            # XXX have to cast to silence mypy bug
            self.broker_producer = cast(List[URL], broker_producer)
        self.ssl_context = ssl_context
        self.store = self._first_not_none(store, STORE_URL)
        self.cache = self._first_not_none(cache, CACHE_URL)
        self.web = self._first_not_none(web, WEB_URL)
        self.web_enabled = web_enabled
        if processing_guarantee is not None:
            # XXX have to cast to silence mypy bug
            self.processing_guarantee = cast(
                ProcessingGuarantee, processing_guarantee)
        if autodiscover is not None:
            self.autodiscover = autodiscover
        if broker_api_version is not None:
            self.broker_api_version = broker_api_version
        if broker_client_id is not None:
            self.broker_client_id = broker_client_id
        if canonical_url:
            # XXX have to cast to silence mypy bug
            self.canonical_url = cast(URL, canonical_url)
        # datadir is a format string that can contain e.g. {conf.id}
        # XXX have to cast to silence mypy bug
        self.datadir = cast(Path, datadir or DATADIR)
        # XXX have to cast to silence mypy bug
        self.tabledir = cast(Path, tabledir or TABLEDIR)
        if broker_credentials is not None:
            # XXX have to cast to silence mypy bug
            self.broker_credentials = cast(CredentialsT, broker_credentials)
        if broker_request_timeout is not None:
            self.broker_request_timeout = want_seconds(broker_request_timeout)
        self.broker_commit_interval = cast(float, (
            broker_commit_interval or self._broker_commit_interval))
        self.broker_commit_livelock_soft_timeout = cast(float, (
            broker_commit_livelock_soft_timeout or
            self._broker_commit_livelock_soft_timeout))
        if broker_session_timeout is not None:
            self.broker_session_timeout = want_seconds(broker_session_timeout)
        if broker_rebalance_timeout is not None:
            self.broker_rebalance_timeout = want_seconds(
                broker_rebalance_timeout)
        if broker_heartbeat_interval is not None:
            self.broker_heartbeat_interval = want_seconds(
                broker_heartbeat_interval)
        self.table_cleanup_interval = cast(float, (
            table_cleanup_interval or self._table_cleanup_interval))

        if timezone is not None:
            self.timezone = timezone

        if broker_commit_every is not None:
            self.broker_commit_every = broker_commit_every
        if broker_check_crcs is not None:
            self.broker_check_crcs = broker_check_crcs
        if broker_max_poll_records is not None:
            self.broker_max_poll_records = broker_max_poll_records
        if broker_max_poll_interval is not None:
            self.broker_max_poll_interval = broker_max_poll_interval
        if key_serializer is not None:
            self.key_serializer = key_serializer
        if value_serializer is not None:
            self.value_serializer = value_serializer
        if table_standby_replicas is not None:
            self.table_standby_replicas = table_standby_replicas
        if table_key_index_size is not None:
            self.table_key_index_size = table_key_index_size
        if topic_replication_factor is not None:
            self.topic_replication_factor = topic_replication_factor
        if topic_partitions is not None:
            self.topic_partitions = topic_partitions
        if topic_allow_declare is not None:
            self.topic_allow_declare = topic_allow_declare
        if topic_disable_leader is not None:
            self.topic_disable_leader = topic_disable_leader
        if reply_create_topic is not None:
            self.reply_create_topic = reply_create_topic
        if logging_config is not None:
            self.logging_config = logging_config
        self.loghandlers = loghandlers if loghandlers is not None else []
        if stream_buffer_maxsize is not None:
            self.stream_buffer_maxsize = stream_buffer_maxsize
        if stream_wait_empty is not None:
            self.stream_wait_empty = stream_wait_empty
        if stream_ack_cancelled_tasks is not None:
            warnings.warn(UserWarning(
                'deprecated stream_ack_cancelled_tasks will have no effect'))
            self.stream_ack_cancelled_tasks = stream_ack_cancelled_tasks
        if stream_ack_exceptions is not None:
            warnings.warn(UserWarning(
                'deprecated stream_ack_exceptions will have no effect'))
            self.stream_ack_exceptions = stream_ack_exceptions
        if stream_publish_on_commit is not None:
            self.stream_publish_on_commit = stream_publish_on_commit
        if stream_recovery_delay is not None:
            self.stream_recovery_delay = cast(float, stream_recovery_delay)
        if stream_processing_timeout is not None:
            self.stream_processing_timeout = cast(
                float, stream_processing_timeout)
        if producer_linger_ms is not None:
            self.producer_linger_ms = producer_linger_ms
        if producer_max_batch_size is not None:
            self.producer_max_batch_size = producer_max_batch_size
        if producer_acks is not None:
            self.producer_acks = producer_acks
        if producer_max_request_size is not None:
            self.producer_max_request_size = producer_max_request_size
        if producer_compression_type is not None:
            self.producer_compression_type = producer_compression_type
        if producer_partitioner is not None:
            self.producer_partitioner = producer_partitioner  # type: ignore
        if producer_request_timeout is not None:
            self.producer_request_timeout = cast(
                float, producer_request_timeout)
        if producer_api_version is not None:
            self.producer_api_version = producer_api_version
        if consumer_group_instance_id is not None:
            self.consumer_group_instance_id = consumer_group_instance_id
        if consumer_api_version is not None:
            self.consumer_api_version = consumer_api_version
        if consumer_max_fetch_size is not None:
            self.consumer_max_fetch_size = consumer_max_fetch_size
        if consumer_auto_offset_reset is not None:
            self.consumer_auto_offset_reset = consumer_auto_offset_reset
        if web_bind is not None:
            self.web_bind = web_bind
        if web_port is not None:
            self.web_port = web_port
        if web_host is not None:
            self.web_host = web_host
        if web_transport is not None:
            # XXX have to cast to silence mypy bug
            self.web_transport = cast(URL, web_transport)
        if web_in_thread is not None:
            self.web_in_thread = web_in_thread
        if web_cors_options is not None:
            self.web_cors_options = web_cors_options
        if worker_redirect_stdouts is not None:
            self.worker_redirect_stdouts = worker_redirect_stdouts
        if worker_redirect_stdouts_level is not None:
            self.worker_redirect_stdouts_level = worker_redirect_stdouts_level
        if worker_shutdown_memory_utilization_percent is not None:
            self.worker_shutdown_memory_utilization_percent = worker_shutdown_memory_utilization_percent
        if worker_shutdown_memory_utilization_bytes is not None:
            self.worker_shutdown_memory_utilization_bytes = worker_shutdown_memory_utilization_bytes

        if reply_to_prefix is not None:
            self.reply_to_prefix = reply_to_prefix
        if reply_to is not None:
            self.reply_to = reply_to
        else:
            self.reply_to = f'{self.reply_to_prefix}{uuid4()}'
        if reply_expires is not None:
            self.reply_expires = cast(float, reply_expires)

        self.GlobalTable = cast(
            Type[GlobalTableT], GlobalTable or GLOBAL_TABLE_TYPE)
        self.SetGlobalTable = cast(
            Type[GlobalTableT], SetGlobalTable or SET_GLOBAL_TABLE_TYPE)
        self.agent_supervisor = cast(
            Type[SupervisorStrategyT],
            agent_supervisor or AGENT_SUPERVISOR_TYPE)

        self.Agent = cast(Type[AgentT], Agent or AGENT_TYPE)
        self.ConsumerScheduler = cast(
            Type[SchedulingStrategyT],
            ConsumerScheduler or CONSUMER_SCHEDULER_TYPE)
        self.Event = cast(Type[EventT], Event or EVENT_TYPE)
        self.Schema = cast(Type[SchemaT], Schema or SCHEMA_TYPE)
        self.Stream = cast(Type[StreamT], Stream or STREAM_TYPE)
        self.Table = cast(Type[TableT], Table or TABLE_TYPE)
        self.SetTable = cast(Type[TableT], SetTable or SET_TABLE_TYPE)
        self.TableManager = cast(
            Type[TableManagerT],
            TableManager or TABLE_MANAGER_TYPE)
        self.Serializers = cast(Type[RegistryT], Serializers or REGISTRY_TYPE)
        self.Worker = cast(Type[_WorkerT], Worker or WORKER_TYPE)
        self.PartitionAssignor = cast(
            Type[PartitionAssignorT],
            PartitionAssignor or PARTITION_ASSIGNOR_TYPE)
        self.LeaderAssignor = cast(
            Type[LeaderAssignorT],
            LeaderAssignor or LEADER_ASSIGNOR_TYPE)
        self.Router = cast(Type[RouterT], Router or ROUTER_TYPE)
        self.Topic = cast(Type[TopicT], Topic or TOPIC_TYPE)
        self.HttpClient = cast(
            Type[HttpClientT], HttpClient or HTTP_CLIENT_TYPE)
        self.Monitor = cast(Type[SensorT], Monitor or MONITOR_TYPE)
        self.__dict__.update(kwargs)  # arbitrary configuration
        object.__setattr__(self, '_accessed', set())
        object.__setattr__(self, '_initializing', False)

    def __getattribute__(self, key: str) -> Any:
        accessed = object.__getattribute__(self, '_accessed')
        if not key.startswith('_'):
            if not object.__getattribute__(self, '_initializing'):
                accessed.add(key)
        return object.__getattribute__(self, key)

    def __setattr__(self, key: str, value: Any) -> None:
        xsd = object.__getattribute__(self, '_accessed')
        if xsd is not None and not key.startswith('_') and key in xsd:
            old_value = object.__getattribute__(self, key)
            self._warn_already_configured_key(key, value, old_value)
        object.__setattr__(self, key, value)

    def _first_not_none(self, *args: Any) -> Any:
        for v in args:
            if v is not None:
                return v

    def _prepare_id(self, id: str) -> str:
        if self.version > 1:
            return self.id_format.format(id=id, self=self)
        return id

    def _prepare_datadir(self, datadir: Union[str, Path]) -> Path:
        return self._Path(str(datadir).format(conf=self))

    def _prepare_tabledir(self, tabledir: Union[str, Path]) -> Path:
        return self._appdir_path(self._Path(tabledir))

    def _Path(self, *parts: Union[str, Path]) -> Path:
        return Path(*parts).expanduser()

    def _appdir_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.appdir / path

    @property
    def name(self) -> str:
        # name is a read-only property
        return self._name

    @property
    def id(self) -> str:
        return self._id

    @id.setter
    def id(self, name: str) -> None:
        self._name = name
        self._id = self._prepare_id(name)  # id is name+version

    @property
    def origin(self) -> Optional[str]:
        return self._origin

    @origin.setter
    def origin(self, origin: Optional[str]) -> None:
        self._origin = origin

    @property
    def version(self) -> int:
        return self._version

    @version.setter
    def version(self, version: int) -> None:
        if not version:
            raise ImproperlyConfigured(
                f'Version cannot be {version}, please start at 1')
        self._version = version

    @property
    def broker(self) -> List[URL]:
        return self._broker

    @broker.setter
    def broker(self, broker: Union[URL, str, List[URL]]) -> None:
        self._broker = urllist(broker, default_scheme=DEFAULT_BROKER_SCHEME)

    @property
    def broker_consumer(self) -> List[URL]:
        consumer = self._broker_consumer
        return consumer if consumer is not None else self.broker

    @broker_consumer.setter
    def broker_consumer(self, broker: Union[URL, str, List[URL]]) -> None:
        self._broker_consumer = urllist(
            broker, default_scheme=DEFAULT_BROKER_SCHEME)

    @property
    def broker_producer(self) -> List[URL]:
        producer = self._broker_producer
        return producer if producer is not None else self.broker

    @broker_producer.setter
    def broker_producer(self, broker: Union[URL, str, List[URL]]) -> None:
        self._broker_producer = urllist(
            broker, default_scheme=DEFAULT_BROKER_SCHEME)

    @property
    def store(self) -> URL:
        return self._store

    @store.setter
    def store(self, store: Union[URL, str]) -> None:
        self._store = URL(store)

    @property
    def web(self) -> URL:
        return self._web

    @web.setter
    def web(self, web: Union[URL, str]) -> None:
        self._web = URL(web)

    @property
    def cache(self) -> URL:
        return self._cache

    @cache.setter
    def cache(self, cache: Union[URL, str]) -> None:
        self._cache = URL(cache)

    @property
    def canonical_url(self) -> URL:
        if not self._canonical_url:
            self._canonical_url = URL(
                f'http://{self.web_host}:{self.web_port}')
        return self._canonical_url

    @canonical_url.setter
    def canonical_url(self, canonical_url: Union[URL, str]) -> None:
        self._canonical_url = URL(canonical_url)

    @property
    def datadir(self) -> Path:
        return self._datadir

    @datadir.setter
    def datadir(self, datadir: Union[Path, str]) -> None:
        self._datadir = self._prepare_datadir(datadir)

    @property
    def appdir(self) -> Path:
        return self._versiondir(self.version)

    def _versiondir(self, version: int) -> Path:
        return self.datadir / f'v{version}'

    def find_old_versiondirs(self) -> Iterable[Path]:
        for version in reversed(range(0, self.version)):
            path = self._versiondir(version)
            if path.is_dir():
                yield path

    @property
    def tabledir(self) -> Path:
        return self._tabledir

    @tabledir.setter
    def tabledir(self, tabledir: Union[Path, str]) -> None:
        self._tabledir = self._prepare_tabledir(tabledir)

    @property
    def processing_guarantee(self) -> ProcessingGuarantee:
        return self._processing_guarantee

    @processing_guarantee.setter
    def processing_guarantee(self,
                             value: Union[str, ProcessingGuarantee]) -> None:
        self._processing_guarantee = ProcessingGuarantee(value)

    @property
    def broker_credentials(self) -> Optional[CredentialsT]:
        return self._broker_credentials

    @broker_credentials.setter
    def broker_credentials(self, creds: CredentialsArg = None) -> None:
        self._broker_credentials = to_credentials(creds)

    @property
    def broker_request_timeout(self) -> float:
        return self._broker_request_timeout

    @broker_request_timeout.setter
    def broker_request_timeout(self, value: Seconds) -> None:
        self._broker_request_timeout = want_seconds(value)

    @property
    def broker_session_timeout(self) -> float:
        return self._broker_session_timeout

    @broker_session_timeout.setter
    def broker_session_timeout(self, value: Seconds) -> None:
        self._broker_session_timeout = want_seconds(value)

    @property
    def broker_rebalance_timeout(self) -> float:
        return self._broker_rebalance_timeout

    @broker_rebalance_timeout.setter
    def broker_rebalance_timeout(self, value: Seconds) -> None:
        self._broker_rebalance_timeout = want_seconds(value)

    @property
    def broker_heartbeat_interval(self) -> float:
        return self._broker_heartbeat_interval

    @broker_heartbeat_interval.setter
    def broker_heartbeat_interval(self, value: Seconds) -> None:
        self._broker_heartbeat_interval = want_seconds(value)

    @property
    def broker_commit_interval(self) -> float:
        return self._broker_commit_interval

    @broker_commit_interval.setter
    def broker_commit_interval(self, value: Seconds) -> None:
        self._broker_commit_interval = want_seconds(value)

    @property
    def broker_commit_livelock_soft_timeout(self) -> float:
        return self._broker_commit_livelock_soft_timeout

    @broker_commit_livelock_soft_timeout.setter
    def broker_commit_livelock_soft_timeout(self, value: Seconds) -> None:
        self._broker_commit_livelock_soft_timeout = want_seconds(value)

    @property
    def broker_max_poll_records(self) -> Optional[int]:
        return self._broker_max_poll_records

    @broker_max_poll_records.setter
    def broker_max_poll_records(self, value: Optional[int]) -> None:
        self._broker_max_poll_records = value

    @property
    def consumer_api_version(self) -> str:
        consumer_api_version = self._consumer_api_version
        if consumer_api_version is None:
            return self.broker_api_version
        else:
            return consumer_api_version

    @consumer_api_version.setter
    def consumer_api_version(self, version: str) -> None:
        self._consumer_api_version = version

    @property
    def producer_api_version(self) -> str:
        producer_api_version = self._producer_api_version
        if producer_api_version is None:
            return self.broker_api_version
        else:
            return producer_api_version

    @producer_api_version.setter
    def producer_api_version(self, version: str) -> None:
        self._producer_api_version = version

    @property
    def producer_partitioner(self) -> Optional[PartitionerT]:
        return self._producer_partitioner

    @producer_partitioner.setter
    def producer_partitioner(
            self, handler: Optional[SymbolArg[PartitionerT]]) -> None:
        self._producer_partitioner = symbol_by_name(handler)

    @property
    def producer_request_timeout(self) -> float:
        return self._producer_request_timeout

    @producer_request_timeout.setter
    def producer_request_timeout(self, timeout: Seconds) -> None:
        self._producer_request_timeout = want_seconds(timeout)

    @property
    def table_cleanup_interval(self) -> float:
        return self._table_cleanup_interval

    @table_cleanup_interval.setter
    def table_cleanup_interval(self, value: Seconds) -> None:
        self._table_cleanup_interval = want_seconds(value)

    @property
    def reply_expires(self) -> float:
        return self._reply_expires

    @reply_expires.setter
    def reply_expires(self, reply_expires: Seconds) -> None:
        self._reply_expires = want_seconds(reply_expires)

    @property
    def stream_recovery_delay(self) -> float:
        return self._stream_recovery_delay

    @stream_recovery_delay.setter
    def stream_recovery_delay(self, delay: Seconds) -> None:
        self._stream_recovery_delay = want_seconds(delay)

    @property
    def stream_processing_timeout(self) -> float:
        return self._stream_processing_timeout

    @stream_processing_timeout.setter
    def stream_processing_timeout(self, timeout: Seconds) -> None:
        self._stream_processing_timeout = want_seconds(timeout)

    @property
    def agent_supervisor(self) -> Type[SupervisorStrategyT]:
        return self._agent_supervisor

    @agent_supervisor.setter
    def agent_supervisor(
            self, sup: SymbolArg[Type[SupervisorStrategyT]]) -> None:
        self._agent_supervisor = symbol_by_name(sup)

    @property
    def web_transport(self) -> URL:
        return self._web_transport

    @web_transport.setter
    def web_transport(self, url: Union[str, URL]) -> None:
        self._web_transport = URL(url)

    @property
    def Agent(self) -> Type[AgentT]:
        return self._Agent

    @Agent.setter
    def Agent(self, Agent: SymbolArg[Type[AgentT]]) -> None:
        self._Agent = symbol_by_name(Agent)

    @property
    def ConsumerScheduler(self) -> Type[SchedulingStrategyT]:
        return self._ConsumerScheduler

    @ConsumerScheduler.setter
    def ConsumerScheduler(
            self, value: SymbolArg[Type[SchedulingStrategyT]]) -> None:
        self._ConsumerScheduler = symbol_by_name(value)

    @property
    def Event(self) -> Type[EventT]:
        return self._Event

    @Event.setter
    def Event(self, Event: SymbolArg[Type[EventT]]) -> None:
        self._Event = symbol_by_name(Event)

    @property
    def Schema(self) -> Type[SchemaT]:
        return self._Schema

    @Schema.setter
    def Schema(self, Schema: SymbolArg[Type[SchemaT]]) -> None:
        self._Schema = symbol_by_name(Schema)

    @property
    def Stream(self) -> Type[StreamT]:
        return self._Stream

    @Stream.setter
    def Stream(self, Stream: SymbolArg[Type[StreamT]]) -> None:
        self._Stream = symbol_by_name(Stream)

    @property
    def Table(self) -> Type[TableT]:
        return self._Table

    @Table.setter
    def Table(self, Table: SymbolArg[Type[TableT]]) -> None:
        self._Table = symbol_by_name(Table)

    @property
    def SetTable(self) -> Type[TableT]:
        return self._SetTable

    @SetTable.setter
    def SetTable(self, SetTable: SymbolArg[Type[TableT]]) -> None:
        self._SetTable = symbol_by_name(SetTable)

    @property
    def GlobalTable(self) -> Type[GlobalTableT]:
        return self._GlobalTable

    @GlobalTable.setter
    def GlobalTable(self, GlobalTable: SymbolArg[Type[GlobalTableT]]) -> None:
        self._GlobalTable = symbol_by_name(GlobalTable)

    @property
    def SetGlobalTable(self) -> Type[GlobalTableT]:
        return self._SetGlobalTable

    @SetGlobalTable.setter
    def SetGlobalTable(
            self, SetGlobalTable: SymbolArg[Type[GlobalTableT]]) -> None:
        self._SetGlobalTable = symbol_by_name(SetGlobalTable)

    @property
    def TableManager(self) -> Type[TableManagerT]:
        return self._TableManager

    @TableManager.setter
    def TableManager(self, Manager: SymbolArg[Type[TableManagerT]]) -> None:
        self._TableManager = symbol_by_name(Manager)

    @property
    def Serializers(self) -> Type[RegistryT]:
        return self._Serializers

    @Serializers.setter
    def Serializers(self, Serializers: SymbolArg[Type[RegistryT]]) -> None:
        self._Serializers = symbol_by_name(Serializers)

    @property
    def Worker(self) -> Type[_WorkerT]:
        return self._Worker

    @Worker.setter
    def Worker(self, Worker: SymbolArg[Type[_WorkerT]]) -> None:
        self._Worker = symbol_by_name(Worker)

    @property
    def PartitionAssignor(self) -> Type[PartitionAssignorT]:
        return self._PartitionAssignor

    @PartitionAssignor.setter
    def PartitionAssignor(
            self, Assignor: SymbolArg[Type[PartitionAssignorT]]) -> None:
        self._PartitionAssignor = symbol_by_name(Assignor)

    @property
    def LeaderAssignor(self) -> Type[LeaderAssignorT]:
        return self._LeaderAssignor

    @LeaderAssignor.setter
    def LeaderAssignor(
            self, Assignor: SymbolArg[Type[LeaderAssignorT]]) -> None:
        self._LeaderAssignor = symbol_by_name(Assignor)

    @property
    def Router(self) -> Type[RouterT]:
        return self._Router

    @Router.setter
    def Router(self, Router: SymbolArg[Type[RouterT]]) -> None:
        self._Router = symbol_by_name(Router)

    @property
    def Topic(self) -> Type[TopicT]:
        return self._Topic

    @Topic.setter
    def Topic(self, Topic: SymbolArg[Type[TopicT]]) -> None:
        self._Topic = symbol_by_name(Topic)

    @property
    def HttpClient(self) -> Type[HttpClientT]:
        return self._HttpClient

    @HttpClient.setter
    def HttpClient(self, HttpClient: SymbolArg[Type[HttpClientT]]) -> None:
        self._HttpClient = symbol_by_name(HttpClient)

    @property
    def Monitor(self) -> Type[SensorT]:
        return self._Monitor

    @Monitor.setter
    def Monitor(self, Monitor: SymbolArg[Type[SensorT]]) -> None:
        self._Monitor = symbol_by_name(Monitor)
