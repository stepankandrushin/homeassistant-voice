#!/usr/bin/env python3
"""
Unit tests for utils.window: the per-mic audio window and the rules that turn
overlapping window transcripts into commands exactly once.

In window mode every mic's last few seconds are re-transcribed every hop, so
one spoken command appears in many consecutive windows and on every mic that
heard it. These tests lock in that it still executes once.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.window import AudioWindow, WindowCommander  # noqa: E402

LIVING = "switch.living_light"
ELECTRICAL = "switch.electrical_electrical_light"
GARDEN = "switch.garden_switch"
MIC_ROOMS = {"cam204": ELECTRICAL, "cam205": LIVING, "cam208": LIVING}


def fake_match(text, source):
    """A tiny stand-in for homeassistant.match_command."""
    action = "turn_off" if "turn off" in text else "turn_on" if "turn on" in text else None
    if action is None or "light" not in text:
        return False, None, None, False
    if "electrical" in text:
        return True, ELECTRICAL, action, False
    if "garden" in text:
        return True, GARDEN, action, False
    return True, MIC_ROOMS[source], action, True


class AudioWindowTest(unittest.TestCase):
    def test_keeps_only_the_last_max_bytes(self):
        w = AudioWindow(4)
        w.append(b"ab")
        w.append(b"cdef")
        self.assertEqual(w.snapshot(), (b"cdef", 6))

    def test_discard_until_keeps_audio_after_the_snapshot(self):
        w = AudioWindow(100)
        w.append(b"abcd")
        _, end = w.snapshot()
        w.append(b"ef")  # arrives while the batch is being transcribed
        w.discard_until(end)
        self.assertEqual(w.snapshot(), (b"ef", 6))

    def test_discard_of_already_trimmed_audio_is_a_noop(self):
        w = AudioWindow(2)
        w.append(b"abcd")
        w.discard_until(1)
        self.assertEqual(w.snapshot(), (b"cd", 4))

    def test_reset_empties_but_offsets_keep_counting(self):
        w = AudioWindow(100)
        w.append(b"abcd")
        w.reset()
        w.append(b"ef")
        self.assertEqual(w.snapshot(), (b"ef", 6))


class WindowCommanderTest(unittest.TestCase):
    def setUp(self):
        self.cmd = WindowCommander(fake_match, hold_hops=1, dedupe_sec=5.0)
        self.t = 1000.0
        self.end = 0

    def round(self, **texts):
        """One hop: every mic in `texts` delivers its window transcript."""
        self.t += 0.5
        self.end += 16000
        with redirect_stdout(io.StringIO()):
            return self.cmd.process_round([(m, t, self.end) for m, t in texts.items()], self.t)

    def test_matching_window_is_held_one_round(self):
        self.assertEqual(self.round(cam204="turn on the light"), ([], {}))
        commands, discards = self.round(cam204="turn on the light")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_on", "turn on the light")])

    def test_one_matching_window_is_enough(self):
        # A phrase read right once, then misheard in the next window, must
        # still run rather than be lost.
        self.round(cam204="turn off the light")
        commands, _ = self.round(cam204="tough delight")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_off", "turn off the light")])

    def test_empty_next_window_does_not_cancel(self):
        self.round(cam204="turn on the light")
        self.assertEqual(len(self.round(cam204="")[0]), 1)

    def test_hold_zero_runs_on_the_first_window(self):
        self.cmd.hold_hops = 0
        commands, _ = self.round(cam204="turn on the light")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_on", "turn on the light")])

    def test_room_named_after_the_device_redirects_the_held_command(self):
        self.assertEqual(self.round(cam205="turn on the light")[0], [])  # mic's own room so far
        commands, _ = self.round(cam205="turn on the light in the electrical room")
        self.assertEqual(commands, [("cam205", ELECTRICAL, "turn_on",
                                     "turn on the light in the electrical room")])

    def test_named_room_runs_on_the_first_window(self):
        # Nothing can still change where it goes, so don't wait a round.
        commands, _ = self.round(cam208="turn off the light in the garden")
        self.assertEqual(commands, [("cam208", GARDEN, "turn_off", "turn off the light in the garden")])

    def test_room_word_still_arriving_waits_one_more_round(self):
        self.round(cam205="turn off the light")
        self.assertEqual(self.round(cam205="turn off the light in the elec")[0], [])  # same parse, grew
        commands, _ = self.round(cam205="turn off the light in the electrical room")
        self.assertEqual(commands, [("cam205", ELECTRICAL, "turn_off",
                                     "turn off the light in the electrical room")])

    def test_named_room_runs_before_a_garbled_reading_can_replace_it(self):
        # The window after the one that named the room reads it garbled (no
        # room -> the mic's living room); the named reading must win.
        self.round(cam205="turn on the light")
        self.round(cam205="turn on the light in the")
        commands = self.round(cam205="turn on the light in the garden")[0]
        commands += self.round(cam205="turn on the light in the gordon")[0]
        self.assertEqual(commands, [("cam205", GARDEN, "turn_on", "turn on the light in the garden")])

    def test_other_mics_named_room_beats_this_mics_own_room(self):
        # Both living-room mics hear the phrase; cam205 garbles the room and,
        # if it won by finishing first, would light the living room instead
        # of the garden.
        self.round(cam205="please turn on the light", cam208="please turn on the light in the")
        commands, discards = self.round(cam205="please turn on the light in the gordon",
                                        cam208="please turn on the light in the garden")
        commands += self.round(cam205="please turn on the light in the gordon",
                               cam208="please turn on the light in the garden")[0]
        self.assertEqual(commands, [("cam208", GARDEN, "turn_on", "please turn on the light in the garden")])

    def test_other_mics_named_room_is_taken_even_before_its_hold_ends(self):
        self.round(cam205="turn on the light")
        commands, discards = self.round(cam205="turn on the light", cam208="turn on the light in the garden")
        self.assertEqual(commands, [("cam208", GARDEN, "turn_on", "turn on the light in the garden")])
        self.assertEqual(set(discards), {"cam205", "cam208"})

    def test_duplicate_named_reading_takes_the_roomless_copy_with_it(self):
        # The garden went off; a moment later both mics read the tail of the
        # same phrase, cam205 without the room word.
        self.assertEqual(len(self.round(cam208="turn off the light in the garden")[0]), 1)
        self.round(cam205="turn off the light", cam208="")
        self.assertEqual(self.round(cam205="turn off the light", cam208="turn off the light in the garden")[0], [])
        self.assertEqual(self.round(cam205="", cam208="")[0], [])

    def test_reading_cut_off_before_its_room_is_not_a_command(self):
        # The window ended inside "in the garden"; the next came back empty.
        # Running it would have switched the mic's own room.
        self.assertEqual(self.round(cam208="the light turn off the light in the")[0], [])
        self.assertEqual(self.round(cam208="")[0], [])

    def test_held_command_waits_while_the_room_is_being_said(self):
        self.round(cam205="turn off the light")
        self.assertEqual(self.round(cam205="turn off the light in the")[0], [])
        self.assertEqual(self.round(cam205="turn off the light in the")[0], [])
        commands, _ = self.round(cam205="turn off the light in the garden")
        self.assertEqual(commands, [("cam205", GARDEN, "turn_off", "turn off the light in the garden")])

    def test_growing_text_waits_at_most_one_extra_round(self):
        self.round(cam204="turn on the light")
        self.round(cam204="turn on the light yes")
        commands, _ = self.round(cam204="turn on the light yes well")
        self.assertEqual(len(commands), 1)

    def test_run_consumes_every_mic(self):
        self.round(cam204="turn on the light", cam205="")
        commands, discards = self.round(cam204="turn on the light", cam205="")
        self.assertEqual(len(commands), 1)
        self.assertEqual(discards, {"cam204": self.end, "cam205": self.end})

    def test_same_mic_same_phrase_later_is_not_executed_again(self):
        # Even if the caller failed to discard, the phrase can't run twice
        # inside the dedupe window.
        executed = []
        for _ in range(9):  # 4.5 s of the phrase staying in the window
            executed += self.round(cam204="turn on the light")[0]
        self.assertEqual(len(executed), 1)

    def test_two_mics_hearing_one_phrase_execute_once(self):
        self.round(cam205="turn off the light", cam208="turn off the light")
        commands, discards = self.round(cam205="turn off the light", cam208="turn off the light")
        self.assertEqual(len(commands), 1)
        self.assertEqual(set(discards), {"cam205", "cam208"})
        # cam208's stream lags and it matches the phrase again a round later
        commands, _ = self.round(cam205="", cam208="turn off the light")
        commands += self.round(cam205="", cam208="turn off the light")[0]
        self.assertEqual(commands, [])

    def test_other_mic_holding_the_same_phrase_is_dropped(self):
        self.round(cam205="turn off the light")
        self.round(cam205="turn off the light", cam208="turn off the light")  # cam205 runs, cam208 held
        self.assertEqual(self.round(cam205="", cam208="")[0], [])

    def test_opposite_command_right_after_is_executed(self):
        self.round(cam204="turn on the light")
        self.round(cam204="turn on the light")
        self.round(cam204="turn off the light")
        commands, _ = self.round(cam204="turn off the light")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_off", "turn off the light")])

    def test_same_command_runs_again_after_dedupe_window(self):
        self.round(cam204="turn on the light")
        self.round(cam204="turn on the light")
        self.t += 5.0
        self.round(cam204="turn on the light")
        self.assertEqual(len(self.round(cam204="turn on the light")[0]), 1)

    def test_particle_verb_ending_in_on_is_not_cut_off(self):
        # "on" is not a room preposition: "turn the light on" is complete.
        cmd = WindowCommander(lambda text, src: (True, LIVING, "turn_on", True), hold_hops=1)
        with redirect_stdout(io.StringIO()):
            cmd.process_round([("cam205", "turn the light on", 1)], 0.0)
            commands, _ = cmd.process_round([("cam205", "turn the light on", 2)], 0.5)
        self.assertEqual(commands, [("cam205", LIVING, "turn_on", "turn the light on")])

    def test_empty_text_never_reaches_the_matcher(self):
        seen = []
        cmd = WindowCommander(lambda text, src: seen.append(text) or (False, None, None, False))
        cmd.process_round([("cam204", "", 1), ("cam205", "  ", 1)], 0.0)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
