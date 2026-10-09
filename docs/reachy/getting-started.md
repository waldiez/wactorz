# Getting started with Reachy Mini

This guide takes you from nothing to a robot that moves and talks when you type to it.
Voice input comes last and is optional. Allow about 30 minutes the first time.

You do not need to know Wactorz or the Reachy SDK. You do need to be comfortable typing
commands into a terminal and editing a text file.

## What you need

| Item | Notes |
| --- | --- |
| A Reachy Mini | **Wireless** (Wi-Fi) or **Lite** (USB). Charged or plugged in. |
| A computer to run Wactorz | **Windows or macOS** for every feature. Linux works for movement and speech; see [Installation › Linux](installation.md#linux) for what is missing there. |
| Python 3.10 to 3.13 | 3.13 is what the robot SDK is tested with. On Linux, 3.14 does not work yet. |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installs the exact, tested versions of every package. |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Only to run the message broker (Mosquitto) Wactorz needs. Any Mosquitto 2.x broker works instead. |
| A language-model API key | Anthropic, OpenAI, Google Gemini or NVIDIA NIM, or a local [Ollama](https://ollama.com). This guide uses Anthropic. |
| Internet access | Reachy's voice is synthesized by an online service (Microsoft Edge text-to-speech). |
| *(Optional)* A [Deepgram](https://console.deepgram.com) API key | Only to talk **to** Reachy by voice. A fully local alternative exists. |

> **Not supported:** running Reachy from the Wactorz Docker image or the Home Assistant
> add-on. Run Wactorz directly on the computer, as below.

## 1. Install Wactorz with Reachy's packages

```bash
git clone https://github.com/waldiez/wactorz.git
cd wactorz
uv sync --locked --extra anthropic --extra reachy
```

Replace `anthropic` with `openai`, `google` or `nim` if you use that provider; Ollama needs
no extra. This creates a `.venv` folder in `wactorz/` with everything installed. On Linux,
first do the preparation in [Installation › Linux](installation.md#linux).

## 2. Configure

Copy the configuration template:

```bash
cp .env.template .env
```

On Windows PowerShell use `Copy-Item .env.template .env` instead.

Open `.env` in a text editor and set these lines:

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6
LLM_API_KEY=your-anthropic-key
MQTT_USERNAME=wactorz
MQTT_PASSWORD=choose-a-long-random-password
```

If your robot is a **Reachy Mini Wireless**, also set its address. Find it in your
router's list of connected devices; `reachy-mini.local` often works too:

```dotenv
REACHY_CONNECTION_MODE=network
REACHY_ROBOT_HOST=192.168.1.42
```

`.env` holds secrets. Never commit it or share it; the repository already ignores it.

## 3. Start the message broker

Wactorz's agents talk to each other through an MQTT broker. Start the one that ships with
the repository (Docker Desktop must be running):

```bash
docker compose -f compose.dev.yaml up -d mosquitto
```

It reads `MQTT_USERNAME` and `MQTT_PASSWORD` from your `.env`, listens only on this
computer, and keeps running in the background until you stop it.

## 4. Prepare the robot

**Reachy Mini Wireless**

1. Power the robot on and wait for it to join your Wi-Fi.
2. Make sure the computer is on the **same network**. Guest networks often stop devices
   from seeing each other.
3. Close the Reachy Mini desktop app and stop any Hugging Face app running on the robot.
   Only one program can control Reachy at a time.

**Reachy Mini Lite**

1. Connect the robot to the computer by USB.
2. Find its serial port: something like `COM3` on Windows or `/dev/ttyACM0` on Linux.
3. In a second terminal, from the `wactorz` folder, start the robot's daemon and leave it
   running:

   ```bash
   uv run --no-sync reachy-mini-daemon -p COM3
   ```

## 5. Start Wactorz

```bash
uv run --no-sync wactorz
```

Open the dashboard at <http://localhost:8888>. The chat is where you will talk to Reachy.

## 6. Add Reachy

In the dashboard chat, send:

```text
@catalog spawn reachy-mini
```

The reply should be **"reachy-mini spawned and running"**. The very first start takes up
to a minute longer while the robot SDK downloads Reachy's library of recorded gestures.

Check the link:

```text
@reachy-mini health
```

A good answer starts with **"I'm connected to my body"**. If it says it is not connected,
see [Troubleshooting › Reachy does not connect](troubleshooting.md#reachy-does-not-connect).

## 7. Your first conversation, typed

Send these one at a time and watch the robot:

```text
@reachy-mini wake up
@reachy-mini look left
@reachy-mini nod
@reachy-mini say Hello, I am Reachy!
@reachy-mini what can you do?
```

Each command answers in the chat too, so a movement that failed never looks like one that
worked. Questions that are not robot commands, such as "what's the weather in Athens?", are
answered by Wactorz and spoken by Reachy.

If the voice is quiet, say `presenter mode`, or install `ffmpeg` (see
[Troubleshooting](troubleshooting.md#reachys-voice-is-quiet)).

## 8. Optional: talk to Reachy by voice

Voice input needs a speech-recognition service. The default is **Deepgram**, a hosted
service: **your voice recordings are sent to Deepgram** while voice input is in use. To
keep audio on your computer instead, see
[Configuration › Speech recognition](configuration.md#speech-recognition).

1. Create an API key at [console.deepgram.com](https://console.deepgram.com).
2. Add it to `.env`:

   ```dotenv
   DEEPGRAM_API_KEY=your-deepgram-key
   ```

3. Restart Wactorz (`Ctrl+C` in its terminal, then `uv run --no-sync wactorz` again).
   Reachy comes back by itself.
4. Ask one question by voice. Reachy records five seconds, then answers out loud:

   ```text
   @reachy-mini listen and ask Wactorz
   ```

5. Or start a hands-free conversation. Speak normally, one turn at a time; say
   **"goodbye"** to end it:

   ```text
   @reachy-mini start conversation
   ```

If voice input is not set up correctly, Reachy says exactly what is missing instead of
starting to listen.

## Shutting down

1. If a conversation is running, say "goodbye" or send `@reachy-mini stop conversation`.
2. Optionally send `@reachy-mini sleep` so the head settles.
3. Press `Ctrl+C` in the Wactorz terminal.
4. Stop the broker when you are done for the day:

   ```bash
   docker compose -f compose.dev.yaml down
   ```

5. Power the robot off with its switch. For a Lite, also stop the daemon with `Ctrl+C`.

Reachy is remembered: the next time Wactorz starts, `reachy-mini` starts with it.

## Next steps

- Everything Reachy understands: [User guide](user-guide.md), or send `@reachy-mini help`.
- Something not working: [Troubleshooting](troubleshooting.md).
- Showing Reachy to an audience: [Demonstration guide](demo-guide.md).
