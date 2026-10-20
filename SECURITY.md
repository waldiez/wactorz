# Security Policy

## Reporting a vulnerability

Please report security issues by email to `development @ waldiez.io`. Do not open a public issue for a suspected vulnerability.

Include the version, how the deployment is exposed (loopback, LAN, Home Assistant add-on), and the steps needed to reproduce. We will acknowledge the report, agree a disclosure timeline with you, and credit you in the release notes unless you would rather we did not.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.7.x   | Yes       |
| < 0.7   | No        |

Security fixes are released on the current minor version. Earlier versions receive no backports. The properties described below are those of 0.7.1.

## The surfaces

A deployment can listen on three: the dashboard and its WebSocket (`WS_PORT`, 8888), the chat REST interface (`PORT`, 8000), and, if configured, a WhatsApp webhook server. They are not equally hardened, and the sections below say which is which.

## What the software protects

- **It listens on loopback unless told otherwise.** The default bind is `127.0.0.1`. Reaching it from another machine requires a deliberate change.
- **It refuses to start reachable and unauthenticated.** A process bound to a network-reachable address with no `API_KEY` exits with an explanation rather than starting. `WACTORZ_EXPOSED_OK=1` waives the check for a deployment whose exposure is already authenticated, such as the add-on behind ingress or a proxy that authenticates for it.
- **`API_KEY` covers every route on the dashboard and the REST interface** except the health probe and the sign-in flow.
- **Cross-origin state changes are rejected on the dashboard.** Its state-changing routes are `POST` or `DELETE`, and `Origin` is validated on those and on the WebSocket upgrade. A page in a browser cannot suppress that header, so it cannot drive the dashboard from another site. This does not extend to the REST interface — see below.
- **The Home Assistant add-on trusts ingress and nothing else.** A request must both carry the Supervisor's ingress marker and arrive from its address range; either alone is not enough, and the bypass does not exist unless the add-on enables it.
- **The add-on does not run as root.** Its start script drops to an unprivileged user before Wactorz starts, with no way back, and Home Assistant's configuration folder is mapped read-only. What agent code can change is the add-on's own data.
- **A node cannot hand the server code to load.** An agent moved back from a node brings its data, arrays and plain bytes, each checked against the hash its hand-back names, but never a value that runs code when it loads, such as a torch module or a joblib-saved model.
- **Failed sign-in attempts are throttled.**
- **Secrets stay server-side.** The Home Assistant token, LLM and broker credentials are not sent to the browser.

## Since 0.7.1, on `dev`

These are on the development branch and in no release yet. With the next one they join the list above.

- **The assistant cannot run a command on a node.** Main's model reaches a node only by named operations that read — uptime, disk, memory, temperature, the OS, network interfaces, Python, installed packages and the node's log — each a fixed command. A command of your own is typed as `/nodes run <node> <command>`, which the model cannot send, and only main may send the installer to a node at all.
- **A package not on the list is installed only when you say yes.** An agent's packages are installed at once only if they are on the list: what the catalogue and the examples use, what the installer knows by import name, `WACTORZ_PIP_ALLOW`, and what you have allowed. For anything else the dashboard asks, and the agent is not started or sent unless you answer Install there or type `/packages allow`. The answer is taken from the dashboard through the server itself, never from the broker, where any agent could publish one. `WACTORZ_PIP_INDEX_URL` sends every install to one index.
- **A node cannot get its code run elsewhere through a migration.** An agent moved off a node is started from main's own copy of its program, on main or on the next node; a different program or bundle the node hands back is discarded, and you are told.
- **A repair is the model's code.** Every self-repair of an agent's program passes the safety check, including a catalogue agent's, which loses the catalogue's trust by it; it may not import a module the original program did not, beyond standard modules that only compute; and the error it is asked to fix reaches the model marked as data, not instructions.
- **The safety check reads names, not spellings.** Generated code is parsed, and each name is read through the code's own imports, so an alias, a `getattr` with a string, `exec` and the attributes that lead into the interpreter are refused like the names they stand for. It is still a check on the code a model usually writes, not containment: see below.
- **A node cannot make main forget another node's agent, or one on main.** An agent's withdrawal, which removes it from main's records, is weighed where the agent runs: refused while it runs on main, and for an agent on a node taken once that node's own heartbeat no longer lists it, or once main gives up on the node, which removes its agents anyway. A node main has not heard from since it started is the exception: a withdrawal for its agents is taken as it comes.
- **Only an interface main trusts may speak for you.** A task that main answers as your own message, slash commands included, is taken only from a catalogue agent running the catalogue's program, such as the Reachy robot's, so code the model wrote cannot type commands as you.
- **Code the model writes can run off main.** A node on main's own machine, `sandbox`, holds none of main's credentials: set up with `sudo wactorz-node setup-local` on Linux, as a sandboxed systemd unit under a user of its own; as a container in the compose stack, where it runs with the app; or in the Home Assistant add-ons with `sandbox_node: true`. Once one exists, an agent the model writes — from a chat turn or a plan — runs there unless it names main, and one that names main waits for your yes on the dashboard (`WACTORZ_MODEL_CODE_ON_MAIN`). The model's agents already on main move there the first time it reports, except one that would not run there, which the chat names. `WACTORZ_MODEL_CODE_NODE=main` keeps the code on main.
- **The document converter keeps to your document folders.** It reads and writes only inside the folders named in `WACTORZ_DOCUMENT_DIRS` (by default your Documents folder) and files attached in the chat, never the state directory, and runs no other program.

## What it does not protect against

These are properties of the design rather than open defects. Read them as the conditions the software expects, and treat anything outside them as the operator's responsibility.

- **Agent code on main runs with the privileges of the process.** An agent on main executes Python in the same process as the rest of the system, with the same environment and the same credentials: the catalogue's agents, which are the project's own code; library agents, which are code you installed; and, with no sandbox node, every agent the model writes. Spawning an agent there, or installing a recipe from anywhere you do not control, grants it everything the process can reach. A sandbox node (above) removes the model's agents from that; on Windows, macOS and the desktop app, which have none, they stay on main.
- **Agents act on content they read.** They ingest email, calendar entries and web pages, and they can write and run code. Text arriving from any of those is untrusted input to something that can act, and nothing in the system separates instructions from data.
- **Broker access is still a large grant.** Commands to edge nodes are signed, and a node refuses one that is not. On the brokers Wactorz configures, each node has an account of its own that is kept out of the other nodes' topics. But the server's own account can publish anywhere, and agents act on the messages they read, which are not signed. A node may publish under any agent's topics: a task sent to an agent by name, and an agent's status, logs and manifest, can come from any node, so an agent treats a task's payload as untrusted input. Run the broker on a network you trust, give it credentials shared with nothing else, and treat the server's broker account as equivalent to shell access on every node.
- **An operator's answer is not authenticated.** The pick an agent receives from `ask_operator` or `notify_user` arrives on its `agents/<id>/answer` topic, where any node, and anything else holding broker access, can publish one. It says what someone answered, never that you did: do not make it the approval for anything you would not let the agent do unasked. Packages are the exception, and are approved through the dashboard itself, never the broker.
- **Main's deploy credentials open every node.** The SSH key or password a `DEPLOY_<NODE>_*` block names gives a shell on that node, as the user it names, so whoever holds main's environment holds every node, as with its broker account. A node deployed as root, or as a user with passwordless sudo, hands over root with it. Deploy nodes as a user without sudo, which the deploy supports through a user unit (see "Run as a service" in `docs/remote-nodes.md`).
- **The dashboard and the REST API are not encrypted.** Their HTTP and WebSocket channels do not use TLS. On anything other than a single-host loopback deployment, terminate TLS in front of them, and list the proxy in `WACTORZ_TRUSTED_PROXIES`. MQTT uses TLS for edge nodes when the broker serves it, as the compose broker and the add-ons' embedded broker do, and for the server's own connection with `MQTT_TLS=1`. Plain `1883` stays cleartext.
- **A loopback deployment trusts the local machine.** Any process on the same host reaches it without a token, and the state directory is deserialized on startup, so write access to it is code execution. Importing a state archive with `wactorz-state import` writes that directory, so an archive is code too: import only your own. An operator who wants more can set `API_KEY` on a loopback bind and restrict the state directory.
- **There is no general request rate limiting** beyond the sign-in throttle, and none on the API-key header path. Uploads are capped per file but not in total. A file is deleted with the last chat message that refers to it, or a day after upload if it was never sent.

## Deploying it safely

The default — bound to loopback, on a machine you control — needs no further configuration.

To reach it from elsewhere, choose one of:

- Put it behind a reverse proxy that terminates TLS and authenticates, and leave the application bound to loopback.
- Set `API_KEY`, bind to the interface you need, and restrict access to it at the network layer.
- Use the Home Assistant add-on, where ingress authenticates before the request arrives.

Give the process its own user account and its own credentials for anything it connects to, so that the blast radius of agent code is bounded by what that account can reach.
