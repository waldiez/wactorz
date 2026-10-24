# Accessibility

Wactorz is run from its dashboard: people watch their agents there, answer the questions agents
ask, and talk to them, at a desk or on a phone, on their own or inside Home Assistant. We want
that to work for everyone who runs it, including people who use only a keyboard, a screen
reader, magnification, or high contrast settings.

This document says what we aim for, what we check on every change, what we know still falls
short, and how to tell us about a barrier you meet.

It covers the web dashboard, wherever it is served from: `wactorz` itself, or the Home
Assistant add-on's panel. The command line, the documentation site and the chat
channels (Telegram, Discord, WhatsApp, whose apps are their own) are not covered yet.

## Priorities

- **Target:** we work toward [WCAG 2.2 Level AA](https://www.w3.org/TR/WCAG22/). It guides our
  work; it is not a claim that the dashboard conforms, and nobody has evaluated it against it.
- **Keyboard:** everything you can do with a pointer, you can do with Tab, Enter, Space and
  Escape, and you can always see where focus is.
- **Screen readers:** controls are named for what they do ("Stop weather", not "Stop"), lists,
  dialogs and landmarks are marked as such, and what changes on its own (a reply arriving, the
  connection dropping) is announced.
- **Reading:** text keeps a contrast of at least 4.5:1, status never relies on colour alone, and
  the page follows your system's "reduce motion" setting.
- **Phones:** the operator's views work at a phone's width, with one thumb.

## What we check

These run on every change to the dashboard, and a failure blocks it:

- [axe](https://github.com/dequelabs/axe-core) on every view of the dashboard as the page builds
  it, in the frontend's unit tests. This checks structure: roles, names, and ARIA that is valid
  where it is used.
- axe again in a real browser (Chromium), in the end-to-end tests, wherever something new is on
  screen: every view, a conversation, the feed with entries, the history panel, the question
  before a delete, a question on a notice and in the chat. Here colour contrast is checked too.
- A walk through every view with Tab, which fails wherever focus does not show.

A serious or critical finding fails the change. One that has to stay for a while is listed in
[`e2e/harness/axe-accepted.json`](e2e/harness/axe-accepted.json) with the reason; the list is
empty.

## Contributor expectations

If you change the dashboard:

- Find elements in tests by their role and name, as a screen reader user would, and build what
  assistive technology reads with the helpers in `frontend/src/ui/dom.ts`. The frontend's
  [CONTRIBUTING.md](frontend/CONTRIBUTING.md) explains both, under "Finding elements" and
  "Accessibility checks".
- Take text colours from the tokens in `frontend/src/styles/base.css`, so nothing dimmer than
  the contrast floor is used for words.
- Go through anything interactive you add with the keyboard alone: it can be reached, it shows
  focus, it works with Enter or Space, Escape closes what opens, and focus is never trapped.
- If an automated check flags something you cannot fix in the change, say why in the pull
  request, and add it to the accepted list with the reason.

## Reporting a barrier

If something in the dashboard stops you, or makes a task harder than it should be, please tell
us. Reports like yours are how we find what our checks cannot.

1. [Open an accessibility issue](https://github.com/waldiez/wactorz/issues/new?template=accessibility.yml).
2. Share what you are comfortable with. Useful, when you can:
   - what you were trying to do, and what happened instead;
   - where in the dashboard (the view, or what was on screen);
   - your browser, operating system, and any assistive technology, with versions;
   - whether you were in Home Assistant, and on a phone or a computer;
   - a screenshot or a recording, only if you want to.

You never need to tell us about a disability or anything else personal. A security problem
goes to [SECURITY.md](SECURITY.md) instead, not to a public issue.

### Severity

You can suggest one; we may adjust it when we look at the report.

- **Critical:** you cannot do something the dashboard is for (for example, answer an agent's
  question, or stop an agent).
- **Serious:** you can, but only with real difficulty or a workaround.
- **Moderate:** it gets in the way, or behaves inconsistently.
- **Minor:** it is noticeable but barely affects what you can do.

### What happens next

- We reply to say we have it, and treat it as expertise, not as a complaint.
- We say what we will do, what limits it, and give a workaround where there is one.
- We work on it by severity, critical first, and say so on the issue when that changes.
- We may ask you to check that a fix works for you before we close it.

## Ownership

The maintainers of [waldiez/wactorz](https://github.com/waldiez/wactorz) look after
accessibility: they triage reports, keep track of the known gaps below, and keep this document
current.

## Where it is tested

- **Checked automatically:** Chromium, at a desktop window's size, through the tests above.
- **Intended to work in:** current Chrome, Edge, Firefox and Safari, on desktops and phones, and
  inside Home Assistant's own pages.
- **Assistive technology:** none has been tried with the dashboard yet. We would welcome a report
  from anyone who uses it with one.

## Known limitations

- **No screen reader pass yet.** What the checks above see is how the page is marked up, not how
  a screen reader reads it. The conversation and the feed announce new messages, but they are
  redrawn whole when you switch conversation or change a filter, and a screen reader may read
  everything in them out again.
- **Contrast is checked only on what the tests put on screen.** States they do not reach, such
  as some warnings and errors, may still fall short.
- **The phone layout is not checked automatically.** The tests run at a desktop window's width.
- **English only.** The dashboard has no translations yet.
- **Charts** show a trend as a picture. Each one has a short text label, and the history panel
  has the same figures as a table below its charts.

If you find a barrier that is not listed here, please report it.

## Feedback

To suggest a change to this document, or to how we work on accessibility, open an issue or a
pull request. Thank you for helping make Wactorz usable by everyone.
