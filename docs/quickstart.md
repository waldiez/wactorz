# How to run Wactorz on Windows

`scripts\start.bat` and `scripts\start.ps1` start Wactorz from a checkout of the
repository: they check Python, help set up `.env`, start the MQTT broker with
Docker Compose, install Wactorz into the checkout's own virtual environment, and
launch it.

---

## Before anything: install these (once)

| What | How |
|---|---|
| **Python 3.11 or later** | `winget install Python.Python.3.13` |
| **Git** | `winget install Git.Git` |
| **Docker Desktop** | `winget install Docker.DockerDesktop`, then open it and wait until the engine is running |

---

## Get the code and start

```powershell
git clone https://github.com/waldiez/wactorz
cd wactorz
scripts\start.bat
```

Or double-click `scripts\start.bat` in Explorer. `start.bat` only runs the
`start.ps1` next to it, so keep the two together in the checkout.

The launcher:

- uses the checkout it is in, as it is (pass `-Update` to `git pull --ff-only` first)
- finds Python 3.11 or later, or reuses the checkout's `.venv`
- creates `.env` from `.env.template` if there is none, and leaves an existing one as it is
- asks for `MQTT_PASSWORD` if it is empty: press Enter to generate one, or `E` to set your own
- opens `.env` in Notepad if your model provider needs an API key and none is set
- starts the authenticated MQTT broker (`docker compose up -d --wait mosquitto`)
- installs Wactorz into `.venv` (again only when `pyproject.toml` changes)
- launches Wactorz and prints the dashboard address, `http://localhost:8888` by default

---

## What the launcher needs from you (first time only)

- **A broker password.** Press Enter when asked, and one is generated and saved in
  `.env` without being shown.
- **Your model provider's key**, unless the provider needs none. With the default
  `LLM_PROVIDER=anthropic`, set `ANTHROPIC_API_KEY` or `LLM_API_KEY`. OpenAI takes
  `OPENAI_API_KEY`, Gemini `GEMINI_API_KEY`, NIM `NIM_API_KEY`; any of them takes
  `LLM_API_KEY`. `LLM_PROVIDER=ollama` runs a local model and needs no key.

---

## Options

```powershell
scripts\start.bat -Update      # git pull --ff-only, then start
scripts\start.bat -NoPrompt    # never ask or open Notepad; stop with an error instead
```

Set `WACTORZ_PYTHON` to a `python.exe` to choose the interpreter the environment is
made with. Any other arguments are passed on to `wactorz`.

---

## Updating Wactorz

```powershell
scripts\start.bat -Update
```

---

## Something is wrong?

**"Wactorz needs Python 3.11 or later"**: install a newer Python
(`winget install Python.Python.3.13`) and run the launcher again.

**".venv uses Python 3.10"**: the environment was made with an old Python. The
launcher leaves it alone; delete `.venv` and run the launcher again.

**"Docker is installed but its engine is not running"**: open Docker Desktop and
wait until the engine is running.

**"MQTT_PASSWORD is empty"**: set it in `.env`, or run the launcher without
`-NoPrompt` and let it generate one.

**"running scripts is disabled"**: use `start.bat`, which runs `start.ps1` with the
execution policy set for that one run.

**Agents can't talk to each other**: check the broker with `docker compose ps`.

**The window closes immediately**: run `scripts\start.bat` from a terminal so you
can read the error.
