# Testing the add-on locally (local add-on workflow)

Validate add-on changes — **especially state persistence across updates** — on a
real Home Assistant OS box **without** pushing to the store or triggering an
update for everyone running Wactorz.

> Why this exists: persistence bugs only show up on a real **update** (the
> container is recreated, and only `/data` survives). Testing with a plain
> `docker restart` keeps the whole container filesystem and hides the bug —
> which is exactly how a broken fix once slipped through. This workflow
> reproduces the real failure mode.

---

## Mental model: there are two sources

When Supervisor builds the add-on, the pieces come from **two different places**:

| Piece | Comes from | How you change it for a test |
|---|---|---|
| `config.yaml`, `build.yaml`, `run.sh` | the add-on **files** Supervisor reads from disk | edit the local copy on the host |
| the `wactorz` Python package | pip-installed inside the Dockerfile from `git+…@${WACTORZ_REF}` | set `WACTORZ_REF` in `build.yaml` to your branch (and drop `image:` to force a build) |

**Takeaway:** `run.sh` changes are picked up from your local copy with no git at
all. **Python** changes (`wactorz/…`) are only picked up if your branch is
**pushed** *and* `build.yaml`'s `WACTORZ_REF` points at it. Merging to `dev` does
**not** make the real add-on use new Python — production builds from `main`.

---

## 1. Copy the add-on into the local add-ons folder

Get a shell on the host (the **Advanced SSH & Web Terminal** add-on, or Samba),
then copy the **contents** of this repo's `ha-addon/` into a new folder under the
local add-ons directory (note the trailing `/.` — it copies contents, not the
folder):

```sh
mkdir -p /addons/wactorz-test
cp -r /path/to/repo/ha-addon/. /addons/wactorz-test/
```

> ⚠️ `config.yaml`, `Dockerfile`, and `run.sh` must sit at the **top level** of
> `/addons/wactorz-test/`. If you end up with `/addons/wactorz-test/ha-addon/…`,
> Supervisor won't detect it (no `config.yaml` at the top).

Give the test copy a **distinct name and slug** so it doesn't clash with the
store version, by editing `/addons/wactorz-test/config.yaml`:

```yaml
name: Wactorz (test)
slug: wactorz_test
```

## 2. Force a local build + point it at your branch (local-only)

The production add-on has an `image:` key, so Supervisor would **pull** the
pre-built image instead of building your local copy. For source testing, make two
host-only edits (**never commit them**):

1. **Remove the `image:` line** from `/addons/wactorz-test/config.yaml` so
   Supervisor builds from the local `Dockerfile` instead of pulling.
2. **Set the wactorz ref** in `/addons/wactorz-test/build.yaml`:

   ```diff
   -  WACTORZ_REF: main
   +  WACTORZ_REF: your-branch
   ```

> Your branch must be **pushed** to GitHub for the ref to resolve. No Dockerfile
> edit needed — the ref flows in as a build arg. (`run.sh` is already the copy
> you made.)
>
> Alternatively, skip the local build entirely: run the **Add-on Image** workflow
> (`workflow_dispatch`) with `ref = your-branch` and `version_tag = test`, then
> point the test copy's `image:` at `ghcr.io/waldiez/wactorz-addon-{arch}:test`.

## 3. Make Supervisor see it, then install

**Settings → Add-ons → Add-on Store → ⋮ → Check for updates.** The test copy
appears under **Local add-ons**.

![Local add-ons in the store](docs/local-testing/01-local-apps.webp)

Open it and **Install**, then **Start**.

![Install the test add-on](docs/local-testing/02-install.webp)

## 4. Generate some state

Open the Web UI and create state you can check for later: chat with an agent,
spawn one or two, let some cost accrue.

![Generate state in the Web UI](docs/local-testing/03-generate-state.webp)

## 5. Simulate an update

This is the whole point — an **update** recreates the container, so only `/data`
survives. A restart does **not** prove anything.

Bump `version` in `/addons/wactorz-test/config.yaml` (e.g. `0.4.3.2` → `0.4.3.3`):

![Bump the version](docs/local-testing/04-bump-version.webp)

Then **Add-on Store → ⋮ → Check for updates** so Supervisor notices the new version:

![Check for updates](docs/local-testing/05-check-updates.webp)

The add-on now shows **Update available** → click **Update**:

![Update available](docs/local-testing/06-update-available.webp)

Confirm the version jump and start the update:

![Update dialog](docs/local-testing/07-update-dialog.webp)

Wait for it to finish (Installed = Latest, "Up-to-date"):

![Update complete](docs/local-testing/08-update-complete.webp)

## 6. Verify persistence

Open the Web UI again. **Chat history, agents, and cost should all still be
there** — the container was recreated, but state lived on `/data`.

![State survived the update](docs/local-testing/09-state-survived.webp)

Optionally confirm on disk, from the host debug shell (the one with `docker`):

```sh
CID=$(docker ps --filter name=wactorz_test -q)
docker exec "$CID" ls -l /data/state /data/mosquitto
# expect: state/wactorz.db  and (embedded MQTT) mosquitto/mosquitto.db
```

---

## Gotchas (the "avoid the mess" checklist)

- **Files at the top level** of the add-on folder — no nested `ha-addon/`.
- **Distinct `slug`** (`wactorz_test`) so it doesn't collide with the store add-on.
- **The `image:` removal + `build.yaml` `WACTORZ_REF` are local-only** — never commit them.
- **`run.sh` is picked up locally** (no git). **Python needs the branch pushed.**
- **Test with an *update*, not a restart** — a restart can't reveal a persistence bug.
- Expected, harmless: `Warning: Mosquitto should not be run as root` — that's the
  `user root` setting that lets the broker persist retained messages to `/data`.
- Storage map: `/data` = add-on-private persistent store
  (chat/SQLite/pickle, embedded Mosquitto retained messages under `/data/mosquitto`).

## Cleanup / shipping

- Remove the test add-on: uninstall it, then `rm -rf /addons/wactorz-test`.
- To ship the fix: merge your branch to `dev`, then `dev → main` (manual). On the
  release tag (`vX.Y.Z`), the **Add-on Image** workflow builds the prebuilt image
  from `main` and pushes `ghcr.io/waldiez/wactorz-addon-{arch}:X.Y.Z`. Bump
  `version` in `config.yaml` to match — Supervisor then **pulls** `{image}:{version}`
  (with progress), no on-device build.
