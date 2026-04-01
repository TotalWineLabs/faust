"""Join strategies."""
from typing import Any, Callable, Optional, Tuple

from .models import Record
from .types import EventT, FieldDescriptorT, JoinT, JoinableT
from .types.joins import KeyJoinT, SubscriptionInstruction

__all__ = [
    'Join',
    'RightJoin',
    'LeftJoin',
    'InnerJoin',
    'OuterJoin',
    'KeyJoin',
    'ForeignKeyJoin',  # deprecated alias
    'SubscriptionMessage',
    'ResponseMessage',
]


class SubscriptionMessage(Record, serializer='json'):
    """Message sent from left-side to right-side via subscription topic."""
    left_pk: Any
    hash: bytes = b''
    instruction: str = SubscriptionInstruction.SUBSCRIBE_AND_RESPOND.value


class ResponseMessage(Record, serializer='json'):
    """Message sent from right-side back to left-side via response topic."""
    right_value: Any = None
    hash: bytes = b''


class Join(JoinT):
    """Base class for join strategies."""

    def __init__(self, *, stream: JoinableT,
                 fields: Tuple[FieldDescriptorT, ...]) -> None:
        self.fields = {field.model: field for field in fields}
        self.stream = stream

    async def process(self, event: EventT) -> Optional[EventT]:
        """Process event to be joined with another event."""
        raise NotImplementedError()

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, type(self)):
            return (other.fields == self.fields and
                    other.stream is self.stream)
        return False

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)


class RightJoin(Join):
    """Right-join strategy."""


class LeftJoin(Join):
    """Left-join strategy."""


class InnerJoin(Join):
    """Inner-join strategy."""


class OuterJoin(Join):
    """Outer-join strategy."""


class KeyJoin(KeyJoinT):
    """Key join strategy linking two tables via subscription/response."""

    def __init__(
        self,
        left_table: Any,
        right_table: Any,
        extractor: Callable[[Any], Any],
        *,
        inner: bool = True,
    ) -> None:
        self.left_table = left_table
        self.right_table = right_table
        self.extractor = extractor
        self.inner = inner


def ForeignKeyJoin(*args: Any, **kwargs: Any) -> 'KeyJoin':
    """Deprecated: use KeyJoin instead."""
    import warnings
    warnings.warn(
        "ForeignKeyJoin is deprecated and will be removed in the next major "
        "version. Use KeyJoin instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return KeyJoin(*args, **kwargs)
