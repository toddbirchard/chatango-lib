import asyncio
import logging
import time
from typing import Dict, List, Optional

from .connection import WebsocketConnection
from .exceptions import AlreadyConnectedError
from .handler import CommandHandler, EventHandler
from .message import _process_pm, message_cut
from .user import Friend, Session, User, UserManager
from .utils import gen_uid, get_token, public_attributes

logger = logging.getLogger(__name__)


class PM(WebsocketConnection, EventHandler):
    """
    Modern WebSocket-based Private Messaging (PM) implementation.
    """

    def __init__(self):
        """Initializes PM connection state, friends list, and history."""
        WebsocketConnection.__init__(self)
        EventHandler.__init__(self)
        self.server = "c1.chatango.com"
        self.port = 8081
        self.session: Session = Session(room=self, user=UserManager.get_user())
        self.reconnect = False
        self.__token = None

        # internal state
        self._uid = gen_uid()
        self._silent = 0
        self._maxlen = 11600
        self._friends: Dict[str, Friend] = dict()
        self._blocked: List[User] = list()
        self._premium = False
        self._history = list()

    def __dir__(self):
        """Limits dir() output to public attributes."""
        return public_attributes(self)

    def __repr__(self):
        """Returns a static PM identifier."""
        return "<PM>"

    @property
    def name(self):
        """Display name of the PM connection."""
        return repr(self)

    @property
    def is_pm(self):
        """Always True for PM connections."""
        return True

    @property
    def user(self):
        """The currently logged-in user."""
        return self.session.user

    @property
    def premium(self):
        """Whether the logged-in user has premium status."""
        return self._premium

    @property
    def history(self):
        """List of received PM messages."""
        return self._history

    @property
    def blocked(self):
        """Users blocked from sending PMs."""
        return self._blocked

    @property
    def friends(self):
        """Names on the friends (watch) list."""
        return list(self._friends.keys())

    async def connect(self, user_name: str, password: str):
        """
        The complete PM handshake workflow written sequentially.
        """
        if self.connected:
            raise AlreadyConnectedError(self.name)

        await self._connect(f"wss://{self.server}:{self.port}/")

        try:
            if not self.__token:
                self.__token = await get_token(user_name, password)

            if not self.__token:
                raise ConnectionError(
                    "Authentication failed: could not retrieve auth token."
                )

            handshake_args = ["tlogin", self.__token, "2"]
            if self.session.session_id:
                handshake_args.append(self.session.session_id)

            ok_args = await self.send_command(
                *handshake_args, expect="OK", timeout=10.0, terminator="\x00"
            )
            self.session.user = UserManager.get_user(user_name)

            if ok_args:
                self.session.session_id = ok_args[0]

            await self.send_command("wl")
            await self.send_command("getblock")
            await self.send_command("settings")

            self.call_event("connect")
            logger.info("PM successfully connected and initialized.")

        except (TimeoutError, ConnectionError) as e:
            logger.error(f"PM Handshake failed: {e}")
            await self.disconnect()
            raise

    async def disconnect(self):
        """Disconnects from the PM server without reconnecting."""
        self.reconnect = False
        await self._disconnect()

    async def listen(self, user_name: str, password: str, reconnect=False):
        """Connects and reconnects to the PM server until stopped."""
        self.reconnect = reconnect
        while True:
            try:
                await self.connect(user_name, password)
                while self.connected:
                    await asyncio.sleep(1)
                self.call_event("disconnect")
            except Exception as e:
                logger.error(f"Error in PM listen loop: {e}")

            if not self.reconnect:
                break
            await asyncio.sleep(3)
        await self.complete_tasks()
        self.end_tasks()

    async def send_message(self, target, message: str, use_html: bool = False):
        """Sends a private message to the target, splitting long messages."""
        if isinstance(target, User):
            target = target.name
        if self._silent > time.time():
            self.call_event("toofast", message)
            return

        if len(message) > 0:
            for msg in message_cut(message, self._maxlen):
                # PM format often uses <m v="1"><g xs0="0"> wrapper
                msg_payload = f'{self.user.styles.get_name_tag()}<m v="1"><g xs0="0">{self.user.styles.format_message(msg, is_pm=True)}</g></m>'
                await self.send_command("msg", target.lower(), msg_payload)

    # --- Command Handlers ---

    async def _rcmd_OK(self, args):
        """Handshake acknowledgement; consumed via expect_command."""
        pass

    async def _rcmd_premium(self, args):
        """Updates premium status and enables backgrounds if premium."""
        if args and args[0] == "210":
            self._premium = True
            await self.enable_bg()
        else:
            self._premium = False

    async def _rcmd_time(self, args):
        """Records server time and computes clock correction."""
        conn_time = args[0]
        self.session.conn_time = conn_time
        # Convert ms to seconds if needed, but chatango correction_time usually expects float
        self.session.correction_time = int(float(conn_time) - time.time())

    async def _rcmd_kickingoff(self, args):
        """Handles being kicked off by a login from another location."""
        self.call_event("kickingoff")
        self.__token = None
        await self.disconnect()

    async def _rcmd_DENIED(self, args):
        """Handles login denial by clearing the token and disconnecting."""
        self.call_event("DENIED")
        self.__token = None
        await self.disconnect()

    async def _rcmd_toofast(self, args):
        """Applies a temporary send cooldown after flooding."""
        self._silent = time.time() + 12
        self.call_event("toofast")

    async def _rcmd_msglexceeded(self, args):
        """Signals that a message exceeded the maximum length."""
        self.call_event("msglexceeded")

    async def _rcmd_msg(self, args):
        # msg:msg_id:sender_handle:timestamp:message_content
        """Processes an incoming private message."""
        msg = await _process_pm(self, args)
        self._add_to_history(msg)
        self.call_event("msg", msg)

    async def _rcmd_msgoff(self, args):
        # msgoff:msg_id:sender_handle:timestamp:message_content
        """Processes a private message that was received while offline."""
        msg = await _process_pm(self, args)
        msg.msgoff = True
        self._add_to_history(msg)
        self.call_event("msgoff", msg)

    async def _rcmd_wl(self, args):
        """Populates the friends list from the watch list payload."""
        self._friends.clear()
        # Modern WL format is just colon-separated fields consumed 4 by 4
        # uid, last_logout, status, idle_mins
        for i in range(0, len(args), 4):
            chunk = args[i : i + 4]
            if len(chunk) < 4:
                break

            name, last_on, status, idle = chunk
            user = UserManager.get_user(name)
            friend = Friend(user, self)

            if status in ["off", "offline"]:
                friend._status = "offline"
            elif status in ["on", "online"]:
                friend._status = "online"
            elif status in ["app"]:
                friend._status = "app"

            friend._check_status(
                float(last_on) if last_on != "None" else 0, None, int(idle)
            )
            self._friends[user.name] = friend
            # Tracking ensures we get real-time presence updates
            await self.send_command("track", user.name)

        self.call_event("wl")

    async def _rcmd_wlonline(self, args):
        """Marks a friend as online."""
        friend = self._friends.get(args[0].lower())
        if friend:
            friend._status = "online"
            friend._last_active = time.time()
            self.call_event("wlonline", friend)

    async def _rcmd_wloffline(self, args):
        """Marks a friend as offline."""
        friend = self._friends.get(args[0].lower())
        if friend:
            friend._status = "offline"
            self.call_event("wloffline", friend)

    async def _rcmd_wlapp(self, args):
        """Marks a friend as online via the mobile app."""
        friend = self._friends.get(args[0].lower())
        if friend:
            friend._status = "app"
            self.call_event("wlapp", friend)

    async def _rcmd_wladd(self, args):
        """Adds a friend to the watch list and starts tracking presence."""
        name = args[0]
        user = UserManager.get_user(name)
        friend = Friend(user, self)
        self._friends[user.name] = friend
        self.call_event("wladd", friend)
        await self.send_command("track", user.name)

    async def _rcmd_wldelete(self, args):
        """Removes a friend from the watch list."""
        name = args[0].lower()
        if name in self._friends:
            friend = self._friends.pop(name)
            self.call_event("wldelete", friend)

    async def _rcmd_idleupdate(self, args):
        # idleupdate:user_handle:state
        """Updates a friend's idle state."""
        name = args[0].lower()
        state = args[1]
        friend = self._friends.get(name)
        if friend:
            friend._idle = state == "1"
            friend._last_active = time.time()
            self.call_event("idleupdate", friend)

    async def _rcmd_presence(self, args):
        # presence:data
        """Emits a raw presence event."""
        self.call_event("presence", args)

    async def _rcmd_block_list(self, args):
        # block_list:list_data (semicolon separated)
        """Populates the blocked-users list."""
        raw_list = ":".join(args)
        self._blocked = [
            UserManager.get_user(name) for name in raw_list.split(";") if name
        ]
        self.call_event("block_list")

    async def _rcmd_unblocked(self, args):
        """Removes a user from the blocked list."""
        name = args[0].lower()
        self._blocked = [u for u in self._blocked if u.name != name]
        self.call_event("unblocked", UserManager.get_user(name))

    async def _rcmd_settings(self, args):
        # settings:settings_json
        """Emits the account settings payload."""
        self.call_event("settings", args[0])

    async def _rcmd_seller_name(self, args):
        # seller_name:name[:session_id]
        """Stores the session id and emits the seller name."""
        if len(args) > 1:
            self.session.session_id = args[1]
        self.call_event("seller_name", args[0])

    async def _rcmd_reload_profile(self, args):
        """Signals that a user's profile should be reloaded."""
        user = UserManager.get_user(name=args[0])
        self.call_event("reload_profile", user)

    # --- Helper methods ---

    async def add_friend(self, user):
        """Adds a user to the friends (watch) list."""
        name = user.name if isinstance(user, User) else str(user)
        return await self.send_command("wladd", name)

    async def remove_friend(self, user):
        """Removes a user from the friends (watch) list."""
        name = user.name if isinstance(user, User) else str(user)
        return await self.send_command("wldelete", name)

    async def block(self, user):
        """Blocks a user from sending PMs."""
        name = user.name if isinstance(user, User) else str(user)
        # Format: block:handle:handle:user_type
        # user_type "1" is generally used for registered users
        return await self.send_command("block", name, name, "1")

    async def unblock(self, user):
        """Unblocks a previously blocked user."""
        name = user.name if isinstance(user, User) else str(user)
        return await self.send_command("unblock", name, name, "1")

    def get_friend(self, name: str) -> Optional[Friend]:
        """Looks up a friend by name."""
        return self._friends.get(name.lower())

    def _add_to_history(self, msg):
        """Appends a message to history, trimming to the last 1000."""
        self._history.append(msg)
        if len(self._history) > 1000:
            self._history.pop(0)

    async def enable_bg(self):
        """Enables message backgrounds in PMs."""
        return await self.send_command("setsettings", "disable_msg_bg", "off")

    async def disable_bg(self):
        """Disables message backgrounds in PMs."""
        return await self.send_command("setsettings", "disable_msg_bg", "on")
