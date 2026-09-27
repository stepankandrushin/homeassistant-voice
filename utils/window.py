"""Window mode: continuous transcription without a speech threshold.

Instead of cutting utterances by loudness, every mic keeps its last
WINDOW_SEC of audio (AudioWindow), and every WINDOW_HOP_SEC main.py sends
all mics' windows to the transcription server in one batch. A spoken command
therefore shows up in many consecutive windows, and on every mic that heard
it; WindowCommander decides which of those transcripts become a command, so
that each utterance executes once.
"""

from threading import Lock


def entity_key(entity_id):
    """Hashable key for dedupe — tuples for lists, strings pass through."""
    if isinstance(entity_id, (list, tuple)):
        return tuple(entity_id)
    return entity_id


class AudioWindow:
    """The last `max_bytes` of one mic's raw audio, safe to share between the
    thread that appends to it and the one that reads it.

    Positions are absolute byte offsets into the mic's stream since start, so
    the reader can drop exactly the audio it snapshotted and acted on while
    keeping whatever arrived after the snapshot.
    """

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes
        self._buf = bytearray()
        self._end = 0  # absolute offset just past the newest byte
        self._lock = Lock()

    def append(self, chunk):
        with self._lock:
            self._buf.extend(chunk)
            self._end += len(chunk)
            excess = len(self._buf) - self.max_bytes
            if excess > 0:
                del self._buf[:excess]

    def snapshot(self):
        """Return (audio currently in the window, absolute end offset)."""
        with self._lock:
            return bytes(self._buf), self._end

    def discard_until(self, pos):
        """Drop buffered audio before absolute offset `pos`."""
        with self._lock:
            drop = len(self._buf) - (self._end - pos)
            if drop > 0:
                del self._buf[:drop]

    def reset(self):
        """Drop everything, e.g. when the audio stream restarts after a gap."""
        with self._lock:
            self._buf.clear()


class WindowCommander:
    """Turns each round's per-mic window transcripts into commands, at most
    once per utterance.

    * A command fires only after `stable_count` consecutive windows from the
      same mic parse to the same (entity, action), each with no more words
      than the one before. A window may end mid-phrase: "turn off the light" and
      then "turn off the light in the elec" both parse as the mic's own room, but the
      text grew, so the phrase isn't finished and the streak starts over
      until "…in the electrical room" settles.
    * When a command fires, the audio every mic has buffered up to this round
      is discarded, so neither this mic nor another one that heard the same
      words acts on them again, including in a later window that cuts the
      phrase differently (e.g. "turn on the light [in the garden]" read as "turn on the light").
    * The same (entity, action) from any mic within `dedupe_sec` of an
      execution is dropped. This backstops a mic whose stream lags and
      delivers part of the phrase only after the discard.
    """

    def __init__(self, match, stable_count=2, dedupe_sec=5.0):
        # match(text, source_name) -> (success, entity_id, action)
        self.match = match
        self.stable_count = stable_count
        self.dedupe_sec = dedupe_sec
        self._streaks = {}  # source_name -> (command key, consecutive windows, word count)
        self._last_executed = {}  # command key -> time of last execution

    def process_round(self, results, now):
        """Take one round of (source_name, text, end_pos) for every mic.

        Returns (commands, discards): `commands` is a list of
        (source_name, entity_id, action, text) to execute, `discards` maps a
        source_name to the absolute offset its window must drop audio up to.
        """
        commands, discards = [], {}
        for name, text, end in results:
            success, entity_id, action = (
                self.match(text, name) if text.strip() else (False, None, None)
            )
            key = (entity_key(entity_id), action) if success else None
            words = len(text.split())
            prev_key, count, prev_words = self._streaks.get(name, (None, 0, 0))
            if key is not None and key == prev_key and words <= prev_words:
                count += 1
            else:
                count = int(key is not None)
            self._streaks[name] = (key, count, words)
            if count < self.stable_count:
                continue

            # The phrase is complete in this mic's window; consume it either way.
            self._streaks[name] = (None, 0, 0)
            discards[name] = end
            last = self._last_executed.get(key)
            if last is not None and now - last < self.dedupe_sec:
                print(
                    f"[{name}] dedupe: {action} {entity_id} already executed "
                    f"{now - last:.2f}s ago, skipping"
                )
                continue

            self._last_executed[key] = now
            commands.append((name, entity_id, action, text))
            # One utterance, one action: consume what every mic has heard so
            # far. The rest of this round's windows hold that same audio.
            for other, _, other_end in results:
                discards[other] = other_end
                self._streaks[other] = (None, 0, 0)
            break
        return commands, discards
