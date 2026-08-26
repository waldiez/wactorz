/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Microphone capture for the branches that record in the browser.
 *
 * Recording is MediaRecorder + getUserMedia, which is available wherever audio
 * capture is allowed at all. What it produces is converted to WAV before it is
 * sent, because the recogniser reads PCM and the server carries no codec.
 */

import { toWav } from "./wav";

export class SpeechToText {
    private recorder: MediaRecorder | null = null;
    private chunks: Blob[] = [];
    private stream: MediaStream | null = null;

    constructor(private apiBase = "") {}

    /** Whether the browser can capture audio at all. */
    static isSupported(): boolean {
        return (
            typeof navigator !== "undefined" &&
            !!navigator.mediaDevices?.getUserMedia &&
            typeof MediaRecorder !== "undefined"
        );
    }

    /** True while a recording is in progress. */
    get recording(): boolean {
        return this.recorder?.state === "recording";
    }

    /** Begin capturing microphone audio. Rejects if permission is denied. */
    async start(): Promise<void> {
        if (this.recording) {
            return;
        }
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        this.chunks = [];
        this.recorder = new MediaRecorder(this.stream);
        this.recorder.ondataavailable = e => {
            if (e.data.size > 0) {
                this.chunks.push(e.data);
            }
        };
        this.recorder.start();
    }

    /** Stop capturing and return the recorded audio (or null if nothing captured). */
    async stop(): Promise<Blob | null> {
        const recorder = this.recorder;
        if (!recorder) {
            return null;
        }
        await new Promise<void>(resolve => {
            recorder.onstop = () => resolve();
            recorder.stop();
        });
        this._releaseStream();
        this.recorder = null;
        const type = recorder.mimeType || "audio/webm";
        return this.chunks.length ? new Blob(this.chunks, { type }) : null;
    }

    /** Stop capturing and transcribe in one step; returns "" if nothing recorded. */
    async stopAndTranscribe(): Promise<string> {
        const blob = await this.stop();
        return blob ? this.transcribe(blob) : "";
    }

    /** POST recorded audio to the recognition endpoint and return the transcript. */
    async transcribe(blob: Blob): Promise<string> {
        // Converted here rather than sent as recorded: the endpoint reads PCM,
        // and decoding WebM server-side would put a codec between the feature
        // and working at all.
        const wav = await toWav(blob);
        const body = new FormData();
        body.append("audio", wav, "speech.wav");
        const res = await fetch(`${this.apiBase}/api/stt`, { method: "POST", body });
        if (!res.ok) {
            throw new Error(`STT failed (${res.status})`);
        }
        const data = (await res.json()) as { text?: string };
        return data.text ?? "";
    }

    /** Abort any in-progress recording and release the microphone. */
    cancel(): void {
        if (this.recorder && this.recorder.state !== "inactive") {
            this.recorder.stop();
        }
        this._releaseStream();
        this.recorder = null;
        this.chunks = [];
    }

    private _releaseStream(): void {
        this.stream?.getTracks().forEach(t => t.stop());
        this.stream = null;
    }
}
