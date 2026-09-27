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

    * One window that parses to a command is enough to run it. The command
      is held for `hold_hops` rounds first, so that a room named after the
      device can still arrive: a window ending right after "turn on the light"
      already reads as a complete command for the mic's own room, and the
      next one may say "turn on the light in the yard". During the hold the latest
      window that parses to a command replaces the held one; a window that
      parses to nothing (misheard, noise) never cancels it. If the text is
      still growing when the hold ends, it waits one more round.
      `hold_hops=0` runs a command the moment a window matches.
    * When a command runs, the audio every mic has buffered up to this round
      is discarded and every held command dropped, so neither this mic nor
      another one that heard the same words acts on them again, including in
      a later window that cuts the phrase differently (e.g. "turn on the light [in the garden]"
      read as "turn on the light").
    * The same (entity, action) from any mic within `dedupe_sec` of an
      execution is dropped. This backstops a mic whose stream lags and
      delivers part of the phrase only after the discard.
    """

    def __init__(self, match, hold_hops=1, dedupe_sec=5.0):
        # match(text, source_name) -> (success, entity_id, action)
        self.match = match
        self.hold_hops = hold_hops
        self.dedupe_sec = dedupe_sec
        self._held = {}  # source_name -> held command, see process_round
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
            words = len(text.split())
            held = self._held.get(name)
            if held is None:
                if not success:
                    continue
                held = self._held[name] = {"hops": 0, "words": words}
            else:
                held["hops"] += 1
            grew = words > held["words"]
            held["words"] = words
            if success:
                held["command"] = (entity_id, action, text)
            # Wait out the hold, plus one round if the phrase is still growing
            if held["hops"] < self.hold_hops or (grew and held["hops"] == self.hold_hops):
                continue

            # Consume the command's audio whether it runs or is a duplicate.
            entity_id, action, heard = held["command"]
            del self._held[name]
            discards[name] = end
            key = (entity_key(entity_id), action)
            last = self._last_executed.get(key)
            if last is not None and now - last < self.dedupe_sec:
                print(
                    f"[{name}] dedupe: {action} {entity_id} already executed "
                    f"{now - last:.2f}s ago, skipping"
                )
                continue

            self._last_executed[key] = now
            commands.append((name, entity_id, action, heard))
            # One utterance, one action: consume what every mic has heard so
            # far. The rest of this round's windows hold that same audio.
            for other, _, other_end in results:
                discards[other] = other_end
            self._held.clear()
            break
        return commands, discards
