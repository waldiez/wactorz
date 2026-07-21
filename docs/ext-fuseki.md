# Fuseki extension

Bridges Wactorz to an Apache Jena Fuseki triple store: agent manifests and
Home Assistant state become RDF, queryable from the dashboard's **Graph** tab
through the `/api/fuseki/<dataset>/sparql` proxy. See [Extensions](extensions.md)
for the general extension mechanics this module follows.

## Layout

```
wactorz/ext/fuseki/
├── __init__.py     # setup(), public_config(), on_ready(), __deps__
├── bridge.py       # FusekiClient, HAFusekiBridge, HAWebSocketClient
├── manager.py      # BridgeManager — instance state (ha_task, agent_tasks)
└── proxy.py        # aiohttp route handlers for /api/fuseki/<dataset>/sparql

frontend/src/ext/fuseki/
├── index.ts        # register({ url, dataset, onRender, registerView })
└── fusekiView.ts   # the Graph tab (SPARQL console)
```

The extension declares `__deps__ = ["swid"]`: its `on_ready()` links agent DIDs
onto the graph nodes, so it runs after the swid extension has minted them.

## Activation

Entirely env-gated — without `FUSEKI_URL` the extension is a no-op:

```bash
FUSEKI_URL=http://fuseki:3030     # in .env; container service name or localhost
FUSEKI_DATASET=wactorz            # optional, default "wactorz"
FUSEKI_USER=admin                 # optional
FUSEKI_PASSWORD=admin             # optional
```

The Fuseki server itself ships as an additive compose overlay:

```bash
docker compose -f compose.yaml -f compose.fuseki.yaml --profile full up -d
```

## Frontend

`public_config()` exposes `{url, dataset}` (never credentials); the barrel
registers `wactorz-fuseki-url` / `wactorz-fuseki-dataset` via
`registerConfigEntry()`, and `register()` adds the Graph tab only when a URL is
configured.
