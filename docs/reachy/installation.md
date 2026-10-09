# Installation

[Getting started](getting-started.md) is the short path. This page covers the choices it
skips: which systems work, installing without cloning the repository, Linux preparation,
local speech recognition, and how to check an installation.

## Supported systems

| Wactorz runs on | Movement and speech | Listening, conversation, camera | Notes |
| --- | --- | --- | --- |
| Windows 10/11 | Yes | Yes | The robot SDK brings its own media libraries. |
| macOS | Yes | Yes | As Windows. |
| Linux | Yes | **No**, unless you build GStreamer's WebRTC plugin (`gst-plugins-rs`) yourself | Python 3.13 or older. Tested on Ubuntu 26.04 with Python 3.13 and a Reachy Mini Wireless. |
| Docker image, Home Assistant add-on | Not supported | Not supported | The `ultra` image carries the SDK's build tools, but running Reachy from a container has not been validated. |
| WSL, virtual machines | Possible | As the host OS | Automatic discovery cannot reach the robot; set `REACHY_ROBOT_HOST`. |

| Robot | Connection | What you run |
| --- | --- | --- |
| Reachy Mini **Wireless** | Same Wi-Fi network as the computer | Nothing extra: the daemon runs on the robot. |
| Reachy Mini **Lite** | USB cable | `reachy-mini-daemon -p <serial_port>` on the computer, left running. |

Python: Wactorz supports 3.10 and newer; the robot SDK is pinned to `reachy-mini==1.8.4`
and is used with Python 3.13 in testing. Prefer 3.13 everywhere.

## Option A: from a clone, with the lock file (recommended)

This installs the exact versions the project tests with, including a security override
for one of the SDK's web dependencies that plain `pip` does not apply.

```bash
git clone https://github.com/waldiez/wactorz.git
cd wactorz
uv sync --locked --extra anthropic --extra reachy
uv run --no-sync wactorz
```

Use the extra for your language-model provider in place of `anthropic`: `openai`,
`google` or `nim`. Ollama needs none. `.env` goes in the `wactorz` folder.

## Option B: from PyPI with pip

```bash
python -m venv .venv
```

Activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` elsewhere),
then:

```bash
pip install 'wactorz[anthropic,reachy]'
wactorz
```

**Keep the virtual environment inside the folder that holds your `.env`.** An installed
Wactorz looks for `.env` starting from its own install location and walking up the
folders, not from the folder you start it in. A `.venv` inside your project folder puts
`.env` on that path.

## Adding Reachy to an existing Wactorz

Install the extra into the same environment and restart Wactorz once:

```bash
pip install 'wactorz[reachy]'
```

Without it, `@catalog spawn reachy-mini` installs the same packages on first use. That
works, takes a few minutes, and usually asks for one restart: the robot SDK needs an older
version of a library Wactorz has already loaded. Installing the extra first avoids both.

## Linux

The robot SDK compiles part of itself on Linux. On Debian or Ubuntu, install the build
tools and GStreamer first:

```bash
sudo apt install python3-venv python3-dev build-essential pkg-config \
  libcairo2-dev libgirepository1.0-dev \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-nice gstreamer1.0-libav \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0
```

Use Python 3.13 or older: the SDK needs a PyGObject release that does not work on 3.14,
which is the default on Ubuntu 26.04. With uv:

```bash
uv venv --seed -p 3.13 .venv
.venv/bin/pip install 'wactorz[anthropic,reachy]'
```

Wactorz then connects without Reachy's camera and microphone and says so in its log.
Movement and speech work. Listening, conversation and the camera are refused with an
explanation until GStreamer's WebRTC plugin (`gst-plugins-rs`) is installed, which
Linux distributions do not package.

## Optional extras

| Want | Install | Then set in `.env` |
| --- | --- | --- |
| Louder speech | `ffmpeg` on the computer running Wactorz: `winget install ffmpeg`, `brew install ffmpeg`, or `sudo apt install ffmpeg` | nothing; restart Wactorz |
| Voice input through Deepgram (default) | included in the `reachy` extra | `DEEPGRAM_API_KEY` |
| Voice input that never leaves the computer | `uv pip install faster-whisper` (or `pip install faster-whisper`) | `REACHY_STT_BACKEND=faster-whisper` |
| Voice input through OpenAI | the `openai` extra | `REACHY_STT_BACKEND=openai` and `OPENAI_API_KEY` |

The first local transcription downloads the Whisper model, which can take minutes and
several hundred megabytes. Do it once before a demonstration, not during one.

In an environment made by `uv sync`, a later `uv sync` removes packages that are not in
the lock, `faster-whisper` included. Install it again afterwards, or run
`uv sync --inexact` instead.

## The message broker

Wactorz needs an MQTT broker. From a clone, the bundled development broker runs in
Docker, listens only on this computer, and uses `MQTT_USERNAME` and `MQTT_PASSWORD`
from `.env`:

```bash
docker compose -f compose.dev.yaml up -d mosquitto
```

Any Mosquitto 2.x broker works instead: set `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME` and
`MQTT_PASSWORD` to match it.

## Checking the installation

Without a robot, the repository can run the agent against the SDK's simulated robot.
This checks that the agent and the SDK work together; it says nothing about motors,
audio or the camera:

```bash
uv run --no-sync python scripts/reachy_sim_check.py
```

Every line should say `PASS`. It needs port 8000 free for the duration of the run.

With the robot, spawn the agent and send `@reachy-mini health` (see
[Getting started](getting-started.md#6-add-reachy)).

## Updating

From a clone:

```bash
git pull
uv sync --locked --extra anthropic --extra reachy
```

Then restart Wactorz. Reachy restarts with it.
