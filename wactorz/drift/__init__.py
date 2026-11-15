# SPDX-License-Identifier: Apache-2.0
"""Whether a model's readings still look like what it was made for.

`detectors` are the three tests, on plain numbers: PSI, the two-sample KS
test and ADWIN. `check` runs them over the rows of two windows and says what
alarmed; `record` writes an alarm into the provenance graph as an
``observation``. The agent that reads the windows on a cadence is
`wactorz.agents.drift_agent`; ``docs/design/drift.md`` is the decision.
Nothing here needs more than the standard library.
"""

from .check import (
    ADWIN,
    DEFAULT_SCORE_COLUMN,
    KS,
    PSI,
    TESTS,
    Reading,
    Thresholds,
    Window,
    alarmed,
    check_features,
    check_scores,
)
from .detectors import Adwin, KSResult, adwin_changes, ks, psi
from .record import DRIFT, alarms_of, observation_id, raised_episodes, record_alarm

__all__ = [
    "ADWIN",
    "DEFAULT_SCORE_COLUMN",
    "DRIFT",
    "KS",
    "PSI",
    "TESTS",
    "Adwin",
    "KSResult",
    "Reading",
    "Thresholds",
    "Window",
    "adwin_changes",
    "alarmed",
    "alarms_of",
    "check_features",
    "check_scores",
    "ks",
    "observation_id",
    "psi",
    "raised_episodes",
    "record_alarm",
]
