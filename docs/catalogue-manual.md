# Device Manuals agent

`manual-agent` — searches the web for a device's manual, downloads the PDF, extracts the
text, and answers questions about it using the agent's LLM.

**Dependencies** (installed on first spawn): `httpx`, `pdfplumber`, `duckduckgo_search`.

## Spawn

```text
@catalog spawn manual-agent
```

## Usage

```text
load the manual for a Bosch WAT28371 washing machine
how do I start an eco cycle?
what does error E18 mean?
```

Load a manual first, then ask questions against it.

## Structured operations

| Field | Meaning |
|-------|---------|
| `action` | `load_manual`, `ask`, `status`, `clear` |
| `device` | model name or query (for `load_manual`) |
| `question` | question about the loaded manual (for `ask`) |
| `url` | the manual's address, to skip the search (for `load_manual`) |

The agent fetches only public web addresses — a search result, a link in a page,
or a `url` you give it — and checks every redirect the same way, so a manual on
your own network cannot be loaded by URL. A download stops at 50 MB.

Returns `success`, `device`, `url` (the PDF), `pages`, `chars`, a `preview`, and the LLM
`answer`.
