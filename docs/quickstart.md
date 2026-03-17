# How to run Wactorz on Windows

Scripts for setting up and running Wactorz on Windows. Handles cloning or updating the repo, checking the API key, starting the MQTT broker, and launching the application.

---

## Before anything — install these three things (once, ever)

| What | Where |
|---|---|
| **Python** | https://www.python.org/downloads/ — on the first screen check **"Add Python to PATH"** |
| **Git** | https://git-scm.com/download/win |
| **Docker Desktop** | https://www.docker.com/products/docker-desktop/ — after install, open it and wait for the whale icon in the taskbar to stop moving |

---

## The easy way — download and run the script

Open **Windows Terminal** or the black window (`cmd`).

### Option A — you already have the repo

If you cloned Wactorz already, just go into that folder and run:

```
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

Or double-click `scripts\start.bat`.

---

### Option B — you don't have anything yet

Paste this into the terminal and press Enter. It downloads and runs the script in one shot:

```
powershell -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/waldiez/wactorz/main/scripts/start.ps1' -OutFile '%TEMP%\wactorz-start.ps1'; & '%TEMP%\wactorz-start.ps1'"
```

---

The script will automatically:

- Detect if you are already inside an wactorz folder — and use it
- Otherwise look for `C:\waldiez\wactorz` — and use it
- Otherwise clone the repo fresh into `C:\waldiez\wactorz`
- Open your `.env` file in Notepad if your API key is missing
- Start the mosquitto MQTT broker in Docker
- Install the Python package
- Launch Wactorz

When it's running, open your browser at **http://localhost:8080**

---

## What the script needs from you (first time only)

Your **Anthropic API key**. When the script opens Notepad, find this line:

```
LLM_API_KEY=
```

Paste your key after the `=`, save, close Notepad, then press Enter in the terminal to continue.

> Need screenshots? Ask us — we have step-by-step images for the API key setup.

---

## Updating Wactorz

Just run the script again. It does `git pull` and reinstalls automatically.

---

## Something is wrong?

**"running scripts is disabled"** — use the `start.bat` file instead, or run:
```
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

**"pip is not recognized"** — Python is not installed, or "Add Python to PATH" was not checked. Reinstall Python and check that box.

**"git is not recognized"** — install Git from https://git-scm.com/download/win

**"docker is not recognized"** — Docker Desktop is not running. Open it from the Start menu and wait for the whale.

**Agents can't talk to each other** — mosquitto is not running. Run `docker start mosquitto`.

**Window closes immediately after launch** — something crashed. Run `wactorz` from the terminal directly so you can read the error.
