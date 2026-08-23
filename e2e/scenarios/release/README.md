# Release candidates

The regression core answers "did we break what worked". This answers "does the
thing we just built work".

The habit: read `[Unreleased]` in the changelog before tagging, and every
user-facing entry gets a scenario here — or already has one, if it was written
with the feature, which is the better time. One `make e2e-release` run replaces
the manual pre-tag walkthrough.

A revolving door, not a museum. When a scenario proves stable and the feature
becomes core, promote it into `scenarios/`; when a release's checks are
superseded, delete them.

Scenarios needing a live Home Assistant belong here too, and skip — loudly,
never red — when `HA_URL`/`HA_TOKEN` are unset. Scenarios needing real hardware
are marked `@pytest.mark.requires_node` and skip unless a deploy target is named
with `--real-node` or `$E2E_REAL_NODE`.

`make e2e` deliberately skips this directory: a feature in flight must not make
an ordinary run red.
