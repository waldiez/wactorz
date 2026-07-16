/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wire contract for the TTS voice list — the shape the server returns from
 * `GET /api/tts/voices`. Shared by `TTSManager` and the event bus's
 * `tts-voices-loaded` payload; the manager's own behaviour stays in `io/`.
 */

export interface TTSVoice {
    name: string;
    locale: string;
    gender: string;
}
