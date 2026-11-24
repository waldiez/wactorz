# scripts/

Build and maintenance scripts.

| | |
| --- | --- |
| `build.py` | Package build entry point |
| `build_docs.py` | Renders `docs/*.md` into `static/docs/` |
| `sync_versions.py` | Propagates a version across the manifests |
| `sync_command_docs.py` | Writes the command table in `docs/reference.md` from what `/help` lists (`--check` to test it) |
| `gen_icons.py` | Draws the WZ mark and writes every icon and logo from it: the dashboard's, the add-ons', the README's (`--media` adds the docs site's) |
| `reachy_sim_check.py` | Runs the Reachy Mini agent against the SDK's simulated robot; no hardware needed (`--recovery` also tests reconnecting) |
| `hooks/` | Hatch build hooks |
| `start.ps1`, `start.bat` | Windows launcher: checks Python 3.11+ and `.env`, starts the authenticated broker, installs into the checkout's `.venv` and runs Wactorz (`start.bat` is the double-click wrapper; see [Start script](../docs/windows.md#start-script)) |
| `watch-costs.ps1` | Windows: follows the LLM spend |

See [docs/development.md](../docs/development.md) and [docs/windows.md](../docs/windows.md) for running Wactorz itself.
