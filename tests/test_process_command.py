#!/usr/bin/env python3
"""
Unit tests for utils.homeassistant.process_command.

These lock in the matching behaviour that several voice commands rely
on, in particular:

  * action-specific entity maps — a room_entities[room][device] value may be a
    {action: entity(s)} dict, so e.g. the pool light's `turn_off` can hit more
    entities than its `turn_on` (jacuzzi + bubbles off when the light goes off).
  * the bare-room "pool" pseudo-device ordering invariant — it must stay LAST in
    device_aliases so a real device word (light/jacuzzi/bubbles) always wins and
    only an utterance with the pool word and no device word falls through to it.

process_command reads the global `config` module, and importing it pulls in
numpy via utils.audio, so the test installs a synthetic config (and a numpy stub
when numpy is absent) into sys.modules before importing, and restores them after.
"""

import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

# Make the project root importable when run directly (run_tests.py already cds here).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

process_command = None          # bound in setUpModule once the stub config is live
_SAVED = {}                     # sys.modules entries we replaced -> restore on teardown
_STUBBED = []                   # sys.modules entries we created -> remove on teardown


def _make_stub_config():
    """A small but representative config exercising every entity-mapping shape."""
    cfg = types.ModuleType("config")

    cfg.action_aliases = {
        "turn_on": ["turn on", "start"],
        "turn_off": ["turn off", "stop"],
    }

    # ON lights the pool; OFF also kills the jacuzzi + bubbles. Shared by the
    # "light" device and the bare-room "pool" device (same object on purpose).
    pool_bundle = {
        "turn_on": ["switch.pool_light", "switch.pool_ceiling"],
        "turn_off": ["switch.pool_light", "switch.pool_ceiling",
                     "switch.pool_jacuzzi", "switch.pool_bubbles"],
    }

    cfg.device_aliases = {
        "light": ["light"],
        "fan": ["fan", "propeller"],
        "bubbles": ["bubbles", "bubble"],   # before jacuzzi
        "jacuzzi": ["jacuzzi"],
        "heater": ["heater"],
        "gate": ["gate"],
        "pool": ["pool"],            # MUST be last
    }

    cfg.room_aliases = {
        "living_room": ["living room", "lounge"],
        "terrace": ["terrace"],
        "pool": ["pool"],
    }

    cfg.default_room = "living_room"
    cfg.source_rooms = {"cam201": "terrace"}
    cfg.devices_without_room = ["jacuzzi", "bubbles"]

    cfg.room_entities = {
        "living_room": {"light": "switch.living_light"},                 # plain str
        "terrace": {
            "fan": ["switch.terrace_fan_1", "switch.terrace_fan_2"],      # plain list
            "light": ["switch.terrace_light_main", "switch.terrace_strip"],
            # action-dict with a "default" fallback and no explicit turn_off
            "heater": {"turn_on": "switch.heater", "default": "switch.heater_eco"},
            # action-dict with neither the spoken action nor a default
            "gate": {"turn_on": "switch.gate"},
        },
        "pool": {
            "light": pool_bundle,    # action-dict
            "pool": pool_bundle,     # bare-room pseudo-device, same bundle
            "jacuzzi": "switch.pool_jacuzzi",
            "bubbles": "switch.pool_bubbles",
        },
    }
    return cfg


def _install_stub(name, module):
    if name in sys.modules:
        _SAVED[name] = sys.modules[name]
    else:
        _STUBBED.append(name)
    sys.modules[name] = module


def setUpModule():
    global process_command
    _install_stub("config", _make_stub_config())
    try:
        import numpy  # noqa: F401  (use the real one if present)
    except ImportError:
        _install_stub("numpy", types.ModuleType("numpy"))
    from utils.homeassistant import process_command as pc
    process_command = pc


def tearDownModule():
    for name in _STUBBED:
        sys.modules.pop(name, None)
    for name, mod in _SAVED.items():
        sys.modules[name] = mod
    # Drop modules that imported our stub config so a later real import is clean.
    for name in ("utils.homeassistant", "utils.audio"):
        sys.modules.pop(name, None)


def run_quiet(transcript, source=None):
    """Call process_command while swallowing its chatty print() logging."""
    with redirect_stdout(io.StringIO()):
        return process_command(transcript, source)


class ActionSpecificMapTest(unittest.TestCase):
    """The {action: entity(s)} dict — the headline new mechanism."""

    def test_pool_light_off_also_kills_jacuzzi_and_bubbles(self):
        ok, entity, action = run_quiet("turn off the pool light")
        self.assertTrue(ok)
        self.assertEqual(action, "turn_off")
        self.assertEqual(entity, ["switch.pool_light", "switch.pool_ceiling",
                                  "switch.pool_jacuzzi", "switch.pool_bubbles"])

    def test_pool_light_on_leaves_jacuzzi_and_bubbles_alone(self):
        ok, entity, action = run_quiet("turn on the pool light")
        self.assertTrue(ok)
        self.assertEqual(action, "turn_on")
        self.assertEqual(entity, ["switch.pool_light", "switch.pool_ceiling"])

    def test_default_key_used_when_action_not_listed(self):
        # heater dict has turn_on + default, no turn_off -> off falls back to default
        ok, entity, action = run_quiet("turn off the heater", "cam201")
        self.assertTrue(ok)
        self.assertEqual((entity, action), ("switch.heater_eco", "turn_off"))

    def test_explicit_action_wins_over_default(self):
        ok, entity, action = run_quiet("turn on the heater", "cam201")
        self.assertEqual((ok, entity, action), (True, "switch.heater", "turn_on"))

    def test_unmapped_action_without_default_fails(self):
        # gate dict has only turn_on, no default -> "off" resolves to nothing
        self.assertEqual(run_quiet("turn off the gate", "cam201"), (False, None, None))


class BareRoomPoolDeviceTest(unittest.TestCase):
    """The "pool" pseudo-device and its must-stay-last ordering invariant."""

    def test_bare_pool_word_off_hits_full_bundle(self):
        ok, entity, action = run_quiet("turn off the pool")
        self.assertTrue(ok)
        self.assertEqual(action, "turn_off")
        self.assertEqual(entity, ["switch.pool_light", "switch.pool_ceiling",
                                  "switch.pool_jacuzzi", "switch.pool_bubbles"])

    def test_bare_pool_word_on_hits_lights_only(self):
        ok, entity, action = run_quiet("turn on the pool")
        self.assertEqual((ok, action), (True, "turn_on"))
        self.assertEqual(entity, ["switch.pool_light", "switch.pool_ceiling"])

    def test_real_device_word_wins_over_pool_fallback(self):
        # "jacuzzi" must match the jacuzzi device, not the bare-pool bundle.
        ok, entity, action = run_quiet("turn off the jacuzzi in the pool")
        self.assertEqual((ok, entity, action), (True, "switch.pool_jacuzzi", "turn_off"))

    def test_bubbles_alias_ordered_before_jacuzzi(self):
        ok, entity, _ = run_quiet("turn on the bubbles in the jacuzzi")
        self.assertEqual((ok, entity), (True, "switch.pool_bubbles"))


class RoomResolutionTest(unittest.TestCase):
    """Room defaulting, per-source rooms, and devices_without_room."""

    def test_devices_without_room_found_from_any_mic(self):
        # No room word; jacuzzi is globally unique -> resolved by cross-room search.
        ok, entity, action = run_quiet("turn on the jacuzzi", "cam205")
        self.assertEqual((ok, entity, action), (True, "switch.pool_jacuzzi", "turn_on"))

    def test_source_room_used_when_room_unspoken(self):
        # cam201 -> terrace, so an unqualified "light" routes to terrace lights.
        ok, entity, action = run_quiet("turn off the light", "cam201")
        self.assertEqual(action, "turn_off")
        self.assertEqual(entity, ["switch.terrace_light_main", "switch.terrace_strip"])

    def test_default_room_used_when_source_unknown(self):
        ok, entity, _ = run_quiet("turn on the light")  # source None -> living_room
        self.assertEqual((ok, entity), (True, "switch.living_light"))

    def test_spoken_room_without_that_device_fails(self):
        # living_room has no fan -> no match even though both words are recognised.
        self.assertEqual(run_quiet("turn on the fan living room"), (False, None, None))


class PlainMappingTest(unittest.TestCase):
    """str and list entity values keep working unchanged."""

    def test_plain_string_entity(self):
        self.assertEqual(run_quiet("turn on the light"), (True, "switch.living_light", "turn_on"))

    def test_plain_list_entity(self):
        ok, entity, action = run_quiet("turn on the fan", "cam201")
        self.assertEqual((ok, action), (True, "turn_on"))
        self.assertEqual(entity, ["switch.terrace_fan_1", "switch.terrace_fan_2"])


class NoMatchTest(unittest.TestCase):
    """Early-exit paths return a clean (False, None, None)."""

    def test_empty_transcript(self):
        self.assertEqual(run_quiet(""), (False, None, None))

    def test_no_action_word(self):
        self.assertEqual(run_quiet("pool please"), (False, None, None))

    def test_no_device_word(self):
        self.assertEqual(run_quiet("turn off this"), (False, None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
