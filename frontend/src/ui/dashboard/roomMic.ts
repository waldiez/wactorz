/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The chat box's microphone on the `host` branch: the room's, not this browser's.
 *
 * A wake word starts a turn when nobody is at a screen. Somebody who is should
 * not have to say it: this asks the machine's microphone to listen once and
 * puts what it heard in the composer, as every other microphone here does, so
 * it is read and corrected before it is sent rather than acted on as heard.
 * While it listens the button stays lit and cannot be pressed again: the
 * server decides when the speaker has finished.
 */

import { listenInTheRoom } from "../../ext/stt";
import { iconButton } from "../dom";
import { toast } from "../ToastManager";
import { iconMarkup } from "./icons";

const IDLE = "Ask the room to listen";

/** Put what the room heard in the composer, or say why there is nothing to put there. */
async function listenOnce(btn: HTMLButtonElement, input: HTMLTextAreaElement): Promise<void> {
    btn.disabled = true;
    btn.classList.add("recording");
    btn.setAttribute("aria-label", "Listening in the room…");
    btn.title = "Listening in the room…";
    try {
        const answer = await listenInTheRoom(window.__WACTORZ_INGRESS_PATH ?? "", false);
        if (answer.kind === "heard") {
            // Added to what is already written, as the other microphones add it.
            input.value = input.value ? `${input.value} ${answer.text}` : answer.text;
            input.dispatchEvent(new Event("input"));
            input.focus();
        } else if (answer.kind === "speaking") {
            // Not silence: it declines while the room is being spoken to, so
            // that it does not hear its own reply and answer it.
            toast.show({ type: "alert-warning", title: "Still speaking", message: "Try again in a moment." });
        } else if (answer.kind === "silence") {
            toast.show({
                type: "system",
                title: "Nothing heard",
                message: "The microphone opened, but nobody spoke.",
            });
        } else if (answer.kind === "failed") {
            toast.show({ type: "alert-error", title: "Could not listen", message: answer.reason });
        }
    } finally {
        btn.disabled = false;
        btn.classList.remove("recording");
        btn.setAttribute("aria-label", IDLE);
        btn.title = IDLE;
    }
}

/** A microphone button that asks the room to listen, writing what it hears into `input`. */
export function buildRoomMicBtn(input: HTMLTextAreaElement): HTMLButtonElement {
    const btn = iconButton("af-mic-btn", IDLE, iconMarkup("mic", 16));
    btn.addEventListener("click", () => void listenOnce(btn, input));
    return btn;
}
