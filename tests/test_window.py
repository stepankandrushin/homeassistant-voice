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
MIC_ROOMS = {"cam204": ELECTRICAL, "cam205": LIVING, "cam208": LIVING}


def fake_match(text, source):
    """A tiny stand-in for homeassistant.match_command."""
    action = "turn_off" if "turn off" in text else "turn_on" if "turn on" in text else None
    if action is None or "light" not in text:
        return False, None, None
    entity = ELECTRICAL if "electrical room" in text else MIC_ROOMS[source]
    return True, entity, action


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
        # 2026-09-27 19:20: "turn off the light" read right once, then the same
        # audio came back as "magical" and the command was lost.
        self.round(cam204="turn off the light")
        commands, _ = self.round(cam204="magical")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_off", "turn off the light")])

    def test_empty_next_window_does_not_cancel(self):
        self.round(cam204="turn on the light")
        self.assertEqual(len(self.round(cam204="")[0]), 1)

    def test_hold_zero_runs_on_the_first_window(self):
        self.cmd.hold_hops = 0
        commands, _ = self.round(cam204="turn on the light")
        self.assertEqual(commands, [("cam204", ELECTRICAL, "turn_on", "turn on the light")])

    def test_room_named_after_the_device_redirects_the_held_command(self):
        self.round(cam205="turn on the light")                       # mic's own room so far
        self.assertEqual(self.round(cam205="turn on the light in the electrical room")[0], [])  # grew: wait
        commands, _ = self.round(cam205="turn on the light in the electrical room")
        self.assertEqual(commands, [("cam205", ELECTRICAL, "turn_on", "turn on the light in the electrical room")])

    def test_room_word_still_arriving_waits_one_more_round(self):
        self.round(cam205="turn off the light")
        self.assertEqual(self.round(cam205="turn off the light in the elec")[0], [])  # same parse, grew
        commands, _ = self.round(cam205="turn off the light in the electrical room")
        self.assertEqual(commands, [("cam205", ELECTRICAL, "turn_off", "turn off the light in the electrical room")])

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

    def test_empty_text_never_reaches_the_matcher(self):
        seen = []
        cmd = WindowCommander(lambda text, src: seen.append(text) or (False, None, None))
        cmd.process_round([("cam204", "", 1), ("cam205", "  ", 1)], 0.0)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
