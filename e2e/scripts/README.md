# Fake-provider scripts

JSON objects mapping a substring of the user's message to the reply the fake
provider gives it. Loaded by the profile and handed to the backend as
`LLM_FAKE_SCRIPT`; the provider itself is `wactorz/agents/llm/providers/fake.py`.

Longest key wins, so a specific phrase beats a substring of itself and adding a
broad entry cannot shadow the precise ones already here.

`default.json` is what the `test` profile runs: the minimum needed for the
regression core to be deterministic, and nothing written for an audience.
`demo.json` is for the camera — the same mechanism, answers phrased so a
recording reads well.

No scenario names a script. A scenario that only passes under one set of replies
is asserting what the model said, which is the thing that cannot hold under both
providers.
