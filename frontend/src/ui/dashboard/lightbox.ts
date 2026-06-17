/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Minimal full-screen image lightbox: click the backdrop or press Escape to
 * close. One instance at a time.
 */
export function openLightbox(url: string, alt = ""): void {
    document.querySelector(".af-lightbox")?.remove();

    const overlay = document.createElement("div");
    overlay.className = "af-lightbox";

    const img = document.createElement("img");
    img.src = url;
    img.alt = alt;
    overlay.appendChild(img);

    const close = (): void => {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
    };
    const onKey = (e: KeyboardEvent): void => {
        if (e.key === "Escape") {
            close();
        }
    };
    overlay.addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
}
