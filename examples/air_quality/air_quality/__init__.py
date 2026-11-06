# SPDX-License-Identifier: Apache-2.0
"""Room air quality: a model of what is usual for each hour, and a person asked about the rest.

`plant` is the simulated building, `model` what is usual, `train` stores it,
`publish` plays a day of readings, `agents` holds the detector and the agent
that asks, `recipe` is the model as the train step fits it, `loop` the
recorder, drift agent, trainer and rule that retrain without a person, and
`tab` is the dashboard's Air tab.
"""
