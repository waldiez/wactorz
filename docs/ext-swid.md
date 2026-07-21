# SWID extension

Mints real `did:swid` Spatial Web identities (SWF-STD-5, via the `waldiez-swid`
library) for agents, Home Assistant devices, and areas, and adds an **Identity**
tab to the dashboard listing every minted identity with its class, readable
handle, and DID. See [Extensions](extensions.md) for the general extension
mechanics this module follows.

## Layout

```
wactorz/ext/swid/
├── __init__.py     # setup(), on_ready() — minting + identity index
└── resolver.py     # W3C DID Resolution: GET /1.0/identifiers/{did}

frontend/src/ext/swid/
├── index.ts        # register({ onRender, registerView })
└── identityView.ts # the Identity tab
```

The extension populates the app-state keys in `wactorz/core/contract.py`
(`IDENTITY_MINTER`, `AGENT_IDENTITY`); other extensions consume them via
`on_ready()` ordering (`__deps__ = ["swid"]` — e.g. fuseki links DIDs onto
graph nodes).

## Activation

Entirely env-gated — without a keystore passphrase only readable handles are
generated (no key-bound DIDs):

```bash
SWID_KEYSTORE_PASSPHRASE=…        # in .env; enables real DID minting
SWID_DATA_DIR=…                   # optional, keystore + CEL registry location
SWID_HSTP_BASE=…                  # optional, resolver base URL
```

Identities are served from `GET /api/swid/identities` and resolve at
`GET /1.0/identifiers/{did}`.
