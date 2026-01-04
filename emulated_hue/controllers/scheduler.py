"""Scheduler for emulated_hue."""
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

# ----------------------------------------------------------------------
#  Internal storage: map a numeric schedule‑id → the asyncio.Task that runs it
# ----------------------------------------------------------------------
# NOTE: The original code used ``dict[int : asyncio.Task]`` which is invalid
# Python syntax.  The correct generic syntax is ``dict[int, asyncio.Task]``.
_schedules: dict[int, asyncio.Task] = {}


def _async_scheduler_factory(
    func: Callable[[], Awaitable[None]], interval_ms: int
) -> Awaitable[None]:
    """Create a coroutine that runs an async ``func`` every ``interval_ms``."""
    async def scheduler_func():
        while True:
            await asyncio.sleep(interval_ms / 1000)
            await func()

    return scheduler_func()


def _scheduler_factory(func: Callable[[], None], interval_ms: int) -> Awaitable[None]:
    """Create a coroutine that runs a sync ``func`` every ``interval_ms``."""
    async def scheduler_func():
        while True:
            await asyncio.sleep(interval_ms / 1000)
            func()

    return scheduler_func()


def _is_async_function(
    func: Callable[[Any], None] | Callable[[Any], Awaitable[None]]
) -> bool:
    """Return ``True`` if ``func`` is a coroutine function or async generator."""
    is_async_gen = inspect.isasyncgenfunction(func)
    is_coro_fn = asyncio.iscoroutinefunction(func)
    return is_async_gen or is_coro_fn


def add_scheduler(
    func: Callable[[], None] | Callable[[], Awaitable[None]], interval_ms: int
) -> int:
    """
    Register a recurring task.

    * ``func`` – the callable to execute (sync or async).
    * ``interval_ms`` – how often to run it, in milliseconds.

    Returns a numeric scheduler‑id that can later be used with
    :func:`remove_scheduler`.
    """
    next_id = max(_schedules.keys()) + 1 if _schedules else 1
    if _is_async_function(func):
        task = asyncio.create_task(_async_scheduler_factory(func, interval_ms))
    else:
        task = asyncio.create_task(_scheduler_factory(func, interval_ms))
    _schedules[next_id] = task
    return next_id


def remove_scheduler(id: int) -> None:
    """Cancel and delete a single scheduler by its id."""
    task = _schedules.pop(id, None)
    if task is not None:
        task.cancel()


def remove_all_schedulers() -> None:
    """Cancel **all** registered schedulers and clear the internal map."""
    for task in _schedules.values():
        task.cancel()
    _schedules.clear()


async def async_stop() -> None:
    """Convenient async wrapper – stops all schedulers."""
    remove_all_schedulers()