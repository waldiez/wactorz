# The promotion policy

A decision record, not a guide: what makes a version on trial the version
served, who decides it, and what is stored to say so. It builds on the
pointers and the shadow trial (`model-pointers.md`), on what a version is
judged on (`wactorz.core.judgement`), and on the `train` step
(`train-step.md`). The "What follows" section says what is built.

## The problem

A shadow trial ends with a comparison: agreement, scores, latencies, and when
the version declares what it is judged on, each side's precision and recall
against the labels, with intervals and a test of whether the difference is
chance. That says how the two versions differ. It does not say whether the
difference is enough, over enough time, on enough labelled events, for the
shadow to be served, and nothing decides that but a person on the Models tab.
A loop that retrains on its own and then waits for a person at every
promotion is not closed; one that promotes on any difference is reckless.

The `train` step has a margin of its own, met on held-out rows before the
version is stored. That is the wrong thing to promote on: the recipe's score
on recorded rows is not the model's behaviour on live input beside the one
served, which is what the trial measures.

## The decision

### A policy per model name

A model's name carries a policy, beside its pointers, saying when its shadow
may become active:

| Field | What it says | Default |
| --- | --- | --- |
| `metric` | The figure compared, among those the trial reports for the model's kind: `f1`, `precision`, `recall`, `mcc`, `false_positive_rate` for a detector; `macro_f1`, `weighted_f1`, `accuracy` for a classifier; `mae`, `rmse` for a regressor, where less is better | the kind's main one, the first named |
| `margin` | How much better the shadow must be on it | `0` |
| `min_days` | How long the trial must have run, in the event time its pairs cover (`event-time.md`) | `1` |
| `min_labelled` | How many labelled inputs the trial must hold | `100` |
| `significant` | Whether the test must say the difference is not chance: McNemar's, or for a regressor the sign test on which side was nearer the truth | `true` |
| `automatic` | Whether main promotes when the policy is met, or only says so | `false` |

The policy is about the name, not a version: it holds across retrains, since
the question "when is a new detector good enough" is asked of the model, not
of version seven. It lives in the store's index under its own key, `policies`,
beside `pointers`, changed under the same lock by `wactorz-artifacts policy`,
`/artifacts policy` in chat and the Models tab, each recording who set it and
when. The index format stays 2: a reader that does not know the key drops a
setting, not a served model, and says nothing it should not; the writer keeps
every key it read.

### Main checks, on a cadence, and says what it found

While a shadow is set, main reads the trial on a cadence and judges it as the
version declares. When every term is met, main writes a `decision` node about
the model, with the figures, the policy as it stood and the verdict, and:

- with `automatic` off, puts the promotion in the Inbox as a question to the
  operator, with the figures, whose answer promotes or clears; the question
  stands until answered or the trial ends;
- with `automatic` on, promotes as `policy`, with the figures in the move's
  `reason`, so the feed and the history say why.

A trial that does not meet the policy is left running; nothing clears it,
since the next day's labels may change the verdict. When the terms were met
and are no longer, the decision stands and a new one records the change. A
version the operator promotes by hand before the policy is met is still a
promotion: the policy says when main may act, not when a person may.

### The train step's margin is not this

The step's margin, on held-out rows, decides whether a version is worth a
trial. This policy, on the trial, decides whether it is worth serving. The two
are different figures on different data, and each is stored where it was met:
the step's in the move to `shadow`, the policy's in the `decision` node and
the move to `active`.

## What was decided against

- **Automatic promotion by default.** Rejected: an install that has not
  thought about it should keep its operator in the loop; one that has sets
  `automatic`.
- **The serving agent deciding.** Rejected, as `model-pointers.md` says: the
  comparison is shown and recorded there; the decision reads the policy and
  writes the graph, which is main's.
- **Promoting on the step's score.** Rejected, above: a figure on recorded
  rows is not the live behaviour the pointer is about.
- **A policy per version.** Rejected: the terms would be copied forward with
  every retrain and drift apart; a version's own terms are `judged_on`, which
  is about how it is judged, not about when it is served.
- **A policy in the graph, or in the database.** Rejected: the policy is a
  setting beside the pointers it governs, changed under the same lock, and
  exported with the store.
- **A minimum number of pairs instead of labelled inputs.** Rejected: pairs
  without labels say only that the two agreed or did not; what is promoted on
  is being right.

## What follows

- **1.6b, the policy** (built): the `policies` key and its commands
  (`wactorz/core/promotion.py`, `ArtifactStore.policy`, `wactorz-artifacts
  policy`, `/artifacts policy`, `PUT /api/models/{name}/policy`), main's
  cadence check with the `decision` node, the Inbox question and the automatic
  move as `policy` (`wactorz/agents/main/shadow_serving.py`), and the Models
  tab's trial card showing each term met or short. One thing to know: a
  decision is written when the verdict changes, not at every check, and a
  verdict already decided in an earlier run of main is neither asked nor
  promoted again.
- **1.9, the controller**, reads the same policy and the same decisions, and
  may ask for a promotion earlier than the cadence with a decision of its own.
- **1.12, the review**, finds the promotions that followed from a label
  through the `decision` nodes written here.
