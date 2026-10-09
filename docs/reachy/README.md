# Reachy Mini with Wactorz

Talk to a [Reachy Mini](https://www.pollen-robotics.com/reachy-mini/) robot in plain
language, by typing or by voice, and have it move, speak, look through its camera and
pass requests on to the rest of your Wactorz agents and your Home Assistant home.

> **Status: beta.** The integration has been shown to the public and is used for
> supervised demonstrations. It is not meant for unattended production use. Keep the
> robot within reach while it moves.

## Pick your guide

| You are… | Start here |
| --- | --- |
| Setting it up for the first time | [Getting started](getting-started.md) |
| Installing on a specific system, or something failed to install | [Installation](installation.md) |
| Looking for a setting | [Configuration reference](configuration.md) |
| Using it day to day | [User guide](user-guide.md) |
| Fixing a problem | [Troubleshooting](troubleshooting.md) |
| Preparing a public demonstration | [Demonstration guide](demo-guide.md) |
| Extending or changing the code | [Developer guide](developer-guide.md) and [Architecture](architecture.md) |
| Wondering where your voice goes | [Privacy and data](privacy.md) |

The complete command and MQTT reference is the
[Reachy Mini catalogue page](../catalogue-reachy-mini.md).

## What it is, in one paragraph

Wactorz is a self-hosted runtime for AI agents. Reachy Mini joins it as a
**catalogue agent** called `reachy-mini`: a program Wactorz starts on request
(`@catalog spawn reachy-mini`) that connects to the robot with Pollen Robotics' official
Python SDK. Text you type in the Wactorz dashboard, or speech Reachy hears, becomes robot
commands. When it is a question rather than a command, it is passed to Wactorz's main
agent, whose answer Reachy speaks aloud. Wactorz is required: this is not a standalone
Reachy application.

## What it can do

- **Move and express:** wake, sleep, look in a direction, turn, nod, shake its head,
  dance, wiggle its antennas, play Pollen's recorded emotion clips, and (optionally) keep
  subtle "alive" idle motion going.
- **Speak:** any text, in English or Greek, through the robot's own speaker, at named
  volume levels.
- **Listen:** one question at a time (push-to-talk), or a continuing hands-free
  conversation that ends when you say "goodbye".
- **See:** take a photo, or describe what is in front of it or around the room using a
  vision-capable language model.
- **Act on your home:** "turn off the living-room light" and similar requests go through
  Wactorz to Home Assistant, when Home Assistant is configured.
- **Report on itself:** connection state, motor faults the robot reports, and why it
  cannot do something.
