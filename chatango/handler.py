import asyncio
import logging
import sys
import traceback
from collections.abc import Iterable
from typing import Coroutine, Optional

logger = logging.getLogger(__name__)


class TaskHandler:
    """
    Base class with helpers for asyncio task management. This allows chat rooms
    and other objects to offer some simple task infrastructure so users don't
    have to track and await them manually.  Tasks with errors will be logged for
    visibility during development.  You can await complete_tasks which returns
    when all tasks are finished, or tasks_forever which never returns.
    """

    @property
    def tasks(self):
        """All tasks stored and tracked."""
        assert self.task_loop
        if not hasattr(self, "_tasks"):
            self._tasks = []
        return self._tasks

    @property
    def task_loop(self):
        """Main task loop which is started automatically and never ends."""
        if not hasattr(self, "_task_loop") or not self._task_loop:
            self._task_loop = asyncio.create_task(self.tasks_forever())
        return self._task_loop

    def add_task(self, coro: Coroutine):
        """Add and run a new task."""
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def _delayed_task(self, delay_time, coro: Coroutine):
        """Convenience wrapper for sleep before a task."""
        await asyncio.sleep(delay_time)
        await coro

    def add_delayed_task(self, delay_time, coro: Coroutine):
        """Add a task that will start after some time."""
        self.add_task(self._delayed_task(delay_time, coro))

    def cancel_tasks(self):
        """Cancel all remaining tasks."""
        for task in self.tasks:
            task.cancel()

    def end_tasks(self):
        """Cancel all tasks including the task loop."""
        self.cancel_tasks()
        self.task_loop.cancel()

    def _prune_tasks(self):
        """Remove all done tasks, and log any exceptions if present."""
        for task in self.tasks:
            if task.done():
                if task.exception():
                    self._on_task_exception(task)
                    # Run as a one-off task in case it throws an exception itself
                    asyncio.create_task(self.on_task_exception(task))
                self.tasks.remove(task)

    def _on_task_exception(self, task: asyncio.Task):
        """Default behavior when a task results in an exception."""
        logger.error(f"Exception in task: {repr(task.get_coro())}")
        task.print_stack(file=sys.stderr)

    async def on_task_exception(self, task: asyncio.Task):
        """Callback for custom behavior on task errors."""
        pass

    async def tasks_forever(self):
        """Infinite loop to keep task maintenance for the life of object."""
        while True:
            self._prune_tasks()
            await asyncio.sleep(1)

    async def complete_tasks(self):
        """Loop to watch tasks and exit when all are completed."""
        while self.tasks:
            self._prune_tasks()
            await asyncio.gather(*self.tasks)
            await asyncio.sleep(0.1)


class EventHandler(TaskHandler):
    """
    Base class which allows generating events for itself and other listeners.
    In general this allows a chat room to generate events, and customs bots
    can implement "on_event" style callbacks to add custom behaviors, either
    through a subclass or by a listener class.  For listeners, this object is
    passed as the first parameter to the callback.

     Event:
       room.call_event("message", msg_obj)
     Callbacks:
       room.on_message(msg_obj)
       room.on_event("message", msg_obj)
       listener.on_message(room, msg_obj)
       listener.on_event(room, "message", msg_obj)
    """

    @property
    def listeners(self):
        """All objects listening here for events."""
        if not hasattr(self, "_listeners"):
            self._listeners = set()
        return self._listeners

    def add_listener(self, listener):
        """Add a listener for our events."""
        self.listeners.add(listener)

    def call_event(self, event: str, *args, **kwargs):
        """
        Trigger an event, which looks for callback methods on this object,
        and any listening objects.
        """
        attr = f"on_{event}"
        self._log_event(event, *args, **kwargs)
        # Call a generic event handler for all events
        if hasattr(self, "on_event"):
            self.add_task(getattr(self, "on_event")(event, *args, **kwargs))
        # Call the event handler on self
        if hasattr(self, attr):
            self.add_task(getattr(self, attr)(*args, **kwargs))
        # Call the same handlers on any listeners, passing self as first arg
        if self.listeners and isinstance(self.listeners, Iterable):
            for listener in self.listeners:
                if isinstance(listener, TaskHandler):
                    target = listener
                else:
                    target = self
                if hasattr(listener, "on_event"):
                    target.add_task(
                        getattr(listener, "on_event")(self, event, *args, **kwargs)
                    )
                if hasattr(listener, attr):
                    target.add_task(getattr(listener, attr)(self, *args, **kwargs))

    def _log_event(self, event: str, *args, **kwargs):
        """Debug log all events."""
        if len(args) == 0:
            args_section = ""
        elif len(args) == 1:
            args_section = args[0]
        else:
            args_section = repr(args)
        kwargs_section = "" if not kwargs else repr(kwargs)
        logger.debug(f"EVENT {event} {args_section} {kwargs_section}")


class CommandHandler:
    """
    Base class for any socket connection to Chatango. Concrete classes must
    provide implementation for _send_command which sends the command out on
    the network.

    The method _receive_command parses the command format, and will automatically
    call a method handler named _rcmd_{action}.  It also supports Request-Response
    multiplexing via the expect_command method.

     Command:
       premium:0:12345678

     Method:
       _rcmd_premium
     args:
       ["0", "12345678"]
    """

    def __init__(self):
        """Initializes the registry mapping expected commands to asyncio.Futures."""
        self._pending_waiters = {}

    def expect_command(self, action: str, timeout: float = 10.0):
        """
        Returns an awaitable that resolves when the server sends a command
        matching 'action'.
        """
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if action not in self._pending_waiters:
            self._pending_waiters[action] = []
        self._pending_waiters[action].append(fut)
        return self._expect_command_internal(action, fut, timeout)

    async def _expect_command_internal(
        self, action: str, fut: asyncio.Future, timeout: float
    ):
        """Internal awaitable for expect_command."""
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            # Cleanup this specific future from the registry
            if action in self._pending_waiters:
                if fut in self._pending_waiters[action]:
                    self._pending_waiters[action].remove(fut)
                if not self._pending_waiters[action]:
                    del self._pending_waiters[action]

    async def _send_command(self, *args, **kwargs) -> None:
        """
        Internal method to send a command using the protocol of the
        subclass (websocket, tcp, etc.)
        """
        raise TypeError("CommandHandler child class must implement _send_command")

    async def send_command(
        self, *args, expect: Optional[str] = None, timeout: float = 10.0, **kwargs
    ):
        """
        Public send method. If 'expect' is provided, it returns the server's
        response for that matching command.
        """
        waiter = None
        if expect:
            waiter = self.expect_command(expect, timeout)

        command = ":".join(str(a) for a in args)
        logger.debug(f"OUT {command}")
        await self._send_command(command, **kwargs)

        if waiter:
            return await waiter

    async def _receive_command(self, command: str):
        """
        Receive an incoming command and dynamically call a handler.
        First checks for sequential waiters before calling the dynamic
        method handler.
        """
        if not command:
            return
        logger.debug(f" IN {command}")
        action, *args = command.split(":")

        # 1. Resolve all waiters for this action
        if action in self._pending_waiters:
            # Pop the entire list to clear expectations immediately
            waiters = self._pending_waiters.pop(action)
            for fut in waiters:
                if not fut.done():
                    fut.set_result(args)

        # 2. Dynamic Dispatch
        if hasattr(self, f"_rcmd_{action}"):
            try:
                await getattr(self, f"_rcmd_{action}")(args)
            except Exception as e:
                logger.error(f"Error while handling command {action}")
                traceback.print_exception(e, file=sys.stderr)
        else:
            logger.error(f"Unhandled received command {action}")

    def _cancel_all_pending_futures(self, reason: Optional[Exception] = None):
        """Release any workflows waiting for a response if the connection drops."""
        exc = reason or ConnectionError("Connection closed unexpectedly.")
        for action in list(self._pending_waiters.keys()):
            waiters = self._pending_waiters.pop(action)
            for fut in waiters:
                if not fut.done():
                    fut.set_exception(exc)
        self._pending_waiters.clear()
