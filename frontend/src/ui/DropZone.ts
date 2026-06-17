/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Global full-page drag-and-drop file upload overlay.
 *
 * Dragging files anywhere over the window reveals a centred drop zone; dropping
 * validates type/size, uploads each file and emits an `af-attachment-added`
 * event the chat input picks up as a pending attachment chip.
 */
import { toast } from "./ToastManager";
import { isAccepted, humanSize, MAX_BYTES, uploadFile } from "./dashboard/uploads";

export class DropZone {
    private overlay: HTMLElement;
    private list: HTMLElement;
    /** dragenter/leave fire per element; count depth so leave only hides at 0. */
    private dragDepth = 0;

    constructor(private apiBase = "") {
        this.overlay = this._buildOverlay();
        this.list = this.overlay.querySelector(".af-drop-list") as HTMLElement;
        document.body.appendChild(this.overlay);
        this._wireDragEvents();
    }

    private _buildOverlay(): HTMLElement {
        const overlay = document.createElement("div");
        overlay.className = "af-drop-overlay";
        overlay.innerHTML = `
      <div class="af-drop-card">
        <div class="af-drop-icon">⬆</div>
        <div class="af-drop-title">Drop files to upload</div>
        <div class="af-drop-sub">images · audio · pdf · ppt · txt · up to ${humanSize(MAX_BYTES)} each</div>
        <div class="af-drop-list"></div>
      </div>`;
        return overlay;
    }

    private _wireDragEvents(): void {
        // Prevent the browser from navigating to dropped files everywhere.
        window.addEventListener("dragover", e => e.preventDefault());
        window.addEventListener("drop", e => e.preventDefault());

        window.addEventListener("dragenter", e => {
            if (!this._hasFiles(e)) {
                return;
            }
            this.dragDepth++;
            this._show();
        });
        window.addEventListener("dragleave", () => {
            this.dragDepth = Math.max(0, this.dragDepth - 1);
            if (this.dragDepth === 0) {
                this._hide();
            }
        });
        this.overlay.addEventListener("dragover", e => e.preventDefault());
        this.overlay.addEventListener("drop", e => this._onDrop(e));
    }

    private _hasFiles(e: DragEvent): boolean {
        return Array.from(e.dataTransfer?.types ?? []).includes("Files");
    }

    private _show(): void {
        this.overlay.classList.add("open");
    }

    private _hide(): void {
        this.overlay.classList.remove("open");
        this.list.innerHTML = "";
    }

    private _onDrop(e: DragEvent): void {
        e.preventDefault();
        this.dragDepth = 0;
        const files = Array.from(e.dataTransfer?.files ?? []);
        const accepted = this._partition(files);
        if (accepted.length) {
            void this.uploadFiles(accepted);
        }
        this._hide();
    }

    /** Split into accepted files; toast a summary of anything rejected. */
    private _partition(files: File[]): File[] {
        const accepted: File[] = [];
        const rejected: string[] = [];
        for (const file of files) {
            if (!isAccepted(file)) {
                rejected.push(`${file.name} (unsupported type)`);
            } else if (file.size > MAX_BYTES) {
                rejected.push(`${file.name} (over ${humanSize(MAX_BYTES)})`);
            } else {
                accepted.push(file);
            }
        }
        if (rejected.length) {
            toast.show({
                type: "alert-warning",
                title: "Some files skipped",
                message: rejected.join(", "),
            });
        }
        return accepted;
    }

    /** Upload accepted files; emit each as a pending attachment for the chat. */
    async uploadFiles(files: File[]): Promise<void> {
        for (const file of files) {
            try {
                const attachment = await uploadFile(file, this.apiBase);
                document.dispatchEvent(new CustomEvent("af-attachment-added", { detail: { attachment } }));
            } catch (err) {
                toast.show({ type: "alert-error", title: "Upload failed", message: String(err) });
            }
        }
    }
}
