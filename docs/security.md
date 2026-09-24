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
- **Edge nodes reach the broker over TLS where the broker serves it.** Wactorz
  keeps a private CA in the state directory and issues the broker's certificate
  from it; the compose stack and the add-on's embedded broker serve TLS on `8883`
  beside plain `1883`. `/deploy` hands a node the CA and switches it to TLS only
  after checking from the node that the broker answers it — a node that could not
  stays on cleartext, and the deploy log says so. The server's own connection uses
  TLS with `MQTT_TLS=1`, on the broker's TLS port.
  Plain `1883` stays open for anything not yet on TLS, so the broker still belongs
  on a network you trust, and nothing here replaces a tunnel or VPN across the
  public internet. See "Encrypted connections (TLS)" in `remote-nodes.md`.
- **Edge nodes hold broker credentials.** `/deploy` writes them to the node over
  SSH, and by default a node uses the server's own account, so a stolen node
  holds full broker access. `WACTORZ_NODE_ACCOUNTS=1` gives each node an account
  of its own instead, and on the brokers Wactorz configures an access list that
  keeps a node to its own `nodes/<name>/...` and the shared agent traffic, out of
  every other node's, out of `agents/+/commands` and out of `system/`. It takes
  effect for a node at its next `/deploy`, so rotate the shared password once the
  last one has moved. A node's agents share its account: the boundary is the
  machine, not the agent.
- **Commands to an edge node are signed.** A node runs the code in a spawn it
  receives, so broker access alone must not be enough to send one. Main signs
  every command it sends a node with a key derived for that node, which `/deploy`
  writes to the node with its broker credentials. With `WACTORZ_NODE_SIGNING=enforce`
  a node refuses a command not signed for it; with the default, `warn`, it acts on
  it and main says so in chat, so you can see that nothing legitimate arrives
  unsigned before you enforce. A node deployed before signing holds no key and
  checks nothing until it is deployed again. Commands are what is signed: an
  agent's own messages, and what an agent reads from the broker, are not.

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
   Behind a reverse proxy, list it in `WACTORZ_TRUSTED_PROXIES` and have it set
   `X-Forwarded-Host` and `X-Forwarded-Proto` rather than append to them.
   Forwarded headers from any other peer are ignored. Do not list a loopback
   address: a page in a browser on the same machine connects from there too. For
   a proxy on the same host, have it pass `Host` through instead.
3. Put the broker on a trusted network segment, and set `MQTT_PASSWORD`.
4. Set `WACTORZ_NODE_ACCOUNTS=1` so each edge node gets its own broker account,
   deploy every node again, then rotate the account they shared.
5. Deploy every edge node again after upgrading, so it holds a signing key, and
   set `WACTORZ_NODE_SIGNING=enforce` once no node reports unsigned commands.
   Publish the broker's `8883` where nodes can reach it first, so the deploy puts
   them on TLS, and check the deploy log says so.
6. Give Wactorz only the credentials the agents you run actually need.
7. Restrict filesystem access to the state directory — it also holds the secret
   the node keys are derived from, and the key of the CA nodes trust the broker by.
8. Treat the ability to spawn agents as equivalent to shell access, and hand it
   out on that basis.

---

## Reporting a problem

Please report security issues privately, by email to `development @ waldiez.io`,
rather than in a public issue. The maintainers will work with you on a fix
before any public disclosure.
