# Home Assistant Voice Control

A voice-controlled system for Home Assistant that listens for spoken English commands, transcribes them with a speech-to-text server, and sends the appropriate commands to your Home Assistant instance.

## Features

- **Continuous Audio Monitoring**: Listens on one or several mics at once, each read from any process that writes raw s16le to stdout (local `arecord`/`parecord` or `ffmpeg` from an RTSP camera)
- **Speech Detection**: Automatically detects when someone is speaking based on volume threshold (VAD mode), or skips detection and continuously transcribes a sliding window of every mic (window mode) — see below
- **Speech-to-Text**: Transcribes spoken commands via a small HTTP API (see [Transcription API](#transcription-api)); developed against NVIDIA Parakeet TDT 0.6B v2, English
- **Command Processing**: Parses transcribed text to identify actions, devices, and rooms
- **Home Assistant Integration**: Sends commands to Home Assistant via its REST API, with support for toggling multiple entities in a single call
- **Audio Feedback**: Optional Piper TTS and confirmation sounds (can be disabled on hosts without speakers)
- **Systemd Service**: Can be run as a background service on Linux systems

## Requirements

- Linux, Python 3.8+
- One of: ALSA/PulseAudio for a local mic, or `ffmpeg` for an RTSP camera audio track
- A speech-to-text HTTP server that implements the [Transcription API](#transcription-api), local or remote
- Home Assistant instance with API access
- Optional: Piper TTS server and an OpenAI API key for the AI assistant branch

Python packages (`requirements.txt`): numpy, requests and openai. `openai` is
imported at startup but only called by the AI assistant branch.

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/stepankandrushin/homeassistant-voice.git
   cd homeassistant-voice
   ```

2. Create and activate a virtual environment:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Create a configuration file:
   ```
   cp config.py.sample config.py
   ```

5. Edit `config.py` to configure your:
   - Transcription server URL
   - Home Assistant URL and access token. Generate Long-lived access token in the bottom of http://homeassistant.local:8123/profile/security
   - Audio recording settings
   - Commands and device mappings

## Configuration

The `config.py` file contains all the configuration options. `config.py.sample`
lists them with comments.

### Transcription API
```python
TRANSCRIPTION_API_URL = "http://localhost:8889/transcribe"
```
The server must provide:

- `POST /transcribe` — a multipart form with one WAV file in the `audio`
  field; returns JSON `{"text": "..."}`.
- `POST /transcribe_batch` (window mode only) — a multipart form with
  several WAV files, each in its own `audio` field; returns JSON
  `{"texts": ["...", ...]}`, one text per file, in the order sent. Its URL
  defaults to `TRANSCRIPTION_API_URL + "_batch"`; set
  `TRANSCRIPTION_BATCH_API_URL` to override it.

Texts should be lowercase and without punctuation: aliases are matched
against them as they are. The clips are mono 16-bit WAV at `SAMPLE_RATE`
(16 kHz).

### Audio Source Configuration
```python
# Local ALSA mic:
AUDIO_RECORD_CMD = ["arecord", "-D", "dsnoop:CARD=MS,DEV=0",
                    "-r", "16000", "-c", "1", "-f", "S16_LE", "-t", "raw"]

# Or a Hikvision RTSP camera audio track:
AUDIO_RECORD_CMD = [
    "ffmpeg", "-loglevel", "quiet", "-rtsp_transport", "tcp",
    "-i", "rtsp://user:pass@camera.local:554/Streaming/Channels/102",
    "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
    "-f", "s16le", "-",
]
```
Anything that writes raw `s16le` @ `SAMPLE_RATE` to stdout will work. A
source whose process exits or stops sending audio is restarted.

#### Several mics

`AUDIO_RECORD_CMDS` replaces `AUDIO_RECORD_CMD` and runs every source at
once. As a dict, its keys name the mics in logs and in the per-mic settings
below; as a plain list, the mics are named `mic1`, `mic2`, …

```python
AUDIO_RECORD_CMDS = {
    "kitchen": ["ffmpeg", ..., "rtsp://user:pass@kitchen-camera.local:554/...", ...],
    "garden":  ["ffmpeg", ..., "rtsp://user:pass@garden-camera.local:554/...", ...],
}

# A command that names no room goes to the room of the mic that heard it
# (default_room for mics not listed here).
source_rooms = {"kitchen": "kitchen", "garden": "garden"}

# Per-mic speech threshold in dB (VAD mode), overriding DB_THRESHOLD.
source_db_thresholds = {"kitchen": 62}

# Two mics hearing one phrase: the same entity + action runs only once
# within this many seconds.
DEDUPE_WINDOW_SEC = 2.0
```

### Home Assistant Configuration
```python
HOMEASSISTANT_URL = "http://homeassistant.local:8123"
HOMEASSISTANT_TOKEN = "YOUR_LONG_LIVED_ACCESS_TOKEN"
```

### Audio Configuration
```python
DB_THRESHOLD = 50  # Speech detection threshold in dB
SILENCE_THRESHOLD_MS = 500  # Silence duration threshold in ms
MIN_RECORDING_LENGTH_SEC = 1.0  # Minimum recording length to process
PREROLL_DURATION_SEC = 0.5  # Audio kept from before speech was detected
SAVE_RECORDINGS_TO_DISK = False  # True: also save each utterance as a WAV
RECORDINGS_DIR = "recordings"  # ... under RECORDINGS_DIR/YYYY/MM/DD/<mic>_<time>.wav
```

Every transcript is appended to `transcripts/<mic>.log` (directory set by
`TRANSCRIPTS_DIR`).

### Sounds, TTS and the AI assistant
```python
HOMEASSISTANT_SOUND = "sounds/homeassistant.wav"  # played when a command runs; "" for none
AI_SOUND = "sounds/ai.wav"                        # played when the AI assistant is called
AUDIO_PLAY_CMD = ["aplay", ...]                   # plays those files
TTS_API_URL = "http://localhost:5000"             # Piper TTS HTTP server
VOICE_PLAY_CMD = ["aplay", ..., "-r", "22050", ...]  # plays the TTS audio

ai_assistant_names = ["smarty"]  # wake words
OPENAI_API_KEY = "sk-..."
```

In VAD mode, an utterance that is not a Home Assistant command and starts
with one of `ai_assistant_names` ("smarty, what's the capital of France")
goes to OpenAI (GPT-4o), and the answer is spoken through the TTS server.

### Window mode (noisy mics)

With `STT_MODE = "vad"` (the default) an utterance is cut out when the level
crosses `DB_THRESHOLD` and transcribed once. When a mic's noise floor sits
close to speech level (a fan, a far camera), quieter syllables fall below the
threshold and phrases get chopped (`turn on` | `the light`), so commands are missed.

`STT_MODE = "window"` drops the threshold: each mic keeps its last
`WINDOW_SEC` of audio, and every `WINDOW_HOP_SEC` the last window of every
mic goes to the transcription server in **one batch** (`/transcribe_batch`).
A spoken command lands whole in several
consecutive windows, so a bad cut or a misheard window no longer loses it.

```python
STT_MODE = "window"
WINDOW_SEC = 5.0           # audio per window; must fit your longest command
WINDOW_HOP_SEC = 0.5       # how often every mic is re-transcribed
WINDOW_HOLD_HOPS = 1       # rounds a room-less command waits for a room word (0 = run at once)
# TRANSCRIPTION_BATCH_API_URL defaults to TRANSCRIPTION_API_URL + "_batch"
```

Each command runs from a single matching window, and each utterance
executes once (`utils/window.py`, tests in `tests/test_window.py`):

- one window that parses to a command is enough. A command that names its
  room ("turn off the light in the garden") runs at once. One that doesn't
  is held for `WINDOW_HOLD_HOPS` rounds (default 1, i.e. 0.5 s) because a
  window ending right after "turn on the light" already reads as a command
  for the mic's own room, and the next window may add "in the garden".
  During the hold the latest window that parses to a command replaces the
  held one; a window that parses to nothing (misheard, noise) never cancels
  it. If the text is still growing when the hold ends ("… in the gar"), it
  waits one more round. A room-less reading that ends in "in", "at" or
  "the" ("turn off the light in the") was cut off before its room: it is
  not a command, and a held one doesn't run on it ("on" doesn't count:
  "turn the light on" is complete);
- when several mics are ready in the same round, a reading that names its
  room beats one that fell back to the mic's own room. Two mics in one room
  hear the same phrase, and the noisier one may read the room word as
  gibberish ("in the gordon" for "in the garden"); that reading must not send the
  command to the living room;
- once it fires, the audio every mic has buffered so far is discarded, so
  neither that mic nor another mic that heard the same words acts on them
  again (one utterance, one action — even when two mics with different
  default rooms heard it);
- the same entity + action is deduped across mics for
  `max(DEDUPE_WINDOW_SEC, WINDOW_SEC)` as a backstop for a lagging stream.

A command that names its room reaches HA in the same round as the first
window that holds the whole phrase; one without a room, a hop (0.5 s)
later. Window mode handles Home Assistant commands only — the AI
wake-word branch needs whole utterances, which only VAD mode produces.
Transcript logs record a mic's window text only when it changes.

### Command Configuration

Define aliases for actions, devices, and rooms. An alias is a substring of
the lowercase transcript, or a compiled regex (`re.compile(...)`) searched in
it — for what a substring can't say, such as a particle verb split around
its object ("turn the light on") or a short word that must not match inside
others ("ac" in "back"):

```python
import re

# Action aliases (turn on/off). The words between "turn"/"switch" and
# "on"/"off" may not be "on"/"off", so "turn off the light on the terrace"
# stays turn_off.
action_aliases = {
    "turn_on": ["turn on", "switch on", "enable", "start",
                re.compile(r"\b(?:turn|switch) (?:(?!on\b|off\b)\w+ ){1,5}on\b")],
    "turn_off": ["turn off", "switch off", "disable", "stop",
                 re.compile(r"\b(?:turn|switch) (?:(?!on\b|off\b)\w+ ){1,5}off\b")],
}

# Device aliases
device_aliases = {
    "ac": [re.compile(r"\ba ?c\b"), "air conditioner", "air conditioning"],
    "light": ["light", "lamp"],
    "tv": ["tv", "television"]
}

# Room aliases
room_aliases = {
    "office": ["office", "study"],
    "bedroom": ["bedroom"],
    "living_room": ["living room", "lounge"]
}
```

The first matching entry wins in each dict, so order matters: e.g. list a
bare-room pseudo-device like "pool" after the real devices.

Map devices to Home Assistant entity IDs. A device may be mapped to a
single entity ID or to a **list** of entity IDs that share a domain — a
single voice command will then toggle all of them in one HA API call.

```python
room_entities = {
    "office": {
        "ac": "climate.office_ac",
        "light": "light.office_main",
    },
    "garden": {
        # Two-relay switch — one command hits both outputs
        "light": ["switch.garden_switch", "switch.garden_switch_2"],
    },
}
```

A device entry may also be a dict `{action: entity(s)}` when one action
should hit a different set than another — e.g. a light whose `turn_off`
also switches off a pump while `turn_on` doesn't. A `"default"` key covers
the actions not listed.

When a command names no room, it goes to the room of the mic that heard it
(`source_rooms`), else to `default_room`. A device listed in
`devices_without_room` is instead looked up across all rooms, so "turn on
the AC" finds it wherever it is.

#### "Everywhere" / cross-room commands

To make a phrase like `turn on the light everywhere` trigger every
room's light at once, add a **virtual room**
to `room_aliases` and `room_entities` whose device entry is a flat list
of every entity to hit. No code changes needed — the existing room
matching plus list-entity fan-out already does it.

```python
room_aliases = {
    "office":     ["office"],
    "garden":     ["garden", "outside"],
    "everywhere": ["everywhere", "all lights", "all the lights"],
}

room_entities = {
    "office":  {"light": "light.office_main"},
    "garden":  {"light": ["switch.garden_switch", "switch.garden_switch_2"]},
    "everywhere": {
        "light": [
            "light.office_main",
            "switch.garden_switch", "switch.garden_switch_2",
        ],
    },
}
```

Keep the `everywhere` lists in sync when adding entities to real rooms.
All entities in one list must share a domain.

## Usage

### Running Manually

```
python main.py
```

The program will start listening for voice commands. In VAD mode, when it detects speech, it will:
1. Record the audio
2. Transcribe it via the transcription server
3. Process the transcription to identify commands
4. Play the confirmation sound and send the command to Home Assistant

In window mode it transcribes every mic's last few seconds every
`WINDOW_HOP_SEC` instead (see [Window mode](#window-mode-noisy-mics)).

### Running as a Service

1. Edit `homeassistant-voice.service` to match your installation: it assumes
   user `pi` and the checkout in `/home/pi/homeassistant-voice`
2. Copy the service file to systemd:
   ```
   sudo cp homeassistant-voice.service /etc/systemd/system/
   ```
3. Enable and start the service:
   ```
   sudo systemctl enable homeassistant-voice.service
   sudo systemctl start homeassistant-voice.service
   ```
4. Check the status:
   ```
   sudo systemctl status homeassistant-voice.service
   ```
5. Check logs:
   ```
   sudo journalctl -u homeassistant-voice.service
   
   # To see only the most recent logs:
   sudo journalctl -u homeassistant-voice.service -n 50
   
   # To follow the logs in real-time (like tail -f):
   sudo journalctl -u homeassistant-voice.service -f
   
   # To see logs since the last boot:
   sudo journalctl -u homeassistant-voice.service -b
   ```

### Service Management and Graceful Shutdown

The service supports graceful shutdown when stopped or restarted via systemctl:

```bash
# Stop the service
sudo systemctl stop homeassistant-voice.service

# Restart the service
sudo systemctl restart homeassistant-voice.service
```

When the service receives a shutdown signal (SIGTERM):
1. It stops accepting new voice commands
2. Stops the audio capture threads cleanly
3. Stops any currently playing TTS audio
4. Clears any pending TTS queue items
5. Exits gracefully with proper cleanup

This ensures that:
- No audio processes are left running
- No partial recordings are processed
- The service can be restarted cleanly without issues

### Restart when a USB audio device is plugged back in

If a USB speakerphone sometimes drops off the bus, a udev rule can restart
the service (and tell you over Telegram) whenever the device comes back.
`99-usb-jabra.rules` does this for a Jabra Speak2 40.

1. Find your device's vendor and product IDs: unplug and replug it, then
   run `dmesg | tail` (or `lsusb`) and note `idVendor` / `idProduct`.
2. Set up the notification script and fill in your Telegram bot token and
   chat ID:
   ```
   cp report.sh.sample report.sh
   chmod +x report.sh
   ```
3. Edit `99-usb-jabra.rules`: put in your `idVendor` / `idProduct` and the
   path to `report.sh`, then install it:
   ```
   sudo cp 99-usb-jabra.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules
   sudo udevadm trigger
   ```

### Testing

#### Unit tests

```bash
python run_tests.py
```

Runs the command-matching and window-mode tests. They need no `config.py`,
no servers and no audio devices. `tests/test_ai_integration.py` makes a
real OpenAI call and is skipped unless `config.py` and the `openai` package
are available.

#### Manual scripts

These run against your real `config.py`:

- `python tests/test_shutdown.py` — starts `main.py`, sends it SIGTERM and
  checks that it exits cleanly
- `python tests/test_tts.py`, `python tests/test_tts_debug.py`,
  `python tests/test_tts_direct.py` — speak test phrases through the TTS
  server and your speakers (see `tests/TTS_TEST_README.md`)

#### Testing Transcription

Send a WAV file to the transcription server, e.g. one saved with
`SAVE_RECORDINGS_TO_DISK = True`:

```
python transcribe.py recordings/2025/02/27/mic_20250227_101030.wav
```

### Controlling an entity from the command line (`ha.py`)

`ha.py` is a small helper that calls the Home Assistant REST API using
the URL and token from `config.py`. Useful when you need to probe which
relay is which (e.g. identifying the two outputs on a dual-relay switch)
or to script a quick toggle outside the voice pipeline.

```
# Toggle (no second argument)
./ha.py switch.garden_switch

# Explicit on / off (accepts on|1|true and off|0|false)
./ha.py switch.garden_switch on
./ha.py switch.garden_switch_2 off
./ha.py light.office_main 1
./ha.py climate.office_ac 0
```

The entity's domain (`switch`, `light`, `climate`, …) is inferred from
the part before the dot, so any entity that supports
`turn_on`/`turn_off`/`toggle` works.

## Voice Command Format

The system recognizes commands in the format:
- `[action] [device] in [room]`

Examples:
- "Turn on the light in the office"
- "Turn off the AC in the bedroom"
- "Turn the TV on" (with the particle-verb regexes above)

The room is optional: without one, the command goes to the mic's room
(`source_rooms`) or `default_room`, and a device in `devices_without_room`
is found in whichever room has it.

## Troubleshooting

### Audio Issues

- Make sure your microphone is properly connected and configured
- Adjust the `DB_THRESHOLD` value in `config.py` if speech detection is too sensitive or not sensitive enough. With several mics, override it per source via `source_db_thresholds` (a few dB above each mic's idle noise floor)
- If a mic's noise floor is too close to speech level for any threshold to work, switch to `STT_MODE = "window"` (see Window mode)
- Check the ALSA/PulseAudio configuration in `config.py` to match your system

### Transcription Issues

- Verify that your transcription server is running and accessible
- Check the server logs for any errors
- Try testing with the `transcribe.py` script to isolate issues

### Home Assistant Issues

- Verify that your Home Assistant URL and token are correct
- If `homeassistant.local` doesn't resolve (no mDNS on your network or host), use the Home Assistant IP address instead
- Check that the entity IDs in your configuration match those in Home Assistant
- Ensure that your Home Assistant instance is running and accessible

## Contributing

Project overview and conventions for contributors: CLAUDE.md

## License

MIT — see [LICENSE](LICENSE).
