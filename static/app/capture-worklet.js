/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 *
 * The microphone half that runs on the audio thread.
 *
 * A real file rather than a blob: a worklet module is fetched as a script, and
 * the page is served under a policy that allows scripts from its own origin
 * only. Loosening that to admit blobs would widen what any injected string on
 * the page could run, for the sake of one small file.
 *
 * It only gathers and forwards. Converting rates and cutting frames is the same
 * work on either thread, and is left where it is tested.
 */
class CaptureProcessor extends AudioWorkletProcessor {
    constructor(options) {
        super();
        this._block = new Float32Array(options.processorOptions.block);
        this._filled = 0;
    }

    process(inputs) {
        const channel = inputs[0] && inputs[0][0];
        if (!channel) {
            return true;
        }
        for (let i = 0; i < channel.length; i++) {
            this._block[this._filled++] = channel[i];
            if (this._filled === this._block.length) {
                this.port.postMessage(this._block.slice());
                this._filled = 0;
            }
        }
        return true;
    }
}

registerProcessor("wactorz-capture", CaptureProcessor);
