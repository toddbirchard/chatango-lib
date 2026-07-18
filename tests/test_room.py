"""Tests for Room permission logic."""

from chatango.room import Room
from chatango.user import AdminFlags, ModeratorFlags, UserManager


def build_room_with_mods():
    """Builds a room with an owner, an admin, and a regular mod (no network)."""
    room = Room("testroom")
    owner = UserManager.get_user(name="testowner")
    admin = UserManager.get_user(name="testadmin")
    mod = UserManager.get_user(name="testmod")
    room.owner = owner
    # Mods are stored as ModeratorFlags values, as built by _rcmd_ok/_rcmd_mods
    room._mods[admin] = ModeratorFlags.EDIT_MODS | ModeratorFlags.EDIT_GROUP
    room._mods[mod] = ModeratorFlags.SEE_IPS
    return room, owner, admin, mod


def test_get_level_owner():
    """Owner resolves to level 3."""
    room, owner, _, _ = build_room_with_mods()
    assert room.get_level(owner) == 3


def test_get_level_admin():
    """A mod holding any AdminFlags resolves to level 2."""
    room, _, admin, _ = build_room_with_mods()
    assert room._mods[admin] & AdminFlags
    assert room.get_level(admin) == 2


def test_get_level_moderator():
    """A mod without AdminFlags resolves to level 1 (regression: no .isadmin
    attribute exists on ModeratorFlags, which previously raised AttributeError)."""
    room, _, _, mod = build_room_with_mods()
    assert room.get_level(mod) == 1


def test_get_level_regular_user():
    """A user absent from the mods dict resolves to level 0."""
    room, _, _, _ = build_room_with_mods()
    regular = UserManager.get_user(name="testregular")
    assert room.get_level(regular) == 0


def test_get_level_accepts_names():
    """get_level resolves string names to User objects."""
    room, _, _, _ = build_room_with_mods()
    assert room.get_level("testowner") == 3
    assert room.get_level("testmod") == 1
