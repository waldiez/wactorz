# Local add-on testing

Use this workflow to test the Home Assistant add-on locally on a real HAOS machine.

## What comes from where

The local add-on folder and the installed Python package are separate inputs:

| Item | Source | How to test changes |
|---|---|---|
| `config.yaml`, `build.yaml`, `run.sh` | Local files under `/addons/wactorz-test` | Copy or edit the files on the HA host |
| `wactorz` Python package | Installed by the Dockerfile from GitHub | Push your branch and set `WACTORZ_REF` to that branch |

If `config.yaml` contains `image:`, Supervisor pulls a prebuilt image and does
not build the local Dockerfile. Remove `image:` in the test copy when you want to
test local Dockerfile or Python-package changes.

## 1. Copy the add-on

On the HA host, copy the contents of this repository's `ha-addon/` directory into
a local add-on folder:

```sh
mkdir -p /addons/wactorz-test
cp -r /path/to/repo/ha-addon/. /addons/wactorz-test/
```

The files must be directly under `/addons/wactorz-test`. Supervisor will not
detect the add-on if the files end up under `/addons/wactorz-test/ha-addon/`.

Edit `/addons/wactorz-test/config.yaml` so the local copy has a unique name and
slug:

```yaml
name: Wactorz (test)
slug: wactorz_test
```

## 2. Choose what to test

### Test a local source build

Use this path when testing Dockerfile changes or Python changes from a branch.

1. Remove the `image:` line from `/addons/wactorz-test/config.yaml`.
2. Set the branch or commit to install in `/addons/wactorz-test/build.yaml`:

   ```diff
   -  WACTORZ_REF: main
   +  WACTORZ_REF: your-branch
   ```

The branch must be pushed to GitHub before Supervisor builds the image.

`run.sh` changes are read from the local add-on folder. They do not need a Git
branch unless the change is also part of the Python package.

### Test a prebuilt image

Use this path when testing the published-image workflow.

1. Run the **Add-on Image** workflow manually.
2. Set `ref` to the branch or commit to install.
3. Set `version_tag` to a test tag, for example `test`.
4. Point the test add-on at that image tag:

   ```yaml
   image: "ghcr.io/waldiez/wactorz-addon-{arch}"
   version: "test"
   ```

Supervisor pulls `{image}:{version}`, so the `version` value must match the image
tag you pushed.

## 3. Install the local add-on

In Home Assistant, go to **Settings -> Add-ons -> Add-on Store -> menu -> Check
for updates**. The test copy should appear under **Local add-ons**.

![Local add-ons in the store](docs/local-testing/01-local-apps.webp)

Open it, install it, and start it.

![Install the test add-on](docs/local-testing/02-install.webp)

## 4. Create state to verify later

Open the Wactorz web UI and create state that should survive an update: send a
chat message, spawn an agent, or let some cost accrue.

![Generate state in the Web UI](docs/local-testing/03-generate-state.webp)

## 5. Simulate an update

Bump the version in `/addons/wactorz-test/config.yaml`, for example:

```diff
-version: "0.4.4-test.1"
+version: "0.4.4-test.2"
```

![Bump the version](docs/local-testing/04-bump-version.webp)

Then go to **Add-on Store -> menu -> Check for updates**.

![Check for updates](docs/local-testing/05-check-updates.webp)

The add-on should show **Update available**. Click **Update** and confirm the
version change.

![Update available](docs/local-testing/06-update-available.webp)

![Update dialog](docs/local-testing/07-update-dialog.webp)

Wait until the installed version matches the latest version.

![Update complete](docs/local-testing/08-update-complete.webp)

## 6. Verify persistence

Open the Wactorz web UI again. Chat history, agents, and cost should still be
present after the update.

![State survived the update](docs/local-testing/09-state-survived.webp)

You can also check the add-on data directory from the HA host:

```sh
CID=$(docker ps --filter name=wactorz_test -q)
docker exec "$CID" ls -l /data/state /data/mosquitto
```

Expected files include `/data/state/wactorz.db` and, when embedded MQTT is
enabled, `/data/mosquitto/mosquitto.db`.

## Checklist

- Keep `config.yaml`, `build.yaml`, `Dockerfile`, and `run.sh` at the top level
  of `/addons/wactorz-test`.
- Use a unique `slug`, such as `wactorz_test`.
- Remove `image:` only in the local test copy when you need a source build.
- Push the branch named by `WACTORZ_REF` before building.
- Test persistence with an update, not only with a restart.
- Treat edits under `/addons/wactorz-test` as host-local test changes.

## Cleanup

Uninstall the test add-on, then remove the local copy:

```sh
rm -rf /addons/wactorz-test
```

For a release, publish a matching add-on image tag and add-on version. Supervisor
pulls:

```text
ghcr.io/waldiez/wactorz-addon-{arch}:{version}
```
