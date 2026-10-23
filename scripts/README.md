# scripts/

Build and maintenance scripts.

| | |
| --- | --- |
| `build.py` | Package build entry point |
| `build_docs.py` | Renders `docs/*.md` into `static/docs/` |
| `sync_versions.py` | Propagates a version across the manifests |
| `gen_ha_icons.py` | Generates the Home Assistant add-on icons |
| `reachy_sim_check.py` | Runs the Reachy Mini agent against the SDK's simulated robot; no hardware needed (`--recovery` also tests reconnecting) |
| `hooks/` | Hatch build hooks |
| `start.ps1`, `start.bat`, `watch-costs.ps1` | Windows launchers |

See [docs/quickstart.md](../docs/quickstart.md) for running Wactorz itself.
