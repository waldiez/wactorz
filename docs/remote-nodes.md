# Remote Nodes

Deploy a single file to any machine — Raspberry Pi, VM, edge device — and spawn agents on it from the main Wactorz chat. Remote agents heartbeat back to the central dashboard exactly like local ones.

## Overview

The remote node system is built around a single self-contained script: `remote_runner.py`. It requires no Wactorz installation on the edge device — only Python 3 and three pip packages. It connects to the shared MQTT broker, listens for spawn commands from the main machine, and runs DynamicAgents locally with the same supervisor semantics.

```
[Main machine]                        [Edge device — Raspberry Pi, VM, etc.]

MainActor  ──MQTT──►  nodes/{name}/spawn  ──►  remote_runner.py
                                                   │  compiles + runs agent
                                                   │  local ONE_FOR_ONE supervisor
Dashboard  ◄──MQTT──  agents/{id}/heartbeat  ◄──┘  heartbeats every 10 s
```

Remote agents appear in the central dashboard alongside local agents. The only visual difference is a `node` field in their heartbeat payload showing which machine they run on.

---

## Setup on the edge device

#### 1. Install dependencies (minimal)

```bash
pip install aiomqtt psutil aiohttp --break-system-packages
```

#### 2. Copy `remote_runner.py` to the device

```bash
scp wactorz/remote_runner.py pi@raspberrypi.local:~/
```

#### 3. Start the runner

```bash
python3 remote_runner.py --broker 192.168.1.10 --name rpi-livingroom
```

Replace `192.168.1.10` with the IP of the machine running the MQTT broker. The `--name` is the node identifier — it must be unique across all nodes and is used to address this device when spawning agents.

#### Command-line options

| Flag | Default | Description |
|------|---------|-------------|
| `--broker` | `localhost` | MQTT broker hostname or IP. Also reads `$WACTORZ_BROKER`. |
| `--port` | `1883` | MQTT broker port. |
| `--name` / `--node` | random | Unique node name. Also reads `$WACTORZ_NODE`. |
| `--loglevel` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |

#### Run as a service (systemd)

**A deploy from the dashboard installs this for you.** It picks the least
privilege the node supports — root, then a user unit with lingering enabled,
then passwordless sudo — and falls back to `nohup` only when the node offers
none of them. The deploy result says which it got, so a node reported as
`nohup — unsupervised` is one that will not come back after a reboot.

The unit it writes, for a node deployed as user `pi`:

```ini
[Unit]
Description=Wactorz remote node
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/wactorz
EnvironmentFile=/home/pi/wactorz/.env
ExecStart=/home/pi/wactorz/venv/bin/python /home/pi/wactorz/remote_runner.py --broker ${WACTORZ_BROKER} --port ${WACTORZ_PORT} --name ${WACTORZ_NODE}
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
```

A user unit is the same minus `User=`, with `WantedBy=default.target`, and lives
at `~/.config/systemd/user/wactorz-node.service`.

Three details worth knowing if you write one by hand:

- **`Restart=on-failure`, not `always`.** `/nodes shutdown` exits cleanly on
  purpose. Under `Restart=always` that command restarts the node instead of
  stopping it.
- **`RestartPreventExitStatus=2`.** Exit 2 means a node name that can never
  work — it contains an MQTT wildcard — or a malformed `ExecStart`. Neither
  succeeds on retry, so restarting is just noise.
- **Every argument comes from `~/wactorz/.env`**, which the deploy writes at
  mode 0600 with `WACTORZ_NODE`, `WACTORZ_BROKER`, `WACTORZ_PORT`, when the
  broker needs them `MQTT_USERNAME`/`MQTT_PASSWORD`, and the `MQTT_TLS` settings
  when the node reaches the broker over TLS. Values are quoted so the
  same file is safe both sourced by a shell and read by systemd — but note that
  a `VAR=$(cmd)` you add by hand is executed by the shell and taken literally by
  systemd, so avoid them.

#### Logs

Under a unit, the runner logs to the journal rather than to
`~/wactorz/<node>.log`:

```bash
journalctl -u wactorz-node -f              # system unit
journalctl --user-unit=wactorz-node -f     # user unit
```

Note the second form: `--user-unit=` queries the system journal for a *user*
unit. `journalctl --user -u wactorz-node` looks like the same thing and is not —
`--user` reads the invoking user's own journal, which on a default Debian or
Raspberry Pi OS install does not exist (no per-user journal files are split out,
and the account is not in the `systemd-journal` group), so it reports
"No journal files were found" while the logs sit in the system journal.

The `~/wactorz/<node>.log` file is only written on the `nohup` fallback.

> **💡 Self-test** — Run `python3 remote_runner.py --test` to execute the built-in supervisor test suite without needing a broker. Useful to verify the script works on a new device before connecting it.

---

## Spawning agents on a remote node

From the main Wactorz chat, add a `"node"` field to any spawn request. The planner and main_actor both support this — or you can do it manually.

#### Natural language (via planner)

```
"deploy a temperature sensor agent to rpi-livingroom"
"spawn an agent on rpi-bedroom that reads the door sensor every 30 seconds"
```

#### Manual spawn via chat

```json
{
  "name":          "temp-sensor-agent",
  "node":          "rpi-livingroom",
  "type":          "dynamic",
  "description":   "Reads temperature from DHT22 sensor",
  "poll_interval": 30,
  "max_restarts":  5,
  "restart_delay": 3.0,
  "install":       ["adafruit-circuitpython-dht"],
  "code": "
    async def setup(agent):
        await agent.log('DHT22 sensor agent ready')

    async def process(agent):
        import random   # replace with real adafruit_dht read
        temp = round(20 + random.uniform(-2, 2), 1)
        await agent.publish('sensors/temperature', {'value': temp, 'unit': 'C', 'node': agent.node})
        await agent.log(f'Temperature: {temp}C')
  "
}
```

The main machine publishes this config to `nodes/rpi-livingroom/spawn`. The runner picks it up, installs any declared `"install"` packages, compiles the code, and starts the agent under a local supervisor.

> **ℹ replace flag** — If an agent with the same name is already running on the node, the spawn is ignored by default. Pass `"replace": true` in the config to stop the old instance and spawn fresh.

---

## Automated deploy from chat

MainActor can deploy `remote_runner.py` to a new machine over SSH, but only to a machine you have configured as a **deploy target**. Add the node to your environment first:

```bash
DEPLOY_TARGETS=rpi-bedroom
DEPLOY_RPI_BEDROOM_HOST=192.168.1.52
DEPLOY_RPI_BEDROOM_USER=pi
DEPLOY_RPI_BEDROOM_KEY=/path/to/id_ed25519    # preferred; or _PASSWORD
DEPLOY_RPI_BEDROOM_BROKER=192.168.1.10        # broker as seen FROM the Pi
```

The block is keyed by the node name upper-cased, with every run of non-alphanumerics collapsed to one underscore — `rpi-bedroom` → `DEPLOY_RPI_BEDROOM_*`. Restart Wactorz, then from the chat:

```
/deploy rpi-bedroom
```

The installer agent SSHes in, creates `~/wactorz/`, uploads `remote_runner.py`, installs the dependencies into a venv, and starts the runner in the background. After that, the node is available for agent spawning.

> **⚠ Credentials never go through chat.** `/deploy` takes a node name and nothing else. The older `/deploy <node> <host> <user> <password>` form is refused: chat messages are written to the conversation history and the chat log, so a password typed there stays on disk long after the deploy. For the same reason, don't ask an agent to SSH somewhere with a password in the request — the installer reads credentials from the environment and ignores any supplied in a task payload.

Leaving `DEPLOY_<NODE>_HOST` unset makes the deploy resolve `<node>.local` over mDNS instead. That is a single name lookup; earlier versions fell back to scanning the local `/24` for open SSH ports, which is gone.

### The broker has to be reachable from the node

A remote node connects back to the MQTT broker over the network, so `broker` in
its target block is the address the **node** should dial — your main machine's
LAN IP, not `localhost`.

The Home Assistant add-on's embedded broker publishes no port by default, so
nothing outside the add-on can reach it: assign `1883` a host port under the
add-on's **Network** settings, or `8883` for TLS, before a remote node can
connect to it at all — whatever credentials that node holds.

Broker credentials travel with the deploy. They are written to `~/wactorz/.env`
on the node (mode `0600`) and sourced when the runner starts, so they appear in
no command line — SSH runs the launch command through a shell whose own
arguments any local user can read with `ps`, which is why they are not passed
that way. A node uses `DEPLOY_<NODE>_BROKER_USER` / `_BROKER_PASSWORD` if set,
and this server's `MQTT_USERNAME` / `MQTT_PASSWORD` otherwise.

Sharing the server's account has a cost worth stating: **a stolen node holds full
broker access**, and the broker carries the code spawned agents run. An account
per node is the answer, and Wactorz can issue them.

### An account per node

`WACTORZ_NODE_ACCOUNTS=1` gives every deployed node an account named after it.
The password is derived from the same secret the signing keys come from, so
nothing new is stored and a leaked password file says nothing about a signing
key, and `/deploy` writes it into the node's `~/wactorz/.env` as before. A
`DEPLOY_<NODE>_BROKER_USER` / `_BROKER_PASSWORD` you set still wins.

For the broker Wactorz configures — compose's, and the add-on's embedded one —
it also writes an access list into `MQTT_BROKER_DIR`, beside the TLS
certificate. With it, a node may:

- publish and read its own `nodes/<name>/...`, and the shared agent traffic
  (`agents/#`, `sensors/#`, `homeassistant/#`, whatever your agents use);

and may not:

- touch another node's `nodes/<other>/...`, in either direction — so it can
  neither drive, impersonate nor watch another node;
- write `agents/+/commands`, which stops agents on the server;
- write anything under `system/`.

The compose broker picks up a new node's account on its own: it watches that
folder, reloads for a new account, and restarts itself if the access list or the
certificate appeared for the first time, since mosquitto reads those only at
startup.

**Only turn this on where the broker has those accounts.** On a broker you run
yourself, create them there first (or keep using `DEPLOY_<NODE>_BROKER_USER`).
A node presenting an account its broker has never heard of is simply refused.

**The rollout is per node.** Every node keeps the shared account until its next
`/deploy`, and nothing changes for it until then — so an install with the option
on and nodes not yet redeployed is exactly as contained as it was. Once the last
node has been redeployed, change `MQTT_PASSWORD` (and the broker's own account)
so the account they used to share no longer opens anything.

**What it does not do:** a node's own agents share the node's account, so the
containment boundary is the machine, not the agent. Nothing stops an agent
publishing readings another agent reads — that traffic is a commons by design.
The access list names the nodes you have configured, so it covers every node that
exists; a node can still write under a `nodes/<name>/...` that no node has yet, and
leave a message waiting for one deployed later. Mosquitto cannot express "every
node's topics except your own" — a `deny` beats every allow, including the node's
own — so naming them is the only shape available. Two things close the rest:
`/deploy` clears whatever is retained on that node's control topics before the node
starts and subscribes, and `WACTORZ_NODE_SIGNING=enforce` makes a node refuse
anything main did not sign for it.

### Signed commands

Everything main tells a node to do — spawn an agent, apply a desired state, stop,
restart, migrate — is signed with a key derived for that node. `/deploy` writes the
key to the node's `~/wactorz/.env` with its broker credentials, so it travels over
SSH and never over the broker. The signature rides in the message's MQTT v5 user
properties, so the payload is unchanged.

What a node does with a command that is not signed for it is set by
`WACTORZ_NODE_SIGNING` on the server, and written to the node when it is deployed:

- `warn` (the default) acts on it and counts it. The node reports the count in its
  heartbeat, and main says in chat when it goes up.
- `enforce` refuses it.

A node deployed before signing holds no key and acts on everything, as it always
did; deploy it again to give it one. A node remembers the commands it has
accepted, so a captured one cannot be replayed, and refuses one published before
its latest deploy. Once a node reports that it checks, main republishes its desired
state signed, replacing one retained from before that the node would otherwise
refuse on every reboot.

The keys are derived from a secret in `<WACTORZ_STATE_DIR>/node_signing.key`. Back
it up with the rest of the state directory. To rotate the keys, delete it and deploy
every node again: until a node is redeployed, it reports or refuses what the new
secret signs.

### Encrypted connections (TLS)

A node's broker connection carries its account's password and the code of every
agent spawned on it, so it should be encrypted on any network you do not fully
trust. Wactorz sets that up with no certificate to buy or renew:

- **The broker gets a certificate.** Wactorz keeps a private certificate authority
  (CA) in `<WACTORZ_STATE_DIR>/mqtt_tls/`, created the first time it is needed, and
  issues the broker's certificate from it. The compose broker serves TLS on port
  `8883` beside plain MQTT on `1883` once the certificate is in
  `infra/mosquitto/generated/`: the `python` and `full` profiles put it there, and so does
  a `wactorz` run on the host with `MQTT_TLS=1`, or `make mqtt-certs` — restart the
  broker after the first time. The Home Assistant add-on's embedded broker serves
  TLS too. The certificate is issued again before it expires, or when it no longer
  names the broker's addresses; the CA stays, so nodes keep trusting it.
- **`/deploy` checks before it switches.** It copies the CA to the node and tries a
  TLS connection to the broker on `8883` from the node itself. If that works, the
  node uses TLS from then on; if not, it stays on plain MQTT, and the deploy log says
  why. A node deployed before this keeps plain MQTT until it is deployed again.

`DEPLOY_<NODE>_BROKER_TLS=on` uses TLS even when the check fails — the broker may
not be up yet — `off` never does, and `DEPLOY_<NODE>_BROKER_TLS_PORT` changes the
port. The node has to be able to reach that port.

A client trusting the generated CA checks that the broker's certificate came from
it, but not the host name inside it: that CA signs nothing but this broker, and a
node should not lose its connection because the broker's LAN address changed.

**A certificate of your own.** Set `MQTT_TLS_CA` on the server to the CA that signed
your broker's certificate, or to `system` for one from a public CA such as Let's
Encrypt. `/deploy` hands that to the node instead, and the host name is then
checked, so give each target a `broker` name the certificate carries.
`MQTT_TLS_CHECK_HOSTNAME=1` or `0` decides the host name check either way.

**The server's own connection** uses TLS with `MQTT_TLS=1`: the server then dials
`MQTT_TLS_PORT` (default `8883`) instead of `MQTT_PORT`, under compose too. It creates
the generated CA and certificate when it starts if they are missing, and writes the
broker's copy to `MQTT_BROKER_DIR` (`infra/mosquitto/generated` in `.env.template`). A CA
that cannot be loaded stops it at startup, naming the file it looked for. Without
`MQTT_TLS` the connection stays plain, which suits a broker beside the server rather
than across a network.

**A node started by hand** reads the same settings from its environment:
`MQTT_TLS=1`, `MQTT_TLS_CA` naming a copy of `<WACTORZ_STATE_DIR>/mqtt_tls/ca.crt`,
and `MQTT_TLS_CHECK_HOSTNAME=0`, with `--port 8883`. A runner told to use TLS that
cannot load its CA refuses to start rather than connecting unverified.

**A broker you run yourself** can serve the generated certificate:
`python -m wactorz.broker_certificates --export <dir>` writes `broker.crt`, with the
CA after it, and `broker.key` there, for its `certfile` and `keyfile`; `--name` adds
an address it is reached by.

Back up `mqtt_tls/` with the rest of the state directory. Should `ca.key` be lost,
delete `ca.crt` and `ca.key` and deploy every node again: a new CA is trusted by no
node holding the old one.

### Host key verification

SSH host keys are checked on every connection. A machine that has not been connected to before has its key recorded on first contact — the same trust-on-first-use that interactive `ssh` does — and any later change to that key fails the connection instead of being accepted.

Learned keys live in `<WACTORZ_STATE_DIR>/known_hosts`, or wherever `DEPLOY_KNOWN_HOSTS` points. Set `DEPLOY_STRICT_HOST_KEYS=1` to turn off first-use learning entirely; every target's key must then already be in the file:

```bash
ssh-keyscan 192.168.1.52 >> "$DEPLOY_KNOWN_HOSTS"   # after verifying the fingerprint
```

---

## MQTT topics

The runner subscribes to a set of control topics scoped to its node name, and publishes heartbeats for itself and all its agents.

| Topic | Direction | Description |
|-------|-----------|-------------|
| `nodes/{name}/spawn` | → runner | Spawn a new agent. Payload: full agent config dict. Not retained — a node that was away catches up from `desired_state`. Signed. |
| `nodes/{name}/desired_state` | → runner | Every agent the node should be running. Retained; the runner starts any that are missing when it connects. Signed. |
| `nodes/{name}/stop` | → runner | Stop a named agent. Payload: `{"name": "agent-name"}`. Signed. |
| `nodes/{name}/stop_all` | → runner | Stop all agents and shut down the runner. Signed. |
| `nodes/{name}/list` | → runner | Request the list of running agents. Response on `nodes/{name}/agents`. |
| `nodes/{name}/agents` | ← runner | Response to `list`. Contains agent names and actor IDs. |
| `nodes/{name}/heartbeat` | ← runner | Runner heartbeat every 10 s. Contains node name, Wactorz version, runtime kind, agent count, broker address, and whether the node checks signed commands. |
| `nodes/{name}/migrate` | → runner | Migrate a running agent to another node. Payload: `{"name": "...", "target_node": "..."}`. Signed. |
| `nodes/{name}/migrate_result` | ← runner | Result of a migration request. |
| `nodes/{name}/reply/{id}` | ← runner | Reply routing for `agent.send_to()` calls originating on this node. |
| `agents/{id}/heartbeat` | ← agent | Per-agent heartbeat every 10 s. Includes `"node": "{name}"` field. |
| `agents/{id}/logs` | ← agent | Log messages from `agent.log()` and `agent.alert()`. |
| `agents/by-name/{name}/task` | → agent | Task addressed to a named agent on any node. Runner routes to local agents by name. |

---

## Supervisor behaviour

Each agent on a remote node runs under a local **ONE_FOR_ONE** supervisor — identical semantics to the main machine. If an agent crashes, the supervisor restarts it with exponential back-off:

```python
delay = min(restart_delay * (2 ** (restart_count - 1)), 60.0)
# restart_delay=3.0: 3s → 6s → 12s → 24s → 48s → 60s (cap)
```

| Scenario | Behaviour |
|----------|-----------|
| Crash in `process()` | Back-off, restart. After 5 consecutive failures in one run, escalates to supervisor for a clean restart. |
| Crash in `setup()` | Fatal — supervisor stops. Broken code won't fix itself on retry. |
| Compile error | Fatal — supervisor stops immediately. |
| Restart budget exhausted (`max_restarts`) | Agent is marked `failed`, removed from the registry, and a fatal event is published. |
| 10 consecutive successful `process()` calls | One restart token is credited back (gradual budget recovery). |
| Deliberate `stop` command | No restart — clean shutdown. |

Default values: `max_restarts=5`, `restart_delay=3.0`. Override per agent in the spawn config.

---

## Agent API on the edge

The `agent` object available inside remote agent code mirrors the local DynamicAgent API. All of the following work identically:

| Method | Description |
|--------|-------------|
| `await agent.publish(topic, data)` | Publish to any MQTT topic via the shared broker. |
| `await agent.log(message)` | Log to `agents/{id}/logs` — visible in the central dashboard. |
| `await agent.alert(message, severity)` | Publish an alert. Levels: `info`, `warning`, `error`. |
| `agent.persist(key, value)` | Write to `/tmp/agentflow_{name}_state.json` (JSON, not pickle — portable). |
| `agent.recall(key)` | Read a persisted value. |
| `agent.state` | In-memory dict, not persisted. |
| `await agent.send_to(name, payload)` | Send a task to any agent (local or remote) via MQTT request/reply. Times out after 30 s by default. |
| `agent.node` | The node name this agent is running on (e.g. `"rpi-livingroom"`). |
| `agent.agents()` | List of all agents running on this node. |

> **⚠ No `agent.subscribe()` on edge** — The remote runner does not implement `agent.subscribe()`. For MQTT subscriptions in remote agents, open an `aiomqtt.Client` directly inside `setup()` — the broker address is available as the machine's IP passed to `--broker`.

---

## Agent migration

A running agent can be moved from one node to another without stopping it manually. The runner on the source node captures the agent's config, publishes it as a spawn command to the target node, then stops the local instance.

```bash
# From the main machine, publish to MQTT:
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/migrate" \
  -m '{"name": "temp-sensor-agent", "target_node": "rpi-bedroom"}'
```

Or trigger it from agent code using `agent.send_to()` if you build a migration manager. The result is published to `nodes/{source_node}/migrate_result`.

---

## Debugging

#### Verbose diagnostics

The runner has a built-in diagnostics logger that prints startup checks, connection events, and every published message to stderr — even if logging is misconfigured. Run with `--loglevel DEBUG` to see everything:

```bash
python3 remote_runner.py --broker 192.168.1.10 --name rpi-test --loglevel DEBUG
```

On startup it prints:

- Whether `aiomqtt`, `psutil`, `aiohttp` are installed
- Whether the broker is TCP-reachable (5 s timeout)
- Every subscribe, publish, and received message with counts

#### Self-test (no broker needed)

```bash
python3 remote_runner.py --test
```

Runs 7 supervisor tests: stable agent, crash + restart, budget exhaustion, deliberate stop, health credit, compile error, setup failure. All should pass.

#### Watch node traffic from the main machine

```bash
mosquitto_sub -h localhost -t 'nodes/#' -v
mosquitto_sub -h localhost -t 'agents/+/heartbeat' -v
```

#### List agents on a node

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/list" -m '{}'
mosquitto_sub -h localhost -t "nodes/rpi-livingroom/agents" -C 1
```

#### Stop a specific remote agent

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/stop" \
  -m '{"name": "temp-sensor-agent"}'
```

#### Stop the entire runner

```bash
mosquitto_pub -h localhost -t "nodes/rpi-livingroom/stop_all" -m '{}'
```
