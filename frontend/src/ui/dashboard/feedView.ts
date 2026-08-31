/**
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2025 - 2026 Waldiez & contributors
 */
/**
 * The dashboard's activity view: a filter toolbar plus a scrolling list of
 * rows drawn from two sources — agent activity and the application log.
 *
 * Rows for the two read differently on purpose. An agent event is *typed*
 * (spawn, chat, alert) and carries an actor; a log record is *levelled* and
 * carries a logger name. Type keeps its icon, level gets a coloured left border,
 * and the log's text is monospace — so the two are distinguishable without a
 * badge on every row.
 *
 * Every string rendered here is untrusted: agent output, model output and
 * third-party log text all land in these rows. Text goes in through
 * `textContent` or a text node, never `innerHTML` — the only `innerHTML` is
 * first-party icon markup from `iconMarkup`. Class names come from the lookup
 * tables below rather than from interpolated payload fields.
 */
import type { ActivityItem, AppLogItem, FeedItem, LogLevel } from "../../types/feed";
import { isAppLog } from "../../types/feed";
import { SYSTEM_AGENT_NAMES } from "./agentState";
import { nameFromWid, displayName } from "../../agents/naming";
import { iconMarkup, type IconName } from "./icons";
import { timeLabel } from "../../time";

const TYPE_CLASS: Record<string, string> = {
    spawn: "af-feed-spawn",
    heartbeat: "af-feed-heartbeat",
    chat: "af-feed-chat",
    "alert-error": "af-feed-alert",
    "alert-warning": "af-feed-alert",
    health: "af-feed-health",
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

/** Severity order, lowest first — the level filter is "this and above". */
export const LEVEL_ORDER: LogLevel[] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

/**
 * Level → border class. A lookup, not `af-feed-lvl-${level}`: `level` arrives
 * in a payload, and interpolating it into a class name lets a crafted record
 * name any class in the stylesheet.
 */
const LEVEL_CLASS: Record<string, string> = {
    DEBUG: "af-feed-lvl-debug",
    INFO: "af-feed-lvl-info",
    WARNING: "af-feed-lvl-warning",
    ERROR: "af-feed-lvl-error",
    CRITICAL: "af-feed-lvl-critical",
};

/** How much of a row is shown before it needs expanding. */
const TRUNCATE_AT = 120;

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

/** The message span: the `@agent` mention (if any) as a styled token, then the
 *  body text, truncated at 120 chars with the full text kept as a tooltip. */
function buildTextSpan(item: FeedItem): HTMLElement {
    const text = document.createElement("span");
    text.className = "af-feed-text";
    const { mention, body } = splitMention(item);
    if (mention) {
        const men = document.createElement("span");
        men.className = "af-feed-mention";
        men.textContent = mention;
        text.append(men, document.createTextNode(" "));
    }
    const shown = body.length > TRUNCATE_AT ? body.slice(0, TRUNCATE_AT) + "…" : body;
    text.appendChild(document.createTextNode(shown));
    if (body.length > TRUNCATE_AT) {
        text.title = body;
    }
    return text;
}

function timeSpan(ms: number): HTMLElement {
    const time = document.createElement("span");
    time.className = "af-feed-time";
    time.textContent = timeLabel(ms, { seconds: true });
    return time;
}

/**
 * The tail of a logger name — `wactorz.agents.installer` reads as
 * `agents.installer`. The prefix is the same on nearly every row and crowds out
 * the part that differs; the whole name stays in `title`.
 */
export function shortOrigin(origin: string): string {
    const parts = origin.split(".");
    return parts.length <= 2 ? origin : parts.slice(-2).join(".");
}

/** Append a single agent-activity row to `container`. */
export function feedItemEl(container: HTMLElement, item: FeedItem): void {
    const row = document.createElement("div");
    row.className = `af-feed-item ${TYPE_CLASS[item.type] ?? ""}`.trim();
    row.dataset["source"] = "agent";
    // Recorded rather than read back from the class: a class says how a row
    // looks, and more than one kind of row can be made to look the same.
    row.dataset["type"] = item.type;

    const icon = document.createElement("span");
    icon.className = "af-feed-icon";
    const iconName = TYPE_ICON[item.type];
    icon.innerHTML = iconName ? iconMarkup(iconName, 14) : "·";

    row.append(icon, timeSpan(item.timestamp), buildAgentSpan(item), buildTextSpan(item));
    container.appendChild(row);
}

/**
 * Append a single application-log row.
 *
 * A traceback is the reason someone opened this view, and it is not a 120-char
 * label — so the row stays one line and reveals the whole record in place when
 * clicked, rather than truncating it away or growing the list unpredictably.
 */
export function appLogItemEl(container: HTMLElement, item: AppLogItem): void {
    const row = document.createElement("div");
    row.className = `af-feed-item af-feed-app ${LEVEL_CLASS[item.level] ?? ""}`.trim();
    row.dataset["source"] = "app";
    row.dataset["level"] = item.level;

    const icon = document.createElement("span");
    icon.className = "af-feed-icon";
    icon.textContent = "·";

    const origin = document.createElement("span");
    origin.className = "af-feed-agent";
    origin.textContent = shortOrigin(item.origin);
    origin.title = item.origin;

    const firstLine = item.text.split("\n", 1)[0] ?? "";
    const expandable = item.text.includes("\n") || firstLine.length > TRUNCATE_AT;

    const text = document.createElement("span");
    text.className = "af-feed-text af-feed-log-text";
    text.textContent = firstLine.length > TRUNCATE_AT ? firstLine.slice(0, TRUNCATE_AT) + "…" : firstLine;

    row.append(icon, timeSpan(item.ts * 1000), origin, text);

    if (expandable) {
        attachExpander(row, item.text);
    }
    container.appendChild(row);
}

/**
 * Make a row reveal its whole record in place.
 *
 * `textContent`, never `innerHTML`. This is the one place the *entire*
 * untrusted record is rendered, and a traceback is exactly the string an agent
 * can be induced to emit — the reach for `innerHTML` here is stored XSS in the
 * feature whose purpose is displaying attacker-influenceable strings.
 */
function attachExpander(row: HTMLElement, fullText: string): void {
    row.classList.add("af-feed-expandable");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-expanded", "false");

    const full = document.createElement("pre");
    full.className = "af-feed-full";
    full.textContent = fullText;
    full.hidden = true;
    row.appendChild(full);

    const toggle = () => {
        full.hidden = !full.hidden;
        row.setAttribute("aria-expanded", String(!full.hidden));
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggle();
        }
    });
}

/** Append any activity entry, dispatching on its source. */
export function activityItemEl(container: HTMLElement, item: ActivityItem): void {
    if (isAppLog(item)) {
        appLogItemEl(container, item);
    } else {
        feedItemEl(container, item);
    }
}

/**
 * When an entry happened, in milliseconds.
 *
 * The two sources disagree on units as well as field name: agent events carry
 * `timestamp` in milliseconds, the log buffer records `ts` in seconds, as
 * Python's `time.time()` returns it. Sorting them together without converting
 * puts every log row in 1970.
 */
export function entryTimeMs(item: ActivityItem): number {
    return isAppLog(item) ? item.ts * 1000 : item.timestamp;
}

/** Identity key for a feed entry — entries carry no id, so dedup on content. */
export function feedKey(item: ActivityItem): string {
    if (isAppLog(item)) {
        // Source-prefixed: an agent row and a log row could otherwise collide on
        // identical text at the same instant, and the survivor would be whichever
        // arrived first.
        return `app|${item.ts}|${item.level}|${item.origin}|${item.text}`;
    }
    return `agent|${item.timestamp}|${item.type}|${item.agentName}|${item.label}`;
}

/**
 * Entries arrive from several unsynchronised sources (SQLite seed, WS
 * log_feed replay, live MQTT/chat, and now the application log) in arrival
 * order, not chronological order, and without ids. Drop exact-duplicate
 * entries and sort by time so the rendered feed is stable and chronological
 * regardless of arrival order.
 */
export function dedupeAndSortFeed<T extends ActivityItem>(items: T[]): T[] {
    const seen = new Set<string>();
    const unique: T[] = [];
    for (const item of items) {
        const key = feedKey(item);
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        unique.push(item);
    }
    return unique.sort((a, b) => entryTimeMs(a) - entryTimeMs(b));
}

/** Rows an agent-activity entry never earns: system chatter, and heartbeats
 *  while they are switched off. Applied before render, unlike the toolbar
 *  filters, because these never become visible by changing a control. */
function isHidden(item: ActivityItem, hideHeartbeats: boolean): boolean {
    if (isAppLog(item)) {
        return false;
    }
    if (hideHeartbeats && item.type === "heartbeat") {
        return true;
    }
    return SYSTEM_AGENT_NAMES.has(nameFromWid(item.agentName));
}

/** Which rows the toolbar is currently letting through. */
export interface FeedFilters {
    /** Which sources to show. Defaults to `agent`, so the panel keeps the
     *  character it had before the application log joined it. */
    source: "all" | "agent" | "app";
    /** Minimum severity for application-log rows; agent rows are unaffected. */
    level: LogLevel;
    /** Case-insensitive substring over the row's visible text. */
    search: string;
    hideHeartbeats: boolean;
}

export const DEFAULT_FILTERS: FeedFilters = {
    source: "agent",
    level: "INFO",
    search: "",
    hideHeartbeats: true,
};

export interface FeedViewOptions {
    filters: FeedFilters;
    onFiltersChange: (filters: FeedFilters) => void;
    /** Fetch the application log again now. */
    onRefresh?: () => void;
    /** Start or stop re-fetching on an interval. */
    onFollowChange?: (following: boolean) => void;
    /** Whether following is on, so the control survives a view rebuild. */
    following?: boolean;
}

/** Whether one already-rendered row survives the current filters. */
function rowMatches(row: HTMLElement, filters: FeedFilters): boolean {
    const source = row.dataset["source"] === "app" ? "app" : "agent";
    if (filters.source !== "all" && filters.source !== source) {
        return false;
    }
    if (source === "app") {
        const level = row.dataset["level"] ?? "INFO";
        const min = LEVEL_ORDER.indexOf(filters.level);
        const own = LEVEL_ORDER.indexOf(level as LogLevel);
        // An unrecognised level is shown rather than hidden: a row nobody can
        // classify is exactly the one worth seeing.
        if (own !== -1 && min !== -1 && own < min) {
            return false;
        }
    } else if (filters.hideHeartbeats && row.dataset["type"] === "heartbeat") {
        return false;
    }
    const needle = filters.search.trim().toLowerCase();
    return !needle || (row.textContent ?? "").toLowerCase().includes(needle);
}

/**
 * Apply the filters to rows already in the DOM.
 *
 * In place rather than by re-rendering, so scroll position survives typing in
 * the search box — and so the expanded state of a row is not thrown away by an
 * unrelated keystroke.
 */
/** Show or hide one row. The whole-list pass is for a filter change; a single
 *  append must not pay for every row already on screen. */
function applyFilterToRow(row: HTMLElement, filters: FeedFilters): boolean {
    const visible = rowMatches(row, filters);
    row.hidden = !visible;
    return visible;
}

export function applyFilters(feed: HTMLElement, filters: FeedFilters): void {
    let shown = 0;
    feed.querySelectorAll<HTMLElement>(".af-feed-item").forEach(row => {
        if (applyFilterToRow(row, filters)) {
            shown += 1;
        }
    });
    const empty = feed.querySelector<HTMLElement>(".af-feed-empty");
    if (empty) {
        empty.hidden = shown > 0;
    }
}

function labelled(text: string, control: HTMLElement): HTMLElement {
    const wrap = document.createElement("label");
    wrap.className = "af-feed-filter";
    const span = document.createElement("span");
    span.textContent = text;
    wrap.append(span, control);
    return wrap;
}

function option(value: string, text: string): HTMLOptionElement {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = text;
    return opt;
}

/** A `<select>` bound to one filter field, rebuilt into `filters` on change. */
function filterSelect(
    values: readonly string[],
    current: string,
    label: (v: string) => string,
    onPick: (value: string) => void,
): HTMLSelectElement {
    const select = document.createElement("select");
    select.className = "af-feed-select";
    values.forEach(v => select.appendChild(option(v, label(v))));
    select.value = current;
    select.addEventListener("change", () => onPick(select.value));
    return select;
}

/** The heartbeat toggle — a level filter in disguise, kept as its own control
 *  because that is the shape people already know it by. */
function heartbeatButton(filters: FeedFilters, changed: () => void): HTMLButtonElement {
    const btn = document.createElement("button");
    const paint = () => {
        btn.innerHTML = `${iconMarkup("heart", 12)} heartbeats: ${filters.hideHeartbeats ? "off" : "on"}`;
        btn.className = `af-mini-btn${filters.hideHeartbeats ? "" : " active"}`;
    };
    btn.style.cssText = "font-size:11px;padding:3px 10px;";
    btn.title = "Toggle heartbeat events";
    paint();
    btn.addEventListener("click", () => {
        filters.hideHeartbeats = !filters.hideHeartbeats;
        paint();
        changed();
    });
    return btn;
}

/**
 * Grey out the controls the current source has no use for.
 *
 * Agent events are typed, not levelled, and log records have no heartbeats —
 * so with one source selected, one of the two controls governs nothing. Left
 * live they invite the reasonable conclusion that the filter is broken.
 * Disabled rather than hidden, so the toolbar keeps its shape as the source
 * changes and the control stays where the eye last saw it.
 */
function syncControlRelevance(
    filters: FeedFilters,
    level: HTMLSelectElement,
    heartbeats: HTMLButtonElement,
): void {
    const levelApplies = filters.source !== "agent";
    const heartbeatsApply = filters.source !== "app";
    level.disabled = !levelApplies;
    heartbeats.disabled = !heartbeatsApply;
    level.title = levelApplies ? "" : "Agent activity has no severity level";
    heartbeats.title = heartbeatsApply ? "Toggle heartbeat events" : "The application log has no heartbeats";
    level.closest(".af-feed-filter")?.classList.toggle("af-feed-filter-off", !levelApplies);
    heartbeats.classList.toggle("af-feed-filter-off", !heartbeatsApply);
}

/**
 * Refresh and follow — the application log's two actions.
 *
 * Follow re-fetches on an interval rather than opening a stream: it reaches the
 * same place over the path that is already a pull, so a page left open receives
 * nothing it did not ask for. When a live push does land it replaces the timer
 * behind this same control, which is why it is named for what the user wants
 * rather than for how it currently works.
 */
function refreshButton(onRefresh: () => void): HTMLButtonElement {
    const refresh = document.createElement("button");
    refresh.className = "af-mini-btn af-feed-refresh";
    refresh.type = "button";
    refresh.title = "Fetch the application log again";
    refresh.textContent = "refresh";
    refresh.addEventListener("click", () => onRefresh());
    return refresh;
}

function followButton(starting: boolean, onChange: (following: boolean) => void): HTMLButtonElement {
    let following = starting;
    const follow = document.createElement("button");
    follow.type = "button";
    follow.title = "Keep fetching new records";
    const paint = () => {
        follow.textContent = `follow: ${following ? "on" : "off"}`;
        follow.className = `af-mini-btn af-feed-follow${following ? " active" : ""}`;
    };
    paint();
    follow.addEventListener("click", () => {
        following = !following;
        paint();
        onChange(following);
    });
    return follow;
}

function logActions(opts: FeedViewOptions): HTMLElement[] {
    const controls: HTMLElement[] = [];
    if (opts.onRefresh) {
        controls.push(refreshButton(opts.onRefresh));
    }
    if (opts.onFollowChange) {
        controls.push(followButton(opts.following ?? false, opts.onFollowChange));
    }
    return controls;
}

/** Source / level / search / heartbeats — the whole toolbar. */
function buildToolbar(feed: HTMLElement, opts: FeedViewOptions): HTMLElement {
    const filters: FeedFilters = { ...opts.filters };
    const level = filterSelect(
        LEVEL_ORDER,
        filters.level,
        v => v.toLowerCase(),
        v => {
            filters.level = v as LogLevel;
            changed();
        },
    );
    const heartbeats = heartbeatButton(filters, () => changed());
    function changed(): void {
        syncControlRelevance(filters, level, heartbeats);
        applyFilters(feed, filters);
        opts.onFiltersChange({ ...filters });
    }

    const source = filterSelect(
        ["agent", "app", "all"],
        filters.source,
        v => ({ agent: "agents", app: "logs", all: "everything" })[v] ?? v,
        v => {
            filters.source = v as FeedFilters["source"];
            changed();
        },
    );

    const search = document.createElement("input");
    search.className = "af-feed-search";
    search.type = "search";
    search.placeholder = "search…";
    search.value = filters.search;
    search.addEventListener("input", () => {
        filters.search = search.value;
        changed();
    });

    const toolbar = document.createElement("div");
    toolbar.className = "af-feed-toolbar";
    toolbar.append(
        labelled("show", source),
        labelled("level", level),
        labelled("find", search),
        heartbeats,
        ...logActions(opts),
    );
    syncControlRelevance(filters, level, heartbeats);
    return toolbar;
}

function populateFeed(feed: HTMLElement, items: ActivityItem[], filters: FeedFilters): void {
    const rows = dedupeAndSortFeed(items).filter(i => !isHidden(i, filters.hideHeartbeats));
    const empty = document.createElement("div");
    empty.className = "af-feed-empty";
    empty.textContent = "No events yet.";
    empty.hidden = rows.length > 0;
    feed.appendChild(empty);
    rows.forEach(item => activityItemEl(feed, item));
    setRowCount(feed, rows.length);
    applyFilters(feed, filters);
}

/** Build the activity view (toolbar + list) for the given entries. */
export function buildFeedView(items: ActivityItem[], opts: FeedViewOptions): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "af-feed-wrap";

    const feed = document.createElement("div");
    feed.className = "af-feed";
    feed.id = "af-feed-view";
    // Append-only activity log — announce new rows to screen readers as they arrive.
    feed.setAttribute("role", "log");
    feed.setAttribute("aria-live", "polite");

    wrap.appendChild(buildToolbar(feed, opts));
    populateFeed(feed, items, opts.filters);
    wrap.appendChild(feed);
    setTimeout(() => {
        feed.scrollTop = feed.scrollHeight;
    }, 0);
    return wrap;
}

/**
 * How many rows the list may hold. Beyond this the oldest are dropped.
 *
 * Nothing used to remove a row. `feedItems` is capped, but that only bounds
 * what a *rebuild* renders — appending never evicted, so a long session on a
 * busy broker grew the DOM without limit. Polling the application log makes
 * that faster and pushing it would make it faster still, so the bound belongs
 * on the append path itself rather than on any one source.
 */
export const MAX_ROWS = 600;

/** How many rows the list holds, cached on the element.
 *
 * Counted rather than queried. `querySelectorAll` on every append made the
 * append path O(n), and with the full re-filter below that was O(n²) over a
 * session — slow enough to time a test out at 600 rows, and the same cost in
 * the browser while `follow` is on. */
function rowCount(feed: HTMLElement): number {
    const cached = feed.dataset["rows"];
    return cached === undefined ? feed.querySelectorAll(".af-feed-item").length : Number(cached);
}

function setRowCount(feed: HTMLElement, count: number): void {
    feed.dataset["rows"] = String(Math.max(0, count));
}

/** Drop the oldest rows once the list is over `MAX_ROWS`. */
function evictOldestRows(feed: HTMLElement): void {
    const count = rowCount(feed);
    for (let over = count - MAX_ROWS; over > 0; over--) {
        feed.querySelector(".af-feed-item")?.remove();
    }
    setRowCount(feed, Math.min(count, MAX_ROWS));
}

/** Append a newly-arrived entry to the live view inside `root`, if shown. */
export function appendFeedItemToView(root: HTMLElement, item: ActivityItem, filters: FeedFilters): void {
    const feed = root.querySelector<HTMLElement>("#af-feed-view");
    if (!feed || isHidden(item, filters.hideHeartbeats)) {
        return;
    }
    // Counted before the append: with no cache yet, `rowCount` falls back to
    // querying the DOM, and reading it afterwards would count the new row and
    // then add it again — leaving the cache one ahead for the rest of the
    // session, so eviction trims a row early.
    const before = rowCount(feed);
    activityItemEl(feed, item);
    setRowCount(feed, before + 1);
    const row = feed.lastElementChild as HTMLElement | null;
    if (row && applyFilterToRow(row, filters)) {
        // Only a visible row can retire the placeholder; a hidden one leaves
        // whatever the last full pass decided.
        const empty = feed.querySelector<HTMLElement>(".af-feed-empty");
        if (empty) {
            empty.hidden = true;
        }
    }
    evictOldestRows(feed);
    feed.scrollTop = feed.scrollHeight;
}
