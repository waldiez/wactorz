# Security

Wactorz runs AI agents that hold your credentials, control your devices, and can
write and run Python. This page describes what it protects, what it does not,
and how to deploy it so the difference does not matter.

Read it before you make Wactorz reachable from anything but the machine it runs
on.

---

## The short version

| Deployment | What protects it |
| --- | --- |
| **Default install** | It listens on `127.0.0.1` only. Nothing off the machine can reach it. |
| **Reachable install** | `API_KEY`. Binding to a reachable address without one refuses to start. |
| **Home Assistant add-on** | Home Assistant's own login. The panel goes through ingress, and requests are verified as coming from the Supervisor. |

The refusal is deliberate rather than a warning: a warning scrolls past in a
container log while you believe you only changed an address. Three ways forward,
and the message names all three — set `API_KEY`, bind to `127.0.0.1`, or set
`WACTORZ_EXPOSED_OK=1` when the only route in is already authenticated.

---

## Trust boundaries

| Inside the boundary | Outside it |
| --- | --- |
| The Wactorz process and every agent in it | The browser |
| The MQTT broker and everything published on it | Any machine that can reach the broker port |
| Edge nodes reached by `/deploy` | The network between them |
| The state directory and the SQLite database | — |

Everything inside shares one privilege level. There is no separation between
agents: an agent is a Python object in a process, not a principal. If two agents
have different levels of trust in your head, they do not have different levels
of access in Wactorz.

---

## Agents run code in the Wactorz process

A spawned agent's body is Python, compiled and executed inside the running
process. It has the same file access, the same network access, the same
environment variables, and the same credentials as Wactorz itself — including
your LLM keys and your Home Assistant token.

Generated code is scanned for dangerous constructs before it runs, and obvious
ones are rejected. **That check is not a sandbox and is not intended as one.**
It reads source text; code that reaches the same capability by another route
passes it. Treat it as a guard against mistakes, not against intent.

The practical consequence:

> **Anyone who can make Wactorz spawn an agent can run arbitrary code as the
> Wactorz process.** Granting that ability is equivalent to granting shell
> access to the machine, with the credentials attached.

Spawning is reachable from the dashboard and the API, both of which sit behind
`API_KEY` on a reachable install. It is deliberately **not** reachable from
social channels — Discord and Telegram use a restricted entry point that refuses
spawning, deletion, code and automation, at the action rather than by inspecting
the text, so it cannot be talked around.

If you need agents that cannot do this, run Wactorz in a container with only the
credentials that deployment needs, and do not mount what it should not reach.

---

## Untrusted input is not just what a user types

The boundary that matters is not "someone typed into chat". It is **any text any
agent reads**, because that text reaches a model that can act.

Agents ingest, among other things:

- web pages and search results
- PDFs and device manuals downloaded from the internet
- email bodies and calendar entries
- MQTT payloads from devices and edge nodes
- Home Assistant entity names and attributes

A concrete example that ships in the catalogue: the device-manual agent searches
the web for a manual, downloads the PDF it finds, extracts the text, and puts
that text into a prompt. Nothing about that PDF is under your control. It
reaches that agent's own model first and the main agent's context only
indirectly, through the reply — two hops rather than a direct line to the
spawner, which is why it is worth stating plainly rather than assuming the
distance protects you.

Assume any document an agent reads can attempt to instruct it. Give agents the
narrowest credentials that let them do their job.

---

## The MQTT broker is the control plane

Agent commands, chat, task delegation and the source of spawned agents all cross
the broker. Anything that can publish to it can drive Wactorz.

- **Credentials are required.** The bundled broker refuses anonymous
  connections, and `docker compose` will not start without `MQTT_PASSWORD`.
- **There is no TLS in the client.** Traffic is cleartext, so the broker and
  everything talking to it belong on a network you trust. Do not route it across
  the public internet without a tunnel or VPN.
- **Edge nodes hold broker credentials.** `/deploy` writes them to the node over
  SSH, and by default a node uses the server's own account. A stolen node
  therefore holds full broker access; give a node its own account when that
  matters.

---

## Secrets, logs and stored data

- **Logs are redacted** as they are written — known credential shapes are
  scrubbed before anything is stored or served. This is a floor, not a
  guarantee: a log can carry a secret nobody chose to write into it. Treat the
  log view as shareable with care.
- **Raising a library to `DEBUG`** puts request bodies and headers into that same
  log. It is refused through the API unless `WACTORZ_LOG_DEBUG_CAPTURE=1` is set
  on the host, so turning it on requires reaching the machine.
- **Chat history, uploaded files and agent state** are stored unencrypted in the
  state directory. Protect it with filesystem permissions; anyone who can read
  it can read every conversation.
- **The browser is never given your API key.** Signing in exchanges it for a
  session cookie that can be revoked on its own, so signing out does not disturb
  scripts and integrations using the key directly.

---

## Deployment checklist

1. Keep the default `127.0.0.1` bind unless you need otherwise.
2. If you need otherwise, set `API_KEY` to something generated —
   `openssl rand -hex 32`.
3. Put the broker on a trusted network segment, and set `MQTT_PASSWORD`.
4. Give each edge node its own broker account if a stolen node would matter.
5. Give Wactorz only the credentials the agents you run actually need.
6. Restrict filesystem access to the state directory.
7. Treat the ability to spawn agents as equivalent to shell access, and hand it
   out on that basis.

---

## Reporting a problem

Please report security issues privately, by email to `development @ waldiez.io`,
rather than in a public issue. The maintainers will work with you on a fix
before any public disclosure.
