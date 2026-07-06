# Gmail agent

`gmail-agent` — search and read your mail, list labels and drafts, and create drafts.
It is **draft-first**: it never sends email, only prepares drafts for you to send.

> **Setup:** one-time OAuth — see **[Google agents — setup & login](catalogue-google-setup.html)**.
> Gmail additionally needs the Gmail API enabled and your account added as a test user
> (its scopes are *restricted*).

## Spawn

```text
@catalog spawn gmail-agent
```

## Usage

```text
any unread email?
show my inbox
emails from stripe
find invoices
what does the trello one say?           # opens & reads that email
content of the latest vodafone bill     # with an LLM, answers "what do I owe?"
list my labels
show my drafts
draft an email to sam@example.com about lunch
```

**Reading & answering.** `read` opens one message (the top match of a topic, or an explicit
id) and returns its body — text/plain, or HTML stripped to text, with tracking URLs and
boilerplate removed. With an LLM configured, it returns a concise answer to your question
(quoting exact amounts/dates) instead of the raw email.

**Drafting.** "draft an email to …" collects the recipient, subject, and body over a short
back-and-forth, then saves a Gmail draft. The agent never sends — you review and send it.

## Structured operations

| Field | Meaning |
|-------|---------|
| `operation` | `status`, `search`, `unread`, `inbox`, `read`, `labels`, `drafts`, `create_draft` |
| `query` | Gmail search query (`search`) or topic/sender to open (`read`) |
| `to`, `subject`, `body` | draft fields (`create_draft`) |
| `thread_id` | message/thread id (`read`) |

## Notes & troubleshooting

- **Free-text search** (`find invoices`, `from:…`) needs a token **without** `gmail.metadata`.
  `wactorz-google-login gmail` requests the right scopes; label filters (`unread`, `inbox`,
  starred) work regardless.
- **"The caller does not have permission"** — expected from the hosted MCP; the REST
  fallback serves it. See the [setup page](catalogue-google-setup.html#troubleshooting).
- **The agent won't send mail** — by design. It creates drafts only.
