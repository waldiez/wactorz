/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * Tiny logging boundary. `info`/`debug` are gated to dev builds so production
 * consoles stay quiet, while `warn`/`error` always surface (real problems users
 * or support may need to see). Vite replaces `import.meta.env.DEV` at build time.
 */
const DEV: boolean = import.meta.env.DEV;

export const log = {
    /** Verbose progress/diagnostics — dev builds only. */
    info: (...args: unknown[]): void => {
        if (DEV) {
            console.info(...args);
        }
    },
    /** Fine-grained tracing — dev builds only. */
    debug: (...args: unknown[]): void => {
        if (DEV) {
            console.debug(...args);
        }
    },
    /** Recoverable issue worth surfacing in any build. */
    warn: (...args: unknown[]): void => {
        console.warn(...args);
    },
    /** Error worth surfacing in any build. */
    error: (...args: unknown[]): void => {
        console.error(...args);
    },
};
