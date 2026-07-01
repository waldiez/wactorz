/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard's activity-feed view: a heartbeat toggle plus a scrolling list
 * of feed rows. System-agent chatter and (optionally) heartbeats are filtered
 * out. Heartbeat visibility is owned by the caller via `onToggleHeartbeats`.
 */
import type { FeedItem } from "../../types/feed";
import { SYSTEM_AGENT_NAMES } from "./agentState";
import { nameFromWid, displayName } from "../../agents/naming";
import { iconMarkup, type IconName } from "./icons";

const TYPE_CLASS: Record<string, string> = {
    spawn: "af-feed-spawn",
    heartbeat: "af-feed-heartbeat",
    chat: "af-feed-chat",
    "alert-error": "af-feed-alert",
    "alert-warning": "af-feed-alert",
    health: "af-feed-heartbeat",
    "qa-flag": "af-feed-chat",
};

const TYPE_ICON: Record<string, IconName> = {
    spawn: "zap",
    heartbeat: "heart",
    chat: "chat",
    "alert-error": "alertCircle",
    "alert-warning": "alertTriangle",
    stopped: "square",
    health: "activity",
    "qa-flag": "flag",
};

/** Whether a feed row represents the user's own chat turn (vs an agent's).
 *  The backend tags chat_log rows with role; the live io-bar path tags the
 *  agentName "user" directly. Either marks a user turn. */
function isUserTurn(item: FeedItem): boolean {
    return item.role === "user" || item.agentName === "user";
}

/** The sender span: "you" (accented) for the user's own chat turns, otherwise
 *  the resolved agent name. Chat turns are persisted under the agent's name, so
 *  without the role check every line would read as the agent. */
function buildAgentSpan(item: FeedItem): HTMLElement {
    const agent = document.createElement("span");
    agent.className = "af-feed-agent";
    if (isUserTurn(item)) {
        agent.textContent = "you";
        agent.classList.add("af-feed-agent-user");
    } else {
        agent.textContent = displayName(item.agentName);
    }
    return agent;
}

/** Split a feed label into an optional `@<agent>` routing mention and the body.
 *  IOManager prefixes user messages with the tag; it's kept on the user's own
 *  turns (rendered as a styled token showing who they addressed) and stripped
 *  from agent rows so those read cleanly. */
function splitMention(item: FeedItem): { mention: string | null; body: string } {
    const raw = item.label ?? "";
    if (!isUserTurn(item)) {
        return { mention: null, body: raw.replace(/^@[\w-]+\s+/, "") };
    }
    const m = raw.match(/^(@[\w-]+)\s+([\s\S]*)$/);
    return m ? { mention: m[1]!, body: m[2]! } : { mention: null, body: raw };
}

/** Append a single feed row to `container`. */
export function feedItemEl(container: HTMLElement, item: FeedItem): void {
    const row = document.createElement("div");
    row.className = `af-feed-item ${TYPE_CLASS[item.type] ?? ""}`.trim();

    const icon = document.createElement("span");
    icon.className = "af-feed-icon";
    const iconName = TYPE_ICON[item.type];
    icon.innerHTML = iconName ? iconMarkup(iconName, 14) : "·";

    const time = document.createElement("span");
    time.className = "af-feed-time";
    time.textContent = new Date(item.timestamp).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
    });

    const agent = buildAgentSpan(item);

    const text = document.createElement("span");
    text.className = "af-feed-text";
    const { mention, body } = splitMention(item);
    if (mention) {
        const men = document.createElement("span");
        men.className = "af-feed-mention";
        men.textContent = mention;
        text.append(men, document.createTextNode(" "));
    }
    const shown = body.length > 120 ? body.slice(0, 120) + "…" : body;
    text.appendChild(document.createTextNode(shown));
    if (body.length > 120) {
        text.title = body;
    }

    row.append(icon, time, agent, text);
    container.appendChild(row);
}

function isHidden(item: FeedItem, hideHeartbeats: boolean): boolean {
    if (hideHeartbeats && item.type === "heartbeat") {
        return true;
    }
    return SYSTEM_AGENT_NAMES.has(nameFromWid(item.agentName));
}

/** Identity key for a feed item — feed events carry no id, so dedup on content. */
export function feedKey(item: FeedItem): string {
    return `${item.timestamp}|${item.type}|${item.agentName}|${item.label}`;
}

/**
 * Feed items arrive from several unsynchronised sources (SQLite seed, WS
 * log_feed replay, live MQTT/chat) in arrival order, not chronological order,
 * and without ids. Drop exact-duplicate events and sort by timestamp so the
 * rendered feed is stable and chronological regardless of arrival order.
 */
export function dedupeAndSortFeed(items: FeedItem[]): FeedItem[] {
    const seen = new Set<string>();
    const unique: FeedItem[] = [];
    for (const item of items) {
        const key = feedKey(item);
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        unique.push(item);
    }
    return unique.sort((a, b) => a.timestamp - b.timestamp);
}

export interface FeedViewOptions {
    hideHeartbeats: boolean;
    onToggleHeartbeats: (hidden: boolean) => void;
}

/** The heartbeat on/off toggle; re-filters `feed` and notifies the caller. */
function buildHeartbeatToggle(feed: HTMLElement, opts: FeedViewOptions): HTMLButtonElement {
    let hideHeartbeats = opts.hideHeartbeats;
    const label = () => `${iconMarkup("heart", 12)} heartbeats: ${hideHeartbeats ? "off" : "on"}`;

    const hbBtn = document.createElement("button");
    hbBtn.className = `af-mini-btn${hideHeartbeats ? "" : " active"}`;
    hbBtn.style.cssText = "font-size:11px;padding:3px 10px;";
    hbBtn.title = "Toggle heartbeat events";
    hbBtn.innerHTML = label();
    hbBtn.addEventListener("click", () => {
        hideHeartbeats = !hideHeartbeats;
        opts.onToggleHeartbeats(hideHeartbeats);
        hbBtn.innerHTML = label();
        hbBtn.className = `af-mini-btn${hideHeartbeats ? "" : " active"}`;
        feed.querySelectorAll<HTMLElement>(".af-feed-heartbeat").forEach(el => {
            el.hidden = hideHeartbeats;
        });
    });
    return hbBtn;
}

function populateFeed(feed: HTMLElement, items: FeedItem[], hideHeartbeats: boolean): void {
    const visible = dedupeAndSortFeed(items).filter(i => !isHidden(i, hideHeartbeats));
    if (visible.length === 0) {
        const empty = document.createElement("div");
        empty.className = "af-feed-empty";
        empty.textContent = "No events yet.";
        feed.appendChild(empty);
        return;
    }
    visible.forEach(item => feedItemEl(feed, item));
}

/** Build the feed view (toolbar + list) for the given items. */
export function buildFeedView(items: FeedItem[], opts: FeedViewOptions): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "af-feed-wrap";

    const feed = document.createElement("div");
    feed.className = "af-feed";
    feed.id = "af-feed-view";
    // Append-only activity log — announce new rows to screen readers as they arrive.
    feed.setAttribute("role", "log");
    feed.setAttribute("aria-live", "polite");

    const toolbar = document.createElement("div");
    toolbar.className = "af-feed-toolbar";
    toolbar.appendChild(buildHeartbeatToggle(feed, opts));
    wrap.appendChild(toolbar);

    populateFeed(feed, items, opts.hideHeartbeats);
    wrap.appendChild(feed);
    setTimeout(() => {
        feed.scrollTop = feed.scrollHeight;
    }, 0);
    return wrap;
}

/** Append a newly-arrived feed item to the live feed view inside `root`, if shown. */
export function appendFeedItemToView(root: HTMLElement, item: FeedItem, hideHeartbeats: boolean): void {
    const feed = root.querySelector<HTMLElement>("#af-feed-view");
    if (!feed || isHidden(item, hideHeartbeats)) {
        return;
    }
    feed.querySelector(".af-feed-empty")?.remove();
    feedItemEl(feed, item);
    feed.scrollTop = feed.scrollHeight;
}
