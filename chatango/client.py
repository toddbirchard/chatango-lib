import asyncio
import logging
from typing import Dict, List, Optional

from .handler import TaskHandler
from .pm import PM
from .room import Room
from .utils import public_attributes

logger = logging.getLogger(__name__)


class ConnectionListener:
    def __init__(self, client):
        """Stores a reference to the parent client."""
        self.client = client

    async def on_connect(self, obj):
        """Marks the PM or room as connected on the parent client."""
        if obj.is_pm:
            self.client.pm_connected = True
        else:
            self.client.initial_rooms_connected.append(obj.name)


class Client(TaskHandler):
    def __init__(
        self,
        username: str = "",
        password: str = "",
        rooms: List[str] = [],
        pm=False,
        room_class=Room,
        pm_class=PM,
    ):
        """Initializes client state and credentials for rooms and PM."""
        super().__init__()
        self._room_class = room_class
        self._pm_class = pm_class
        self.running = False
        self.rooms: Dict[str, Room] = {}
        self.pm: Optional[PM] = None
        self.use_pm = pm
        self.pm_connected = False
        self.initial_rooms: List[str] = rooms
        self.initial_rooms_connected: List[str] = []
        self.username = username
        self.password = password

    def __dir__(self):
        """Limits dir() output to public attributes."""
        return public_attributes(self)

    async def run(self, *, forever=False):
        """Connects to the PM and all initial rooms, then runs until tasks complete."""
        self.running = True

        if not forever and not self.use_pm and not self.initial_rooms:
            logger.error("No rooms or PM to join. Exiting.")
            return

        if self.use_pm:
            self.join_pm()

        for room_name in self.initial_rooms:
            self.join_room(room_name)

        self.add_task(self.confirm_connected())

        if forever:
            await self.task_loop
        else:
            await self.complete_tasks()
        self.running = False

    def join_pm(self):
        """Starts a background task connecting to PM (requires credentials)."""
        if not self.username or not self.password:
            logger.error("PM requires username and password.")
            return

        self.add_task(self._watch_pm())

    async def _watch_pm(self):
        """Creates the PM instance and listens until it disconnects."""
        pm = self._pm_class()
        pm.add_listener(self)
        pm.add_listener(ConnectionListener(self))
        self.pm = pm
        await pm.listen(self.username, self.password, reconnect=True)
        self.pm = None

    def leave_pm(self):
        """Disconnects from PM."""
        if self.pm:
            self.add_task(self.pm.disconnect())

    def join_room(self, room_name: str):
        """Starts a background task connecting to the named room."""
        Room.assert_valid_name(room_name)
        if room_name in self.rooms:
            logger.error(f"Already joined room {room_name}")
            return

        self.add_task(self._watch_room(room_name))

    async def _watch_room(self, room_name: str):
        """Creates the room instance and listens until it disconnects."""
        room = self._room_class(room_name)
        room.add_listener(self)
        room.add_listener(ConnectionListener(self))
        self.rooms[room_name] = room
        await room.listen(self.username, self.password, reconnect=True)
        self.rooms.pop(room_name, None)

    def leave_room(self, room_name: str):
        """Disconnects from the named room."""
        room = self.rooms.get(room_name)
        if room:
            self.add_task(room.disconnect())

    def stop(self):
        """Disconnects from PM and all joined rooms."""
        if self.pm:
            self.leave_pm()

        for room_name in list(self.rooms.keys()):
            self.leave_room(room_name)

    connection_check_timeout = 5

    async def confirm_connected(self):
        """Waits for initial connections, logging failures after the timeout."""
        try:
            await asyncio.wait_for(
                self.connection_checker(), timeout=self.connection_check_timeout
            )
        except asyncio.TimeoutError:
            problem_rooms = set(self.initial_rooms) - set(self.initial_rooms_connected)
            if problem_rooms:
                logger.error(f"Failed to connect: {', '.join(problem_rooms)}")
            if self.use_pm and not self.pm_connected:
                logger.error(f"Failed to connect to PM")
            self.add_task(self.on_started())

    async def connection_checker(self):
        """Polls until all initial rooms and PM are connected, then fires on_started."""
        while True:
            rooms_ready = set(self.initial_rooms) == set(self.initial_rooms_connected)
            pm_ready = not self.use_pm or self.pm_connected
            if rooms_ready and pm_ready:
                self.add_task(self.on_started())
                break
            await asyncio.sleep(0.1)

    async def on_started(self):
        """
        Callback for child classes, called when all initial rooms are connected,
        or after a timeout specified by class attribute connection_check_timeout.
        """
        pass
