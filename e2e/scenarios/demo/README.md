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
