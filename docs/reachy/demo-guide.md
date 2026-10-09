# Demonstration guide

For whoever is putting Reachy in front of an audience: a lab visit, a conference stand,
a trade fair, a customer meeting. It assumes Reachy already works on your computer
([Getting started](getting-started.md)).

The rule behind everything here: **rehearse the exact setup you will use, on the network
you will use, and keep a typed fallback for everything you plan to say.**

## The day before

- [ ] Robot charged. Charger and USB cable packed (the cable lets you run a Lite, or a
      Wireless robot over USB, if Wi-Fi fails).
- [ ] Laptop charged, power supply packed, sleep and automatic updates turned off.
- [ ] Wactorz updated and tested **on this laptop**; no `git pull` on the day.
- [ ] `.env` has the language-model key, `DEEPGRAM_API_KEY` (if using voice), and the
      robot address. Keys have enough credit; set `LLM_COST_LIMIT_USD` to a sensible cap.
- [ ] If you will use local speech recognition, the Whisper model is already downloaded
      (one successful `listen and ask Wactorz` with `REACHY_STT_BACKEND=faster-whisper`).
- [ ] `ffmpeg` installed (`health` says the voice boost is on).
- [ ] A travel router or phone hotspot that you control. Venue Wi-Fi often isolates
      devices, and you cannot fix it on the day.
- [ ] A rehearsal of the full script below, including one deliberate failure and recovery.
- [ ] The backup video recorded (see [Backup plan](#backup-plan)).

## Hardware and network setup

- Put the robot on a stable table, at roughly the audience's chest height, with clear
  space around the head. Nobody's face should be within reach of the head's movement.
- Put the laptop where the presenter can see the dashboard but the audience sees the
  robot.
- Join the robot and the laptop to **your** router or hotspot. Give the robot a fixed
  address if your router allows it, and put that address in `REACHY_ROBOT_HOST` with
  `REACHY_CONNECTION_MODE=network`.
- Internet access is required for: speech output (always), Deepgram voice input (if
  used), and the language model (unless you use Ollama).

## Audio setup

- Halls are loud. Use `presenter mode` and keep the robot's speaker facing the audience.
- Voice input in a noisy hall is the most fragile part of any demo. Ask the visitor to
  stand within a metre and speak towards the robot. Have the presenter repeat the
  question if needed.
- Keep idle motion off or on `antennas only` while a conversation is running; the
  microphone hears the motors. Interruption ("barge-in") stays off.
- If the room is very noisy, prefer **push-to-talk** (`listen and ask Wactorz`, typed by
  the presenter) over a continuous conversation: one bounded recording at a time is easier
  to control.

## Startup sequence (about 5 minutes)

1. Power on the robot; wait for it to join the network.
2. Start the broker: `docker compose -f compose.dev.yaml up -d mosquitto`.
3. Start Wactorz: `uv run --no-sync wactorz`. Open <http://localhost:8888>.
4. Reachy starts by itself if it was spawned before; otherwise
   `@catalog spawn reachy-mini`.
5. `@reachy-mini health` → "I'm connected to my body".
6. `@reachy-mini wake up` → it moves and chimes.
7. `@reachy-mini say Testing, one two three` → audible across the space you will use.
8. If using voice: `@reachy-mini listen and ask Wactorz`, ask "what time is it?".
9. Choose the idle mood: `antennas only` for a quiet table, `showtime` for a busy stand
   with no conversation running, `stop moving` for a talk.

## The three demonstrations

Each is described with what really exists today. "Readiness" uses the classification in
the internal [capability matrix](internal/capability_matrix.md).

### Demo A: conversational robot

*A visitor talks with Reachy and gets natural spoken answers.*

| | |
| --- | --- |
| **The audience sees** | A visitor asks a question; the robot answers aloud within a few seconds, with the exchange appearing on the dashboard screen. |
| **Hardware** | Reachy Mini, laptop, router/hotspot. Optional external screen for the dashboard. |
| **Software** | Wactorz with the `reachy` extra, a language model, Deepgram key (or local faster-whisper). |
| **Setup** | Startup sequence above, then `start conversation`. For a stand that runs unattended between visitors, `{"cmd": "conversation_start", "inactivity_timeout": 60}` so it stops listening when nobody is there. |
| **Expected behaviour** | Reachy listens after it finishes speaking; the visitor's words appear under Reachy; the reply is spoken in sentence-sized pieces. "Goodbye" ends it. |
| **Failure recovery** | Misheard: presenter types the question instead (`@reachy-mini ask Wactorz …`). No answer: check the internet connection, then `stop conversation` / `start conversation`. Robot frozen: `reconnect`. |
| **Duration** | 2–5 minutes per visitor. |
| **Readiness** | Implemented and used in public demonstrations; hardware path not re-verified for this release (see [test report](internal/test_report.md)). |
| **Missing engineering** | Hardware re-validation of the voice path after this release's changes; measured end-to-end latency; a visible "listening / thinking" indicator on the dashboard for the audience. |

### Demo B: context-aware interaction

*Reachy uses its senses: it looks, describes, and reacts to where it is.*

| | |
| --- | --- |
| **The audience sees** | "What do you see?": Reachy turns its head, then describes the people or objects in front of it. "Look around": it scans and describes the room. "What's behind you?": it turns round to look. |
| **Hardware** | As Demo A; good, even lighting in front of the robot. |
| **Software** | As Demo A, with a language model that accepts images (for example a current Claude model). |
| **Setup** | Place a few recognisable objects in front of the robot. Tell visitors their image is sent to the model provider. |
| **Expected behaviour** | A one-sentence description; `look closer` gives detail. |
| **Failure recovery** | A wrong or vague description: ask a specific question (`what colour is the cup?`). An error: the model may not accept images; switch model, or skip to Demo A. |
| **Duration** | 3 minutes. |
| **Readiness** | Camera description: implemented, verified only with Anthropic models' image format; untested on this release's hardware. **Turning towards the speaker by sound direction is not implemented**: the microphone array's direction is readable (`doa`) but nothing uses it. |
| **Missing engineering** | Sound-direction head turn (read `doa`, turn with `look_at`); presence detection to greet arriving visitors. Both are new features, not fixes. |

### Demo C: agent-powered automation

*Reachy is the friendly face of a Wactorz automation.*

| | |
| --- | --- |
| **The audience sees** | "Reachy, turn on the lamp": a real lamp on the stand turns on and Reachy confirms. Then "when the lamp turns off, go to sleep": switching the lamp off sends Reachy to sleep. |
| **Hardware** | As Demo A, plus a Home Assistant instance and one smart plug or bulb it controls. |
| **Software** | Wactorz with `HA_URL` and `HA_TOKEN`; Home Assistant reachable from the laptop on the demo network. |
| **Setup** | Test the lamp from the Wactorz chat first (`turn on the lamp`, without Reachy). Then through Reachy. |
| **Expected behaviour** | Reachy speaks a short confirmation ("Okay, the light is on"); the dashboard shows the result. The rule survives a restart until `stop reacting to the light`. |
| **Failure recovery** | The lamp does not respond: the problem is Home Assistant, not Reachy; show it from the HA app instead. Reachy reacts late: Home Assistant state changes reach Wactorz over its state bridge; check it is running in the dashboard. |
| **Duration** | 3–5 minutes. |
| **Readiness** | Home Assistant control through Reachy and robot reactions to Home Assistant events: implemented and unit-tested; the full chain has not been verified on hardware for this release. |
| **Missing engineering** | A self-contained demo kit (Home Assistant on the laptop or a Pi on the travel router) so the stand does not depend on a home network. |

## A short public script (about 4 minutes)

Typed lines are for the presenter's laptop; spoken lines are said to the robot.

1. *(Robot asleep.)* Presenter: "This is Reachy Mini, a small open-source robot by Pollen
   Robotics. We connect it to Wactorz, our platform for AI agents." Type
   `@reachy-mini wake up`.
2. Type `@reachy-mini say Hello everyone! I'm Reachy.`
3. Type `@reachy-mini start conversation`. Then to the robot: **"What can you do?"**
4. **"Can you nod?"** It nods. **"Do a little dance."**
5. **"What do you see?"** It describes the room or the audience.
6. *(If Home Assistant is set up)* **"Turn on the lamp."**
7. Invite a visitor to ask one question of their own.
8. **"Goodbye."** The conversation ends. Type `@reachy-mini go to sleep`.

Rehearsed fallbacks for each spoken line: type the same words as
`@reachy-mini <words>`. Every spoken command also works typed.

## Recovery procedures

| Symptom during the demo | Recover with | Time |
| --- | --- | --- |
| Robot stops responding to motion | `@reachy-mini reconnect` (or wait: motor-link drops recover by themselves) | 5–15 s |
| Robot says "not connected" | Check the router; `reconnect`; last resort restart Wactorz (`Ctrl+C`, start again) | 15 s–1 min |
| Voice misunderstood repeatedly | `stop conversation`; continue typed, or push-to-talk | immediate |
| No speech, robot otherwise fine | Internet is down. Switch to the hotspot; Reachy still moves meanwhile | 1 min |
| Robot talking over itself, or won't stop | `@reachy-mini stop talking` | immediate |
| Motion looks wrong or unsafe | Switch the robot off at its switch. `@reachy-mini go limp` also cuts motor power, but the head may then sink under its own weight; keep a hand near it | immediate |
| Language-model errors | Check the key's credit and the cost limit in the dashboard Settings | 1 min |

## Safe shutdown

1. `stop conversation` if one is running.
2. `go to sleep`.
3. `Ctrl+C` in the Wactorz terminal; `docker compose -f compose.dev.yaml down`.
4. Switch the robot off at its switch before packing it. Do not carry it by the head or
   antennas.

## Backup plan

Things fail at venues. In order of preference:

1. **Typed demo.** Everything works typed, without voice input: the presenter types and
   Reachy acts and speaks. Needs only the robot, the laptop and internet.
2. **Offline-ish demo.** With no internet: Reachy can still move and run gestures and
   idle motion from typed structured commands (`{"cmd": "gesture", "name": "dance"}`),
   but cannot speak (speech synthesis is online) or answer questions unless the language
   model is local (Ollama).
3. **Video.** Record a 2-minute video of Demo A and Demo B working on your own network
   before the event, and keep it on the laptop.
4. **No robot.** `uv run --no-sync python scripts/reachy_sim_check.py` runs the agent
   against a simulated robot and shows each step passing. Useful for a technical audience
   only.
