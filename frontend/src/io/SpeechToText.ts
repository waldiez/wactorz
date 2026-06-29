/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Backend-backed speech-to-text.
 *
 * Records microphone audio with MediaRecorder + getUserMedia (supported
 * wherever audio capture is allowed) and POSTs it to the server's STT endpoint,
 * so the actual recognition happens server-side.
 */

/**
 * Whether the `/api/stt` backend endpoint is available. Off by default; enable
 * per-deploy at build time with `VITE_STT_ENABLED=true` once the endpoint is
 * live. While off, the mic button is not rendered at all.
 */
export const STT_ENABLED = import.meta.env["VITE_STT_ENABLED"] === "true";

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

    /** POST recorded audio to the backend STT endpoint for transcription. */
    async transcribe(blob: Blob): Promise<string> {
        const body = new FormData();
        body.append("audio", blob, "speech.webm");
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
