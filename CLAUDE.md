# homeassistant-voice

Voice control for Home Assistant, English only: capture audio → detect
speech → transcribe (transcription_api, Parakeet TDT 0.6B v2) → match
action/device/room aliases → call HA REST API.

Entry point: `main.py`. Each audio input runs in its own `SpeechSource`
thread that spawns a subprocess writing raw s16le to stdout, detects speech
by dB threshold (global `DB_THRESHOLD`, per-mic override in
`config.source_db_thresholds`), transcribes the utterance, and pushes the result onto a
shared queue. The main loop reads from the queue, matches commands via
`utils.homeassistant.process_command`, and dedupes repeated commands (same
entity + action) inside `DEDUPE_WINDOW_SEC` (default 2 s) so two mics in
the same room don't trigger twice. That is VAD mode (`STT_MODE = "vad"`,
the default).

In window mode (`STT_MODE = "window"`) there is no
threshold: the threads only fill a `WINDOW_SEC` ring buffer, and the main
loop sends every mic's window to the transcription server's
`/transcribe_batch` every `WINDOW_HOP_SEC`.
`utils/window.py::WindowCommander` guarantees one execution per utterance
(one matching window runs a command: at once if it names its room, else
after a one-round hold for a trailing room word; a reading that names its
room beats another mic's room-less one; discard-all-mics on fire; cross-mic
dedupe). Any change there must keep `tests/test_window.py` passing.

Configure one or many sources via `config.AUDIO_RECORD_CMD` (single) or
`config.AUDIO_RECORD_CMDS` (list of commands, or dict `{name: cmd}` for
nicer log labels). Swap the command to change the audio source — local
`arecord` / `parecord` or `ffmpeg` from an RTSP stream — without touching
code.

## Conventions

- This repo is public. Host names, IPs, credentials and anything about one
  particular deployment stay out of tracked files and commit messages; a
  gitignored `CLAUDE.local.md` may say where such notes live.
- `config.py` is **not** tracked — each deployment has its own. Start from
  `config.py.sample`.
- To ship a code change to a deployment host, commit + push + `git pull` on
  the host. Do not rsync working-tree files.
- `send_homeassistant_command(entity_id, service)` accepts either a string
  or a list of entity IDs sharing a domain; `config.room_entities` may map
  a device to a list when one logical command should hit several entities.
- **Config over code in command routing.** Before adding any branch to
  `utils/homeassistant.py::process_command`, check whether the behavior falls
  out of (a) a new `room_aliases` alias, (b) a `room_entities` entry (single
  ID, list, or per-action dict), or (c) a virtual room combining the two.
  Only reach for code when the config model genuinely can't express it — the
  user explicitly pushed back on an `everywhere_aliases` code path because
  the same behavior fell out of existing room-match + list fan-out; they want
  the command-routing kernel to stay small, with capability growing via
  config. The bullets below are instances of this rule.
- An alias (action, device or room) is a substring of the lowercase
  transcript or a compiled regex searched in it (`homeassistant.alias_in`).
  English particle verbs split around their object ("turn the light on")
  are regex aliases in `action_aliases`, not matching code; so are short
  words that must not match inside others (`\ba ?c\b`, `\bfans?\b`).
- A `room_entities[room][device]` value may also be a `{action: entity(s)}`
  dict when one action should target a different set than another (e.g. the
  pool `light` whose `turn_off` also kills the jacuzzi while `turn_on`
  doesn't). `process_command` resolves the dict by the spoken action, falling
  back to a `"default"` key if present. Prefer this over special-casing in
  `process_command`.
- **Cross-room / "everywhere" commands are config, not code.** When you
  need a phrase like `turn off all the lights` to hit every room's light, add
  a virtual room (e.g. `"everywhere"`) to `room_aliases` and `room_entities`
  whose device entry is a flat list of every entity to trigger. The
  existing room-match + list-entity fan-out handles the rest — do **not**
  add special-case logic in `process_command`.
