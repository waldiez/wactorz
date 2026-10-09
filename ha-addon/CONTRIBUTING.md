# Contributing to ha-addon

## DOCS.md — single source of truth

`ha-addon/DOCS.md` (this directory) is the canonical user documentation. The two
add-on folders each need their own copy (the Supervisor renders `<addon>/DOCS.md`
in the add-on info tab), so after editing the canonical file, sync it down:

```bash
cp ha-addon/DOCS.md ha-addon/wactorz/DOCS.md
cp ha-addon/DOCS.md ha-addon/wactorz-ultra/DOCS.md
```

The three files must stay byte-identical — never edit the copies directly.

## Adding or modifying an option

Options are defined in three places that must stay in sync:

| File | What to change |
|---|---|
| `config.yaml` → `options:` | Default value |
| `config.yaml` → `schema:` | Type declaration (`str`, `bool`, `int`, `float`, `url`, `list(a\|b)`, trailing `?` = optional) |
| `run.sh` | Export the value as an env var that Wactorz reads |
| `DOCS.md` | User-facing description in the Options table |

### Step-by-step

1. Add the option under `options:` in `config.yaml` with a safe default.
2. Add the matching type under `schema:`. Use `str?` for optional strings (Supervisor validates this).
3. In `run.sh`, read the value with `jq` and export it:
   ```bash
   MY_OPTION=$(jq -r '.my_option // ""' "${OPTIONS_PATH}")
   export MY_OPTION
   ```
4. Update the Options table in `DOCS.md` with a clear one-line description.
5. Bump the patch version in `config.yaml` if the change is non-breaking, minor version if it removes or renames an existing option.

## Updating the Dockerfile

- **Base image**: pinned by digest in `ha-addon/bases/Dockerfile`, one line per add-on and architecture, which Dependabot keeps current and the image workflow builds on. Each add-on's `build.yaml` (`build_from`, for a Supervisor source build) and its Dockerfile's `BUILD_FROM` default name the floating tag of the same line (`3.14-alpine3.24`, `trixie`), so Dependabot's weekly dated build changes the bases file alone; a new line (another Python or OS release) is a hand edit of all three, and `tests/test_addon_bases.py` fails until they agree. Only one Dockerfile per add-on is needed.
- **System packages** (`apk add`): Add to the existing `RUN apk add --no-cache` line — avoid extra layers.
- **Wactorz version**: the pip install takes a git ref through the `WACTORZ_REF` ARG, which has no default: a build without one stops rather than installing whatever a branch holds that day. A **release** builds its own tag (`@v0.5.3`), after its tests pass, so a released image is reproducible; `build.yaml` names the same tag for a source build, kept in step by `scripts/sync_versions.py`. A manual run of **Add-on Image** takes the ref and the image tag you give it, and is a dry run unless you untick it; that is the path for an add-on-only rebuild, where the add-on version gains a fourth component (`0.5.3.1`) and the library stays put.
- **New binaries/services**: Add them to the same Alpine RUN block or a dedicated RUN block. If the service needs a config file, `COPY` it alongside `run.sh` and reference it in the entrypoint.

## Modifying run.sh

`run.sh` is the addon entrypoint. Keep it readable:

- Use `jq -r '.key // "default"'` for every option read — never assume the key exists.
- Start embedded services before Wactorz and wait for them to be ready (`mosquitto &` then a short health poll).
- `exec wactorz` at the end so Wactorz is PID 1's child and receives signals correctly.
- Test changes locally with `OPTIONS_PATH=/tmp/options.json bash ha-addon/run.sh` (see `README.md` for a sample `options.json`).

## Testing locally

The quickest loop without a real HA install:

1. Build the image, on the pinned base and from a pushed branch, tag or sha:
   ```bash
   docker build \
     --build-arg BUILD_FROM="$(sed -n 's/^FROM \(.*\) AS wactorz-amd64$/\1/p' ha-addon/bases/Dockerfile)" \
     --build-arg WACTORZ_REF=dev \
     -t wactorz-addon-dev ha-addon/wactorz/
   ```
2. Run with a mock options file:
   ```bash
   docker run --rm \
     -v /path/to/options.json:/data/options.json \
     -p 8000:8000 -p 8888:8888 \
     wactorz-addon-dev
   ```
3. For a proper Supervisor integration test, follow the [HA addon dev docs](https://developers.home-assistant.io/docs/add-ons/testing).

For testing on a **real HA OS box** — required to validate anything about
**state persistence across updates** (a `docker restart` can't reveal those
bugs) — follow the **[local add-on workflow](LOCAL_TESTING.md)**.

## Schema validation gotchas

- `str?` means the field is optional; an absent key is valid. For tokens and credentials that might be blank use `password?`, which is optional the same way and masked in the Supervisor's UI.
- `url` type requires a valid URL scheme; don't use it for hostnames-only values (use `str` instead).
- `list(a|b|c)` is an enum — the Supervisor rejects any value not in the list.
- `port` is a shorthand for `int` with port-range validation (1–65535).

## Release checklist

- [ ] Bump `version:` in `config.yaml`.
- [ ] Update `DOCS.md` Options table if any option was added/changed/removed.
- [ ] Verify `schema:` and `options:` are in sync (every key in `options:` must have a matching `schema:` entry).
- [ ] Test `run.sh` locally with a representative `options.json`.
- [ ] Open PR; addon CI will lint `config.yaml` automatically.
