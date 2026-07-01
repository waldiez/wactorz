/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Ambient global augmentations.
 *
 * `__WACTORZ_INGRESS_PATH` is injected on `window` by the Home Assistant add-on
 * (the ingress base path the dashboard is served under); it is absent when the
 * dashboard runs standalone, hence optional — callers default it to "".
 */
declare global {
    interface Window {
        __WACTORZ_INGRESS_PATH?: string;
    }
}

export {};
