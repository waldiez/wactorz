/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Wire contract for the TTS voice list — the shape the server returns from
 * `GET /api/tts/voices` — and what the audio settings need of whoever speaks.
 */

/** One voice the server offers. */
export interface TTSVoice {
    /** Its name, which picks it. */
    name: string;
    /** Its language. */
    locale: string;
    /** Its gender. */
    gender: string;
}

/** What the audio settings read and change, whichever extension speaks. */
export interface VoiceControls {
    /** Whether a beep sounds on each message. */
    readonly beepEnabled: boolean;
    /** Whether replies are read aloud. */
    readonly ttsEnabled: boolean;
    /** The voices to choose from. */
    readonly voices: readonly { name: string }[];
    /** The voice chosen, or "". */
    readonly selectedVoice: string;
    /** Turn the beep over; returns the new state. */
    toggleBeep(): boolean;
    /** Turn reading aloud over; returns the new state. */
    toggleTTS(): boolean;
    /** Choose a voice. */
    setVoice(name: string): void;
}
