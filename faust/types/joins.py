import abc
import enum
from typing import Any, Callable, MutableMapping, NamedTuple, Optional, Tuple, Type

from .events import EventT
from .models import FieldDescriptorT, ModelT
from .streams import JoinableT

__all__ = [
    'JoinT',
    'JoinedValue',
    'KeyJoinT',
    'SubscriptionInstruction',
]


class JoinT(abc.ABC):
    fields: MutableMapping[Type[ModelT], FieldDescriptorT]
    stream: JoinableT

    @abc.abstractmethod
    def __init__(self, *, stream: JoinableT,
                 fields: Tuple[FieldDescriptorT, ...]) -> None:
        ...

    @abc.abstractmethod
    async def process(self, event: EventT) -> Optional[EventT]:
        ...


class JoinedValue(NamedTuple):
    """Result of a key join containing both sides."""
    left: Any
    right: Any


class SubscriptionInstruction(enum.Enum):
    """Instruction field for key join subscription messages."""
    SUBSCRIBE_AND_RESPOND = 'SUBSCRIBE_AND_RESPOND'
    SUBSCRIBE_ONLY = 'SUBSCRIBE_ONLY'
    UNSUBSCRIBE_ONLY = 'UNSUBSCRIBE_ONLY'


class KeyJoinT(abc.ABC):
    """Abstract type for key join strategies."""
    left_table: Any  # CollectionT (avoiding circular import)
    right_table: Any  # CollectionT
    extractor: Callable[[Any], Any]
    inner: bool


