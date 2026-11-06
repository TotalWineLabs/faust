"""Mock helpers the test suite imports from :mod:`mode.utils.mocks`.

Robinhood's ``mode`` shipped them; ``mode-streaming``, which Faust requires
now, keeps only ``IN``, ``call``, ``mask_module`` and ``patch_module``.
:func:`install` puts the rest back into ``mode.utils.mocks`` before any test
module imports them, with mode's semantics: an :class:`AsyncMock` records the
awaited call on its ``coro`` attribute, which the tests assert on.
"""
import asyncio
import inspect
import types
import unittest.mock
from itertools import count
from typing import Any, Callable, ContextManager, List, Optional, Type, Union, cast

__all__ = [
    'ANY',
    'AsyncContextManagerMock',
    'AsyncContextMock',
    'AsyncMagicMock',
    'AsyncMock',
    'ContextMock',
    'FutureMock',
    'MagicMock',
    'Mock',
    'install',
    'patch',
]

MOCK_CALL_COUNT = count(0)

ANY = unittest.mock.ANY
MagicMock = unittest.mock.MagicMock
patch = unittest.mock.patch


class Mock(unittest.mock.Mock):
    """Mock object that records the global order of its calls."""

    global_call_count: Optional[int] = None
    call_counts: List[int] = cast(List[int], None)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        ret = super().__call__(*args, **kwargs)
        number = self.global_call_count = next(MOCK_CALL_COUNT)
        if self.call_counts is None:
            self.call_counts = [number]
        else:
            self.call_counts.append(number)
        return ret

    def reset_mock(self, *args: Any, **kwargs: Any) -> None:
        super().reset_mock(*args, **kwargs)
        if self.call_counts is not None:
            self.call_counts.clear()


class _ContextMock(Mock, ContextManager):
    """Implements __enter__/__exit__ on the class, as ``with`` requires."""

    def __enter__(self) -> '_ContextMock':
        return self

    def __exit__(self,
                 exc_type: Type[BaseException] = None,
                 exc_val: BaseException = None,
                 exc_tb: types.TracebackType = None) -> Optional[bool]:
        pass


def ContextMock(*args: Any, **kwargs: Any) -> _ContextMock:
    """Mock that mocks :keyword:`with` statement contexts."""
    obj = _ContextMock(*args, **kwargs)
    obj.attach_mock(_ContextMock(), '__enter__')
    obj.attach_mock(_ContextMock(), '__exit__')
    obj.__enter__.return_value = obj
    # A true __exit__ return value would swallow the exception.
    obj.__exit__.return_value = None
    return obj


def _awaiting(target: Callable) -> Callable:
    # What asyncio.coroutine (gone since Python 3.11) made of a plain
    # function: the call runs when awaited, and an awaitable result is
    # awaited in turn.
    async def call(*args: Any, **kwargs: Any) -> Any:
        result = target(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result
    return call


class AsyncMock(unittest.mock.Mock):
    """Mock for ``async def`` function/method or anything awaitable."""

    def __init__(self, *args: Any, name: str = None, **kwargs: Any) -> None:
        super().__init__(name=name)
        coro = Mock(*args, **kwargs)
        self.attach_mock(coro, 'coro')
        self.side_effect = _awaiting(coro)


class AsyncMagicMock(unittest.mock.MagicMock):
    """A magic mock type for ``async def`` functions/methods."""

    def __init__(self, *args: Any, name: str = None, **kwargs: Any) -> None:
        super().__init__(name=name)
        coro = MagicMock(*args, **kwargs)
        self.attach_mock(coro, 'coro')
        self.side_effect = _awaiting(coro)


class AsyncContextMock(unittest.mock.Mock):
    """Mock for :class:`typing.AsyncContextManager`."""

    def __init__(self, *args: Any,
                 aenter_return: Any = None,
                 aexit_return: Any = None,
                 side_effect: Union[Callable, BaseException] = None,
                 **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.aenter_return = aenter_return
        self.aexit_return = aexit_return
        self.side_effect = side_effect

    async def __aenter__(self) -> Any:
        mgr = self.aenter_return or self.return_value
        if self.side_effect:
            if isinstance(self.side_effect, BaseException):
                raise self.side_effect
            return self.side_effect()
        if isinstance(mgr, AsyncMock):
            return mgr.coro
        return mgr

    async def __aexit__(self, *args: Any) -> Any:
        return self.aexit_return


AsyncContextManagerMock = AsyncContextMock


class FutureMock(unittest.mock.Mock):
    """Mock a :class:`asyncio.Future`."""

    awaited = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._loop = asyncio.get_event_loop_policy().get_event_loop()

    def __await__(self) -> Any:
        self.awaited = True
        yield self()

    def assert_awaited(self) -> None:
        assert self.awaited

    def assert_not_awaited(self) -> None:
        assert not self.awaited


def install() -> None:
    """Add the helpers mode-streaming dropped to ``mode.utils.mocks``."""
    import mode.utils.mocks as target
    for name in __all__:
        if name != 'install' and not hasattr(target, name):
            setattr(target, name, globals()[name])
