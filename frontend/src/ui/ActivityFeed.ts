/**
 * Activity feed — collapsible right panel showing all MQTT events.
 *
 * - Collapsed by default; toggle with #feed-toggle button
 * - Badge on toggle button shows count when collapsed
 * - Colour-coded rows: spawn=green, error=red, warning=amber, chat=cyan, stopped=dim
 * - Max 200 items; auto-scrolls; pauses on hover
 */

export type FeedEventType =
  | "spawn"
  | "heartbeat"
  | "chat"
  | "alert-error"
  | "alert-warning"
  | "stopped"
  | "health"
  | "qa-flag";

export interface FeedItem {
  type: FeedEventType;
  label: string;
  agentName: string;
  timestamp: number;
}

const MAX_ITEMS = 200;

const TYPE_COLORS: Record<FeedEventType, string> = {
  spawn: "#34d399",
  heartbeat: "#6aabff",
  chat: "#22d3ee",
  "alert-error": "#fb7185",
  "alert-warning": "#fbbf24",
  stopped: "#5a6a8a",
  health: "#a0a0c0",
  "qa-flag": "#c084fc", // violet — QA flags stand out
};

export class ActivityFeed {
  private panel: HTMLElement;
  private list: HTMLElement;
  private toggleBtn: HTMLButtonElement;
  private badge: HTMLElement;

  private items: FeedItem[] = [];
  private isOpen = false;
  private isPaused = false;
  private unseenCount = 0;

  constructor() {
    this.panel = document.getElementById("activity-feed")!;
    this.list = document.getElementById("feed-list")!;
    this.toggleBtn = document.getElementById(
      "feed-toggle",
    ) as HTMLButtonElement;
    this.badge = document.getElementById("feed-badge")!;

    this.toggleBtn.addEventListener("click", () => this.toggle());
    this.list.addEventListener("mouseenter", () => {
      this.isPaused = true;
    });
    this.list.addEventListener("mouseleave", () => {
      this.isPaused = false;
    });
  }

  /** Push a new event into the feed. */
  push(item: FeedItem): void {
    this.items.push(item);
    if (this.items.length > MAX_ITEMS) {
      this.items.shift();
      this.list.firstElementChild?.remove();
    }

    this.renderItem(item);

    if (!this.isOpen) {
      this.unseenCount++;
      this.updateBadge();
    } else if (!this.isPaused) {
      this.list.scrollTop = this.list.scrollHeight;
    }
  }

  // ── Private ─────────────────────────────────────────────────────────────────

  private toggle(): void {
    this.isOpen = !this.isOpen;
    this.panel.classList.toggle("open", this.isOpen);
    this.toggleBtn.classList.toggle("active", this.isOpen);

    if (this.isOpen) {
      this.unseenCount = 0;
      this.updateBadge();
      this.list.scrollTop = this.list.scrollHeight;
    }
  }

  private renderItem(item: FeedItem): void {
    const row = document.createElement("div");
    row.className = "feed-row";

    const dot = document.createElement("span");
    dot.className = "feed-dot";
    dot.style.background = TYPE_COLORS[item.type] ?? "#6aabff";

    const text = document.createElement("span");
    text.className = "feed-text";
    text.textContent = `${item.agentName}: ${item.label}`;

    const time = document.createElement("span");
    time.className = "feed-time";
    time.textContent = new Date(item.timestamp).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });

    row.appendChild(dot);
    row.appendChild(text);
    row.appendChild(time);
    this.list.appendChild(row);
  }

  private updateBadge(): void {
    if (this.unseenCount > 0 && !this.isOpen) {
      this.badge.textContent = String(
        this.unseenCount > 99 ? "99+" : this.unseenCount,
      );
      this.badge.style.display = "inline-flex";
    } else {
      this.badge.style.display = "none";
    }
  }
}
