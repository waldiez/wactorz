# Contributing to ha-addon

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

- **Base image**: Keep `aarch64-base-python` and `amd64-base-python` in sync. The `BUILD_FROM` ARG is resolved by the Supervisor build matrix; only one Dockerfile is needed.
- **System packages** (`apk add`): Add to the existing `RUN apk add --no-cache` line — avoid extra layers.
- **Fuseki version**: Change the `FUSEKI_VERSION` ARG. Verify the tarball exists at `archive.apache.org/dist/jena/binaries/` before bumping.
- **Wactorz version**: The pip install always pulls `@main`. Version pinning for stable releases happens at the addon `version:` field level, not inside the Dockerfile.
- **New binaries/services**: Add them to the same Alpine RUN block or a dedicated RUN block. If the service needs a config file, `COPY` it alongside `run.sh` and reference it in the entrypoint.

## Modifying run.sh

`run.sh` is the addon entrypoint. Keep it readable:

- Use `jq -r '.key // "default"'` for every option read — never assume the key exists.
- Start embedded services before Wactorz and wait for them to be ready (`mosquitto` / `fuseki-server &` then a short health poll).
- `exec wactorz` at the end so Wactorz is PID 1's child and receives signals correctly.
- Test changes locally with `OPTIONS_PATH=/tmp/options.json bash ha-addon/run.sh` (see `README.md` for a sample `options.json`).

## Testing locally

The quickest loop without a real HA install:

1. Build the image:
   ```bash
   docker build \
     --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20 \
     -t wactorz-addon-dev ha-addon/
   ```
2. Run with a mock options file:
   ```bash
   docker run --rm \
     -v /path/to/options.json:/data/options.json \
     -p 8000:8000 -p 8888:8888 \
     wactorz-addon-dev
   ```
3. For a proper Supervisor integration test, follow the [HA addon dev docs](https://developers.home-assistant.io/docs/add-ons/testing).

## Schema validation gotchas

- `str?` means the field is optional; an absent key is valid. Use it for tokens/credentials that might be blank.
- `url` type requires a valid URL scheme; don't use it for hostnames-only values (use `str` instead).
- `list(a|b|c)` is an enum — the Supervisor rejects any value not in the list.
- `port` is a shorthand for `int` with port-range validation (1–65535).

## Release checklist

- [ ] Bump `version:` in `config.yaml`.
- [ ] Update `DOCS.md` Options table if any option was added/changed/removed.
- [ ] Verify `schema:` and `options:` are in sync (every key in `options:` must have a matching `schema:` entry).
- [ ] Test `run.sh` locally with a representative `options.json`.
- [ ] Open PR; addon CI will lint `config.yaml` automatically.
