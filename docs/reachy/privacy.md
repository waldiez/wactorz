# Privacy and data

Reachy has a microphone and a camera and is often used around members of the public. This
page says plainly what is recorded, where it goes, and what is kept. It describes the
software's behaviour; it is not a legal assessment, and it does not make any deployment
compliant with GDPR or any other regulation by itself.

## The microphone is off unless asked

- Reachy never listens on its own. There is **no wake word** and no always-on recording.
- The microphone records only during:
  - `listen` / `listen and ask Wactorz`: one bounded clip (5 seconds by default, at most 30);
  - a conversation started with `start conversation`, until it ends;
  - the optional interruption check during a conversation started "with interruption".
- `doa` reads the direction a sound came from without recording.

## Where audio goes

| Speech recognizer | Where your voice is processed |
| --- | --- |
| `deepgram` (**default**) | **Sent to Deepgram, Inc.**, a hosted service. In a conversation, audio is streamed while you speak. Their terms and retention policy apply to your account. |
| `openai` | **Sent to OpenAI.** |
| `faster-whisper`, `whisper` | **Stays on the computer running Wactorz.** |

The dashboard's mic button follows the same table when its engine is **Server**. With
the **Browser** engine, the audio goes to the browser's own speech service instead:
Google for Chrome, Microsoft for Edge. **Auto** uses the server when it has a
recognizer, otherwise the browser.

To keep voices on your own hardware, set `REACHY_STT_BACKEND=faster-whisper` (see
[Configuration](configuration.md#speech-recognition)) and choose **Server** or **Off**
for the dashboard mic.

## Where text and images go

- **Transcripts and typed messages** are sent to the language-model provider configured
  for Wactorz (`LLM_PROVIDER`) to work out what to do and to answer. With Ollama this
  stays local.
- **Everything Reachy says aloud** is sent as text to Microsoft's Edge text-to-speech
  service to be turned into audio. There is currently no local speech-output option.
- **Camera images** for "what do you see?" and "look around" are sent to the language-model
  provider. `take a photo` sends nothing anywhere unless you ask it to publish or save the
  frame.

## What is kept

| Data | Kept? |
| --- | --- |
| Raw audio | Not kept. Clips live in memory and in short-lived temporary files deleted after transcription. A clip is written to disk only if a `listen` command names a `path`. |
| Camera frames | Not kept, unless a `camera` command names a `path` or asks to `publish`. |
| Transcripts and replies | **Kept** in the Wactorz chat history (its local SQLite database), like any chat, and visible on the dashboard. Clear it with the reset API's `chat` scope (`POST /api/reset`, or the `wactorz-reset` command; see the [API reference](../api.md)). |
| Personal facts | Wactorz's main agent can remember facts about a user. From **voice**, it remembers only when explicitly told ("remember that…"), so a misheard sentence does not become a stored fact. |
| Speech audio files | Temporary MP3 files; each is deleted when the next sentence plays, so at most one remains. |
| Logs | The Wactorz log can contain short excerpts of transcripts and requests at the default level. Treat `wactorz.log` as personal data at a public event. |

## Who can control the robot

Anyone who can publish to Wactorz's MQTT broker can command Reachy, including asking it to
record, and anyone who can subscribe to it sees each conversation's transcripts and
replies on `custom/reachy/events`. The bundled development broker listens only on the local computer and requires a
password. Do not expose the broker or the dashboard to a shared network without
`MQTT_PASSWORD` and `API_KEY` set; see the [Wactorz security guide](../security.md).

## At a public demonstration

- Put up a visible notice that the robot can record voice and images when asked, and
  which services process them.
- Prefer local speech recognition when visitors may include children or when you cannot
  give notice.
- Do not leave a conversation running unattended; use `inactivity_timeout` so it stops
  by itself.
- After the event, clear the chat history if you do not need it, and delete or rotate
  `wactorz.log`.
