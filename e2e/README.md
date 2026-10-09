# End-to-end journeys

What a person does with Wactorz, done in order against a real install, and read
strictly: a real broker, the application as a process, a node deployed to another
machine over SSH, and a browser.

```bash
make e2e-setup   # once: Playwright and the browser it drives
make e2e         # needs Docker
```

It starts everything it uses, on ports and in a directory of its own, and reads
nothing of your `.env` or your state, so it runs beside an instance you already
have running.

## What it runs against

| Part | What it is |
| --- | --- |
| Broker | The development stack's mosquitto, taken as it is from `compose.dev.yaml` (`stack/compose.yaml`), with the certificate, node accounts and access list Wactorz generates for it. |
| Application | `python -m wactorz` from this checkout, with an API key, TLS to the broker, an account per node and one deploy target. |
| Node | A container with Python and an SSH server (`stack/node/`). The application deploys the node to it with `/deploy`, from the chat, as it would to a Raspberry Pi. |
| Model | The scripted provider (`LLM_PROVIDER=fake`). `model.py` holds every question a journey asks and its answer. |
| Browser | Chromium through Playwright, headless: one tab, signed in, kept open for the whole run. |

## The two rules

**A journey says what an agent answers, word for word.**
`dashboard.expect("main", "…")` waits for the next message in the thread on
screen, and fails as soon as it is from someone else or says something else,
quoting it. `expect_like` takes a pattern, for a message with a part that
differs between runs; it is not a way to stop reading.

**After every journey, nothing may have happened that nobody asked for.** The
guard (`harness/guard.py`) fails the journey on:

- an agent message, in any agent's thread, that no journey claimed;
- an agent answering with the scripted model's "no script" sentence;
- a line at ERROR or above, or a traceback, in the backend's console;
- an error on the page's console, or an exception nothing caught;
- an error toast.

A journey that provokes one of these on purpose says which, with
`unexpected.allow(pattern)`, beside the reason.

## The journeys

| | |
| --- | --- |
| 01 | A browser with no session is asked for the key; a wrong key and a request without one are refused; signed in, every view draws. |
| 02 | Main answers what it was asked, the answer is paid for, and the conversation survives a reload. |
| 03 | Asked for an agent, main starts one; it has a card and answers for itself. |
| 04 | The catalogue lists what it has and starts the recipe asked for. |
| 05 | `/deploy` from the chat puts a node on the machine: a wheel of this checkout, TLS, an account of its own. |
| 06 | An agent is started on the node, moved home and out again with its memory, and another asks the model through the server. |
| 07 | An agent is stopped and started from its card; deleting asks first, and cancel keeps it. |
| 08 | The broker goes away and comes back: the page says so, and so does an agent that cannot be reached. |
| 09 | The server restarts: its agents, the node's agents, the conversations and the open tab all carry on. |
| 10 | Agents are deleted one by one, then everything is wiped: what is left is what a fresh install runs, and the node, still deployed and running nothing. |

## Order

The journeys run in file order and each starts from what the last one left, as
an afternoon with the product does. The run stops at the first failure: after
it, the install is not in the state the next journey starts from.

## When one fails

The run's directory under `e2e/out/` is kept, and the path is printed:

- `logs/backend.log`, `logs/broker.log`, `logs/node.log`, `logs/node-machine.log`
- `traces/dashboard.zip`: `python -m playwright show-trace <file>` replays the
  page at every step.

A run that passes removes its directory. `make e2e-clean` removes what failed
runs kept.

## Adding a journey

1. Add what the model is asked, and its answer, to `model.py`.
2. Write `journeys/test_NN_<what>.py` using the `dashboard`, `app` and `run`
   fixtures. Act through the page; use `app.rest` to read what the page cannot
   show.
3. Claim every agent message the journey causes.
