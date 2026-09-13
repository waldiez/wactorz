# Security Policy

## Reporting a vulnerability

Please report security issues by email to `development @ waldiez.io`. Do not open a public issue for a suspected vulnerability.

Include the version, how the deployment is exposed (loopback, LAN, Home Assistant add-on), and the steps needed to reproduce. We will acknowledge the report, agree a disclosure timeline with you, and credit you in the release notes unless you would rather we did not.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.6.x   | Yes       |
| < 0.6   | No        |

Security fixes are released on the current minor version. Earlier versions receive no backports. The properties described below apply from 0.6.0 onwards.

## The surfaces

A deployment can listen on three: the dashboard and its WebSocket (`WS_PORT`, 8888), the chat REST interface (`PORT`, 8000), and, if configured, a WhatsApp webhook server. They are not equally hardened, and the sections below say which is which.

## What the software protects

- **It listens on loopback unless told otherwise.** The default bind is `127.0.0.1`. Reaching it from another machine requires a deliberate change.
- **It refuses to start reachable and unauthenticated.** A process bound to a network-reachable address with no `API_KEY` exits with an explanation rather than starting. `WACTORZ_EXPOSED_OK=1` waives the check for a deployment whose exposure is already authenticated, such as the add-on behind ingress or a proxy that authenticates for it.
- **`API_KEY` covers every route on the dashboard and the REST interface** except the health probe and the sign-in flow.
- **Cross-origin state changes are rejected on the dashboard.** Its state-changing routes are `POST` or `DELETE`, and `Origin` is validated on those and on the WebSocket upgrade. A page in a browser cannot suppress that header, so it cannot drive the dashboard from another site. This does not extend to the REST interface — see below.
- **The Home Assistant add-on trusts ingress and nothing else.** A request must both carry the Supervisor's ingress marker and arrive from its address range; either alone is not enough, and the bypass does not exist unless the add-on enables it.
- **Failed sign-in attempts are throttled.**
- **Secrets stay server-side.** The Home Assistant token, LLM and broker credentials are not sent to the browser.

## What it does not protect against

These are properties of the design rather than open defects. Read them as the conditions the software expects, and treat anything outside them as the operator's responsibility.

- **Agent code runs with the privileges of the process.** Agents generate and execute Python in the same process as the rest of the system, with the same environment and the same credentials. There is no sandbox and no privilege separation. Spawning an agent — or installing a recipe from anywhere you do not control — grants it everything the process can reach.
- **Agents act on content they read.** They ingest email, calendar entries and web pages, and they can write and run code. Text arriving from any of those is untrusted input to something that can act, and nothing in the system separates instructions from data.
- **Broker access is code execution.** MQTT can require credentials, but there is no per-topic authorization: a client the broker admits can publish to the topics that carry agent code to remote nodes, and a node runs what it receives. Run the broker on a network you trust, give it credentials shared with nothing else, and treat write access to it as equivalent to shell access on every node.
- **The chat REST interface has no origin or host checking.** A page in a browser can make a cross-origin request to it. On a loopback deployment with no `API_KEY`, any page the operator visits can reach it. Set `API_KEY`, or do not run that interface.
- **Transport is not encrypted by default.** Neither MQTT nor the dashboard's HTTP and WebSocket channels use TLS. On anything other than a single-host loopback deployment, terminate TLS in front of them.
- **A loopback deployment trusts the local machine.** Any process on the same host reaches it without a token, and the state directory is deserialized on startup, so write access to it is code execution. An operator who wants more can set `API_KEY` on a loopback bind and restrict the state directory.
- **There is no general request rate limiting** beyond the sign-in throttle, and none on the API-key header path. Uploads are capped per file but not pruned.

## Deploying it safely

The default — bound to loopback, on a machine you control — needs no further configuration.

To reach it from elsewhere, choose one of:

- Put it behind a reverse proxy that terminates TLS and authenticates, and leave the application bound to loopback.
- Set `API_KEY`, bind to the interface you need, and restrict access to it at the network layer.
- Use the Home Assistant add-on, where ingress authenticates before the request arrives.

Give the process its own user account and its own credentials for anything it connects to, so that the blast radius of agent code is bounded by what that account can reach.
