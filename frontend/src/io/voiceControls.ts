/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Who the audio settings talk to. An extension that speaks provides itself here,
 * so the settings need not import it; with none, they offer a page that is silent.
 */
import type { VoiceControls } from "../types/tts";

const SILENT: VoiceControls = {
    beepEnabled: false,
    ttsEnabled: false,
    voices: [],
    selectedVoice: "",
    toggleBeep: () => false,
    toggleTTS: () => false,
    setVoice: () => {},
};

let provided: VoiceControls = SILENT;

/** Offer `controls` to the audio settings. */
export function provideVoiceControls(controls: VoiceControls): void {
    provided = controls;
}

/** Whoever speaks. */
export function voiceControls(): VoiceControls {
    return provided;
}
