# scripts/

Build and maintenance scripts.

| | |
| --- | --- |
| `build.py` | Package build entry point |
| `build_docs.py` | Renders `docs/*.md` into `static/docs/` |
| `sync_versions.py` | Propagates a version across the manifests |
| `gen_ha_icons.py` | Generates the Home Assistant add-on icons |
| `mock-agents.mjs` | Publishes synthetic agent traffic for dashboard work |
| `hooks/` | Hatch build hooks |
| `vendor/` | Bundled third-party code used by the scripts above |
| `start.ps1`, `start.bat`, `watch-costs.ps1` | Windows launchers |

See [docs/quickstart.md](../docs/quickstart.md) for running Wactorz itself.
