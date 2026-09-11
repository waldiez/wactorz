"""Retention: deleting what has been kept long enough.

One job in the maintenance rotation (:mod:`wactorz.core.persistence.maintenance`),
so it runs on a worker thread. The windows are settings — ``RETENTION_*_DAYS`` in
:mod:`wactorz.config` — and 0 keeps a store for ever.

Here rather than under ``core``, because the upload store belongs to the web
layer and this is the one job that needs both, like :mod:`wactorz.reset`.

Not here: the MQTT outbox. It is a database of its own, owned by the publisher,
which expires undelivered messages on its own checkpoint timer.
"""

from __future__ import annotations

import time

from wactorz import config
from wactorz.core.persistence import get_db
from wactorz.web import uploads

#: How often the job does anything. The rotation runs every few minutes; this is
#: needed hourly at most, and each run reads every chat row that names a file,
#: holding the database lock while it does.
EVERY_S = 3600.0

_last_run: float | None = None


def prune() -> dict[str, int]:
    """Prune each store past its window, then sweep uploads nothing refers to.

    The chat log goes first, so a file whose last reference was just pruned is
    collected in this run rather than the next. With no database there is nothing
    to say which files are still referenced, so nothing is swept at all.
    """
    global _last_run
    now = time.monotonic()
    if _last_run is not None and now - _last_run < EVERY_S:
        return {}
    db = get_db()
    if db is None:
        return {}
    _last_run = now
    done: dict[str, int] = {}
    if config.RETENTION_TIMESERIES_DAYS > 0:
        done["timeseries"] = db.prune_old_data(config.RETENTION_TIMESERIES_DAYS)
    if config.RETENTION_CHAT_DAYS > 0:
        done["chat"] = db.prune_chat_log(config.RETENTION_CHAT_DAYS)
    done["uploads"] = uploads.sweep(db.chat_attachment_ids())
    return done
