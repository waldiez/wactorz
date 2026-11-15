# SPDX-License-Identifier: Apache-2.0
"""The ``train`` step: a new version of a model, made inside the loop.

A recipe is a module with ``train(frame, settings) -> model`` and
``score(model, frame) -> metrics``, and may add ``save(model, directory)``
(`recipe`). A run reads a window of one source through `wactorz.data`, cuts
it with a split the loader makes (`splits`), fits and scores in a child
process with a timeout and a memory limit (`worker`), stores what it made
with everything needed to reproduce or judge it, and moves the pointer it is
for only when the new version beats the one served by the margin asked
(`run.run`). Each run is an ``action`` in the provenance graph, each version
stored a ``model`` node (`record`). ``docs/design/train-step.md`` is the
decision.

`spec`, `recipe`, `record` and `run` need nothing but what core needs; `splits`
and `worker` read frames and need pyarrow, the ``data`` extra, so they are
imported where they are used.
"""

from .record import model_node_id
from .run import Outcome
from .spec import POINTERS, SplitSpec, TrainError, TrainSpec

__all__ = [
    "POINTERS",
    "Outcome",
    "SplitSpec",
    "TrainError",
    "TrainSpec",
    "model_node_id",
]
