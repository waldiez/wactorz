# Demonstrations

A story a home-automation user recognises, told end to end and paced for a
camera. The same file runs as a test under the `test` profile, which is what
keeps it from going stale: when the product moves, the scenario goes red before
anyone re-records it.

Assertions hold under both providers. Anything that depends on what the model
*said* belongs behind `@pytest.mark.requires_llm("real")`, which `test` skips
rather than fails — and those can go stale silently, so keep them few and re-run
them before sharing what they produced.

A demo runs on a backend of its own rather than the shared one. `demo/` is
collected before the regression core, so a demo sharing that backend would hand
the core a system with the demo's agents already in it — and the story reads
better starting from nothing anyway.

Recordings land in `e2e/out/videos/`. Share the file, not the directory.

`make e2e-demo` runs this directory and nothing else. A profile says *how* to
run, not what, so left wide it spent a real completion on every scenario in the
suite and opened a browser for each one that takes a page — which made "the take
you keep" fourteen clips with windows appearing between them. Each story holds
one page across its own steps, so one story is one take, and two stories are two
files rather than one with a seam in it. `make e2e-demo-all` is still there for
running everything against a real model, which is a thing to decide rather than
a default.

## Two stories, and they want different instances

`test_nursery_fan.py` names no entity and asks for an automation in the words a
person would use. That is what makes it the story worth recording, and it is why
it only works on a house that already has what it asks for: on an instance with
no nursery and no fan, the model looks, finds neither, and says so — correct
behaviour that reads as a broken demo.

`test_invented_sensor.py` brings its own sensor. Home Assistant will hold a
state for an entity nothing owns, so the scenario creates one, drives it, and
deletes it again. It needs no device and no helper set up in advance, it touches
nothing of yours, and it asserts on values odd enough that a model repeating one
has read the instance rather than guessed. Run this one anywhere; run the other
one where you meant to.

**It never invents a switch, and that is deliberate.** An invented switch accepts
`switch.turn_on` with a `200` and does not move, because nothing is behind it to
do the moving — so a story asserting on one would pass while nothing happened.
Readings in, and the reaction observed through the product.

## A demo against a live Home Assistant switches real things on

The stories here are automations, and an automation that works is one that acts.
With `HA_URL` and `HA_TOKEN` pointed at a real Home Assistant, `make e2e-demo`
built the nursery automation, watched the temperature cross the threshold, and
called `switch.turn_on` on an actual socket — the demo working exactly as
intended, and worth knowing before it happens in your house at 2am.

What it touches depends on what is there. The planner writes the automation
against the entities the instance really has, so it picks the sensor and the
switch it judges relevant; here that was a smart plug, and elsewhere it is
whatever fits the story.

**Point the demo profile at a Home Assistant you do not mind it operating** — a
test instance, or one whose devices are harmless to toggle. Clearing `HA_URL`
and `HA_TOKEN` is the other option: the story still runs and the agents are
still created, and nothing reaches a device.

**The `test` profile is quiet for a thinner reason than it looks.** Every backend
this suite starts inherits the environment it was launched from, `.env`
included — so a `test` run is holding the same Home Assistant credentials a demo
run is. What keeps it from acting is the fake provider: it answers the request
as ordinary chat rather than as a pipeline, so no actuator is ever built. That is
a property of the script, not a wall. A scenario written to drive an actuator
under `test` would reach the same devices.
