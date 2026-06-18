/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * DesktopNotify — no-op stubs.
 *
 * Native desktop notifications are not wired on this branch. These exports are
 * kept so callers (main.ts) don't need to change; they do nothing.
 */

export function initNotifications(): void {}

export function desktopNotifyBackground(_title: string, _body: string): void {}

export function clearUnreadBadge(): void {}
