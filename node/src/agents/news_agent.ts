/**
 * NewsAgent — HackerNews headlines on demand (Node.js port).
 * No API key required. agentType: "data"
 */

import axios from "axios";
import { Actor, MqttPublisher } from "../core/actor";
import { Message, MessageType } from "../core/types";

const HN_API       = "https://hacker-news.firebaseio.com/v0";
const DEFAULT_COUNT = 5;
const MAX_COUNT     = 20;

const FEED_LABELS: Record<string, string> = {
  top:  "Top",
  new:  "Newest",
  best: "Best",
  ask:  "Ask HN",
  show: "Show HN",
  job:  "Jobs",
};

const HELP = `**NewsAgent** — headlines via Hacker News (no API key needed)

\`\`\`
@news-agent              # top 5 stories
@news-agent 10           # top 10 stories
@news-agent new          # newest
@news-agent best         # all-time best
@news-agent ask          # Ask HN
@news-agent show         # Show HN
@news-agent jobs         # job postings
@news-agent help         # this message
\`\`\``;

export class NewsAgent extends Actor {
  constructor(publish: MqttPublisher, actorId?: string) {
    super("news-agent", publish, actorId);
  }

  protected override async onStart(): Promise<void> {
    this.publishSpawn("data");
  }

  override async handleMessage(msg: Message): Promise<void> {
    let text = Actor.extractText(msg.payload).trim();
    text = Actor.stripPrefix(text, "@news-agent", "@news_agent");
    const parts = text.toLowerCase().trim().split(/\s+/);
    const first = parts[0] ?? "";

    let feed = "top";
    let count = DEFAULT_COUNT;

    if (first === "" || first === "top") {
      feed  = "top";
      count = parts[1] && /^\d+$/.test(parts[1]) ? parseInt(parts[1]) : DEFAULT_COUNT;
    } else if (first === "new" || first === "best" || first === "ask" || first === "show") {
      feed = first;
    } else if (first === "jobs" || first === "job") {
      feed = "job";
    } else if (/^\d+$/.test(first)) {
      feed  = "top";
      count = parseInt(first);
    } else if (first === "help") {
      this.replyChat(HELP);
      return;
    }

    count = Math.min(count, MAX_COUNT);
    const label = FEED_LABELS[feed] ?? feed;
    this.replyChat(`📰 Fetching top ${count} ${label} stories from Hacker News...`);

    try {
      const report = await this._fetchHN(feed, count);
      this.replyChat(report);
    } catch (e) {
      this.replyChat(`⚠ Could not fetch news: ${e}`);
    }
    this.metrics.tasksCompleted++;
  }

  private async _fetchHN(feed: string, count: number): Promise<string> {
    const idsUrl = `${HN_API}/${feed}stories.json`;
    const idsResp = await axios.get<number[]>(idsUrl, {
      timeout: 12_000,
      headers: { "User-Agent": "Wactorz-NewsAgent/1.0" },
    });
    const ids = idsResp.data.slice(0, Math.min(count, MAX_COUNT));

    const items = await Promise.all(
      ids.map((id) =>
        axios
          .get<Record<string, unknown>>(`${HN_API}/item/${id}.json`, { timeout: 8_000 })
          .then((r) => r.data)
          .catch(() => null)
      )
    );

    const label = FEED_LABELS[feed] ?? feed;
    const lines: string[] = [];
    let i = 1;
    for (const item of items) {
      if (!item) continue;
      const title  = String(item["title"] ?? "(no title)");
      const url    = item["url"] ? String(item["url"]) : "";
      const score  = Number(item["score"] ?? 0);
      const itemId = item["id"];
      const hnUrl  = `https://news.ycombinator.com/item?id=${itemId}`;
      const link   = url || hnUrl;
      lines.push(`${i}. **[${title}](${link})** — ⬆ ${score} · [HN](${hnUrl})`);
      i++;
    }

    if (lines.length === 0) return `No ${label} stories found right now.`;
    return (
      `**Hacker News — ${label} Stories** (top ${ids.length})\n\n` +
      lines.join("\n") +
      "\n\n*Source: [news.ycombinator.com](https://news.ycombinator.com)*"
    );
  }
}
