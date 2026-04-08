"""Key Join — subscription/response protocol (KIP-213)."""
import mmh3
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple

from mode import Service

from faust.types import AppT, TopicT
from faust.types.joins import (
    KeyJoinT,
    JoinedValue,
    SubscriptionInstruction,
)
from faust.types.tables import CollectionT
from faust import current_event
from faust.serializers import codecs

__all__ = ['KeyJoinProcessor']


class KeyJoinProcessor(Service, KeyJoinT):
    """Manages the full key join lifecycle using the subscription/response
    message-passing protocol."""

    _subscription_topic: Optional[TopicT] = None
    _response_topic: Optional[TopicT] = None
    _subscription_store: Optional[CollectionT] = None
    _previous_fk: Optional[CollectionT] = None
    _output_channel: Any = None

    def __init__(
        self,
        app: AppT,
        left_table: CollectionT,
        right_table: CollectionT,
        extractor: Callable[[Any], Any],
        *,
        inner: bool = True,
        **kwargs: Any,
    ) -> None:
        self.app = app
        self.left_table = left_table
        self.right_table = right_table
        self.extractor = extractor
        self.inner = inner
        Service.__init__(self, **kwargs)

        # Eagerly create internal topics, stores, and output channel
        # so they register with the table manager before finalization.
        _ = self.subscription_topic
        _ = self.response_topic
        _ = self.subscription_store
        _ = self.previous_fk_store

        self._output_channel = self.app.channel()

    # --- Group 2: Internal Topic Creation ---

    async def maybe_create_topics(self) -> None:
        """Create internal topics if they don't exist."""
        topics = [
            self.subscription_topic.maybe_declare(),
            self.response_topic.maybe_declare()
        ]
        await asyncio.gather(*topics)

    def _subscription_topic_name(self) -> str:
        return (
            f'{self.app.conf.id}-{self.left_table.name}-{self.right_table.name}'
            f'-subscription-registration'
        )

    def _response_topic_name(self) -> str:
        return (
            f'{self.app.conf.id}-{self.left_table.name}-{self.right_table.name}'
            f'-subscription-response'
        )

    def _create_subscription_topic(self) -> TopicT:
        """Create the subscription-registration internal topic.

        Keyed by FK (right-table key space), co-partitioned with the
        right table's changelog topic.
        """
        return self.app.topic(
            self._subscription_topic_name(),
            key_type=self.right_table.key_type,
            key_serializer=self.right_table.key_serializer,
            value_serializer='json',
            partitions=self.right_table.partitions,
            compacting=True,
            deleting=True,
            internal=True,
        )

    def _create_response_topic(self) -> TopicT:
        """Create the subscription-response internal topic.

        Keyed by left PK (left-table key space), co-partitioned with
        the left table's changelog topic.
        """
        return self.app.topic(
            self._response_topic_name(),
            key_type=self.left_table.key_type,
            key_serializer=self.left_table.key_serializer,
            value_serializer='json',
            partitions=self.left_table.partitions,
            compacting=True,
            deleting=True,
            internal=True,
        )

    @property
    def subscription_topic(self) -> TopicT:
        if self._subscription_topic is None:
            self._subscription_topic = self._create_subscription_topic()
        return self._subscription_topic

    @property
    def response_topic(self) -> TopicT:
        if self._response_topic is None:
            self._response_topic = self._create_response_topic()
        return self._response_topic

    # --- Group 3: Subscription Store ---

    def _subscription_store_name(self) -> str:
        return (
            f'{self.left_table.name}-{self.right_table.name}'
            f'-subscriptions'
        )

    def _previous_fk_store_name(self) -> str:
        return (
            f'{self.left_table.name}-{self.right_table.name}'
            f'-previous-fk'
        )

    def _create_subscription_store(self) -> CollectionT:
        """Create internal table for tracking subscriptions.

        Keys are composite ``(FK, LeftPK)`` tuples.
        Values are dicts containing ``{'hash': bytes}``.
        Co-partitioned with the right table.
        """
        return self.app.Table(
            self._subscription_store_name(),
            partitions=self.right_table.partitions,
            help='FK join subscription store (internal)',
        )

    def _create_previous_fk_store(self) -> CollectionT:
        """Create internal table for tracking previous FK per left key.

        Keys are left PKs, values are the previously-extracted FK.
        Co-partitioned with the left table.
        """
        return self.app.Table(
            self._previous_fk_store_name(),
            partitions=self.left_table.partitions,
            help='FK join previous FK tracker (internal)',
        )

    @property
    def subscription_store(self) -> CollectionT:
        if self._subscription_store is None:
            self._subscription_store = self._create_subscription_store()
        return self._subscription_store

    @property
    def previous_fk_store(self) -> CollectionT:
        if self._previous_fk is None:
            self._previous_fk = self._create_previous_fk_store()
        return self._previous_fk

    def prefix_scan(self, fk: Any) -> List[Tuple[Any, Dict[str, Any]]]:
        """Return all subscription entries for a given FK.

        Uses the subscription store's ``prefix_scan`` to find all
        composite keys ``"{fk}\\x00{left_pk}"`` matching the FK prefix
        and returns ``[(left_pk, entry), ...]``.
        """
        prefix = f"{fk}\x00"
        results = []
        for key, value in self.subscription_store.prefix_scan(prefix):
            left_pk = key[len(prefix):]
            results.append((left_pk, value))
        return results

    # --- Group 4: Hash Computation ---

    @staticmethod
    def compute_hash(value: Any) -> int:
        """Compute a 128-bit hash of a serialized value.
        """
        if value is None:
            return 0
        if isinstance(value, bytes):
            data = value
        elif isinstance(value, str):
            data = value.encode('utf-8')
        else:
            data = codecs.dumps('json', value)
        return mmh3.hash128(data)

    # --- Helpers: Key Serialization ---

    def _serialize_fk(self, fk: Any) -> bytes:
        """Serialize a foreign key using the right table's key serializer."""
        return self.app.serializers.dumps_key(
            self.right_table.key_type,
            fk,
            serializer=self.right_table.key_serializer,
        )

    def _serialize_left_pk(self, left_pk: Any) -> bytes:
        """Serialize a left primary key using the left table's key serializer."""
        return self.app.serializers.dumps_key(
            self.left_table.key_type,
            left_pk,
            serializer=self.left_table.key_serializer,
        )

    # --- Group 5: Left-Side Subscription Sender ---

    def _on_left_table_change(
        self, key: Any, value: Any, *, is_delete: bool = False,
    ) -> None:
        """Called when the left table is updated."""
        old_fk = self.previous_fk_store.get(key)

        if is_delete:
            # Send UNSUBSCRIBE_ONLY keyed by old FK
            if old_fk is not None:
                self._send_subscription(
                    old_fk, key, b'',
                    SubscriptionInstruction.UNSUBSCRIBE_ONLY,
                )
                del self.previous_fk_store[key]
            return

        new_fk = self.extractor(value)
        current_hash = self.compute_hash(value)

        if old_fk is not None and old_fk != new_fk:
            # FK changed — unsubscribe from old, subscribe to new
            self._send_subscription(
                old_fk, key, 0,
                SubscriptionInstruction.UNSUBSCRIBE_ONLY,
            )

        # Subscribe (or re-subscribe) to new FK
        self._send_subscription(
            new_fk, key, current_hash,
            SubscriptionInstruction.SUBSCRIBE_AND_RESPOND,
        )
        self.previous_fk_store[key] = new_fk

    def _send_subscription(
        self,
        fk: Any,
        left_pk: Any,
        hash_value: int,
        instruction: SubscriptionInstruction,
    ) -> None:
        msg = { "left_pk": left_pk, "hash": hash_value, "instruction": instruction.value }
        partition = self.app.producer.key_partition(current_event().message.topic, self._serialize_fk(fk)).partition
        self.subscription_topic.send_soon(key=fk, value=msg, partition=partition)

    # --- Group 6: Right-Side Subscription Processor ---

    async def _process_subscription(
        self, fk: Any, message: Any,
    ) -> None:
        """Process a subscription message on the right side."""
        instruction = SubscriptionInstruction(message.get('instruction'))
        composite_key = f"{fk}\x00{message.get('left_pk')}"

        if instruction == SubscriptionInstruction.UNSUBSCRIBE_ONLY:
            self.subscription_store.pop(composite_key, None)
            return

        # SUBSCRIBE_AND_RESPOND or SUBSCRIBE_ONLY
        self.subscription_store[composite_key] = {'hash': message.get('hash', 0)}

        if instruction == SubscriptionInstruction.SUBSCRIBE_AND_RESPOND:
            right_value = self.right_table.get(fk)
            left_pk = message.get('left_pk')
            response = { "right_value": right_value, "hash": message.get('hash', 0) }
            partition = self.app.producer.key_partition(current_event().message.topic, self._serialize_left_pk(left_pk)).partition
            await self.response_topic.send(key=left_pk, value=response, partition=partition)

    # --- Group 7: Right-Table Update Propagation ---

    def _on_right_table_change(
        self, key: Any, value: Any, *, is_delete: bool = False,
    ) -> None:
        """Called when the right table is updated."""
        right_value = None if is_delete else value
        subscribers = self.prefix_scan(key)

        for left_pk, entry in subscribers:
            response = { "right_value": right_value, "hash": entry.get('hash', 0) }
            partition = self.app.producer.key_partition(current_event().message.topic, self._serialize_left_pk(left_pk)).partition
            self.response_topic.send_soon(key=left_pk, value=response, partition=partition)

    # --- Group 8: Left-Side Response Handler ---

    async def _process_response(
        self, left_pk: Any, message: Any,
    ) -> None:
        """Process a response message on the left side."""
        left_value = self.left_table.get(left_pk)

        # Left record deleted — discard
        if left_value is None:
            return

        # Check hash for staleness
        current_hash = self.compute_hash(left_value)
        if current_hash != message.get('hash'):
            # Stale response — discard
            return

        # Inner join: skip if right side is None
        if self.inner and message.get('right_value') is None:
            return

        joined = JoinedValue(left=left_value, right=message.get('right_value'))
        if self._output_channel is not None:
            await self._output_channel.send(value=joined)

    # --- Callback Wrappers ---

    def _on_left_key_set(self, key: Any, value: Any) -> None:
        self._on_left_table_change(key, value)

    def _on_left_key_del(self, key: Any) -> None:
        self._on_left_table_change(key, None, is_delete=True)

    def _on_right_key_set(self, key: Any, value: Any) -> None:
        self._on_right_table_change(key, value)

    def _on_right_key_del(self, key: Any) -> None:
        self._on_right_table_change(key, None, is_delete=True)

    # --- Service lifecycle ---

    async def on_start(self) -> None:
        """Start background agents for FK join processing."""

        # Register callbacks on tables
        self.left_table.register_on_key_set(self._on_left_key_set)
        self.left_table.register_on_key_del(self._on_left_key_del)
        self.right_table.register_on_key_set(self._on_right_key_set)
        self.right_table.register_on_key_del(self._on_right_key_del)

        # Start background tasks
        self.add_future(self._run_subscription_agent())
        self.add_future(self._run_response_agent())

    async def on_first_start(self) -> None:
        """Call first time service starts in this process."""
        await super().on_first_start()
        await self.maybe_create_topics()

    async def on_stop(self) -> None:
        """Unregister callbacks on shutdown."""
        self.left_table.unregister_on_key_set(self._on_left_key_set)
        self.left_table.unregister_on_key_del(self._on_left_key_del)
        self.right_table.unregister_on_key_set(self._on_right_key_set)
        self.right_table.unregister_on_key_del(self._on_right_key_del)

    async def _run_subscription_agent(self) -> None:
        """Consume from subscription-registration topic."""
        async for event in self.subscription_topic.stream().events():
            fk = event.key
            message = event.value
            await self._process_subscription(fk, message)

    async def _run_response_agent(self) -> None:
        """Consume from subscription-response topic."""
        async for event in self.response_topic.stream().events():
            left_pk = event.key
            message = event.value
            await self._process_response(left_pk, message)
