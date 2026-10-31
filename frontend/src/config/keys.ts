/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The storage keys of what `/api/config` seeds and the views read, kept here so
 * neither has to import the other.
 */

/** Where the seeded uploads capability is kept. */
export const UPLOADS_KEY = "wactorz-uploads-enabled";

/** Where the seeded "there is a session to end" flag is kept. */
export const SIGN_OUT_KEY = "wactorz-can-sign-out";
