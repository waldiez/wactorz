# Agent identity: names, ids and namespaces

A decision record, not a guide. It covers what tells one agent from another, how a
person or another agent addresses one, and how two installs share a broker without
seeing each other's control traffic. Nothing here is built yet. The "What follows"
section says which step builds each part.

## The problem

Two live agents cannot share a name. The same detector on two nodes, or a counter on
main and one on a Pi, is refused or one replaces the other. Several things hold that in
place, and they all have to change together:

- **The id is the name.** `derive_actor_id(name)` is a uuid5 of the name and nothing
  else, so that broker sessions and the registry key survive a restart. Two agents with
  one name get one id. They then write the same retained topics (`agents/<id>/status`,
  `…/manifest`, `…/questions`) and evict each other's records on the dashboard.
- **Every registry is keyed by name.** That covers main's spawn registry, the chat
  history (`chat_log.agent_name`), delegation and `@mentions`. The dashboard's agent
  store also treats a second agent of a name as a restart of the first.
- **Tasks are routed by name.** Every node subscribes to `agents/by-name/+/task`. If two
  agents of one name ran on two nodes, both would answer the same task.
- **Placement assumes one.** A spawn sent to a node while the name runs on main keeps the
  one on main unless `replace` says otherwise (`_kept_here`).

There is a second collision, a level up. Two installs pointed at one broker, such as a
dev instance beside the house's own, publish onto the same `agents/`, `nodes/`,
`system/` and `models/` topics. Client ids already carry `install_id()`, so they no
longer kick each other off the broker. But each install still reads the other's agents
as its own.

## The decision

### A name is unique per node; `name@node` is the full address

An agent's name is unique on the host it runs on. This includes main, which is
addressed as the node `main`, the way migrations already spell `@main`. The full
address of an agent is `name@node`: `counter@pi-kitchen`, `weather@main`. A bare name
is an address as long as exactly one live agent has that name. That is every agent on
every install today, so nothing anyone types now stops working.

A node may not be named `main`, and the node-name check (`broker_accounts.name_error`)
refuses it. `@` is not allowed in an agent name or a node name, so an address splits in
exactly one way.

### The id is minted once, at spawn, and kept

An agent's id is minted when it is first spawned (a uuid4). It is stored in its spawn
registry entry and sent in the spawn config as `actor_id`. Wherever the agent runs, it
takes the id it is given rather than deriving one. The id stays the same across
restarts, moves between hosts and a change of node. It is what the retained topics,
the chat history and the dashboard key on.

The id does not come from `name@node`. A move changes the node, and the agent's
history, retained records and open questions must follow it rather than start again
under a new id.

Main's own built-in agents, the singletons it starts itself, keep the derived id. Each
of them runs exactly once and never moves, so the name is a sufficient key for them.

### An ambiguous bare name is answered with a question, never guessed

When a bare name matches more than one live agent, whatever used it (a chat message,
an `@mention`, a delegation, a CLI command, a REST call) does not act. A person
receives a question listing the addresses as its options. A program receives a refusal
whose message names them. Main never picks the first agent, the newest or the nearest.
A task that reaches the wrong agent is worse than a task that is not sent.

### The task topic names the node

Tasks go to `agents/at/<node>/<name>/task`, with replies under
`agents/at/<node>/<name>/reply/<id>`. A node subscribes to `agents/at/<its node>/+/task`
and answers only its own agents. The sender resolves the address first: from main's
registry, or for an agent on a node, from the retained manifests it already reads.

`agents/by-name/<name>/task` remains for nodes too old to know the new topic. Main
sends there only when the name is unique and the target node's heartbeat reports a
version from before this change. A node on the new version subscribes to both topics.
A sender only ever uses one of them, so a node never receives the same task twice.

### Placement says whether it moves or adds

A spawn that names a node where the name does not run, while the name runs on another
host, must say what it means:

- `replace` moves it: the agent elsewhere is stopped and the new one keeps its id. This
  is what `replace` does today.
- `alongside` adds a second agent under a new id.
- Neither is answered with a question with both as options. Today the first agent
  silently stays where it is.

A spawn or move onto a node that already runs the name is refused, as a spawn of a
running name is refused today. That includes a migration that would land on such a
node. Renaming either agent on the way in is not offered, because a rename would break
every address that names it.

### Each install's control topics live under its namespace

An install may set a namespace, `WACTORZ_NAMESPACE`. It is a single topic level,
checked by the same rule as an agent name, and chosen by a person: `house`, `lab`,
`dev`. When it is set, Wactorz's own control topics move under it:
`<ns>/agents/…`, `<ns>/nodes/…`, `<ns>/main/…`, `<ns>/system/…`, `<ns>/models/…`. When
it is unset, the topics are the ones in use today.

Only the topics Wactorz owns move. The topics that describe the world, `sensors/`,
`home/`, `custom/`, `schedule/` and `homeassistant/`, stay shared and unprefixed. Two
installs in one house should read the same sensors. `$SYS` is the broker's and is never
prefixed.

What follows from that:

- **One place builds topics.** The namespace is applied by the topic helpers in
  `wactorz/core/topics.py`. Topics spelled out with f-strings in the modules move onto
  those helpers first, so that no topic misses the prefix.
- **The access list follows.** A node's rules gain the prefix: its own
  `<ns>/nodes/<name>/#`, plus `<ns>/agents/#` except `<ns>/agents/+/commands`. A node
  account belongs to one install and cannot read another install's agents. The fence on
  data topics (`_FENCED`) fences the prefixed forms as well as the bare ones.
- **A node is told its namespace when it is deployed**, in its environment, beside the
  broker address. A node whose namespace differs from main's sees nothing, so main
  checks the namespace in the node's first heartbeat and reports a mismatch.
- **Main notices a second install on its namespace.** `system/health` carries
  `install_id()`. A health message on main's own namespace from a different install is
  reported once on the feed, with what to set. That is the dev instance on the house
  broker, caught on the first day.
- **The dashboard and the REST API do not change.** They talk to their own server, which
  already knows its namespace.

A namespace taken from `install_id()` by default was considered and is listed below.
The id stays what it already is: the part of every client id that tells installs apart.

### An upgrade changes nothing that is configured

An install that upgrades keeps every topic, id, history row and node working, with no
step to run:

- **Ids.** A spawn registry entry without a stored id gets `derive_actor_id(name)`
  written into it the first time main reads it. That is the id the agent already has,
  so its retained topics, its broker session and the dashboard's record of it carry
  on. Only an agent spawned after the upgrade gets a minted id.
- **History.** Chat history moves from being keyed by name to being keyed by id. Each
  existing row's id is `derive_actor_id(agent_name)`, which can be filled in
  mechanically and needs no lookup.
- **Topics.** With no namespace set, every topic stays as it is. The `by-name` topic
  stays for older nodes.
- **Nodes.** A node on an older version ignores `actor_id` and derives the id from the
  name. For the only agent of a name, the two ids are the same. Main refuses to place a
  second agent of a name on a node older than this change, saying which version it
  needs, rather than let that node take the first agent's id.
- **Add-on.** The add-on sets no namespace unless its options name one, so it is a
  no-op for every existing configuration.

## What was decided against

- **Names unique across the install.** This is how things stand today, and it is what
  the problem section asks to lift. A fleet of the same agent on several boards is the
  ordinary case, not the odd one.
- **An id from `name@node`.** It would be deterministic without storing anything, but a
  move would change it, and the agent would lose its history, its retained records and
  its open questions every time it moved.
- **Picking one agent when a name is ambiguous.** Whether the choice is the newest, the
  one on main or the last one addressed, it is a silent guess, and nothing would show
  that it was wrong.
- **Renaming on conflict** (`counter-2`). It keeps both agents but breaks every address,
  automation and mention that named the agent being moved.
- **A namespace from `install_id()` by default.** It would separate installs with no
  setting, but an upgrade would move every topic of every install. That breaks Home
  Assistant automations and scripts that subscribe to `agents/+/status`, and every
  node until it is redeployed. The topics would also be unreadable on a broker
  explorer. One install per broker is the common case, and it should keep its topics
  as they are.
- **One broker per install, or broker virtual hosts.** Mosquitto has no virtual hosts,
  and the shared world topics are the reason for one broker. The access list already
  separates nodes, and it separates installs the same way.
- **Prefixing the world topics too.** Two installs in one house would then each need a
  bridge to read the same sensor.

## What follows

Each step is a branch of its own, in this order:

1. **This record.**
2. **The dashboard keyed by id (R1).** Ids are already distinct for every agent that
   can exist today, so this needs nothing from the backend. The store is keyed by id,
   a card belongs to an id, the node is shown on the card, and `name@node` is shown
   on the cards wherever a name is shared. A second agent of a name is no longer
   taken as a restart. It also takes the CardDashboard tests off private members,
   as the frontend review asks. A chat thread stays keyed by name here: what the
   page is sent names the agent but not its id, and the page does not work an id
   out from a name.
3. **The minted id.** `actor_id` in the spawn config and the registry, nodes honouring
   it, the migration of existing entries, and chat history keyed by id. The
   dashboard's threads move to the id with it, from the id the server then sends.
4. **Addresses.** The `name@node` resolver, the question on an ambiguous name,
   mentions, delegation, the CLI and the REST API, `agents/at/…` with `by-name` kept
   for older nodes, `alongside` on a spawn, and the refusals on placement and moves.
5. **Namespaces.** Topics built in one place, then the prefix, the access list, the
   node's deploy environment, the heartbeat check and the second-install notice.
