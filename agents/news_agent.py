"""
NewsAgent — HackerNews headlines on demand. No API key required.

Sources:
  - Hacker News Firebase API (default)
  - Any RSS/Atom feed via NEWS_RSS_URL env var (future)

Usage (via IO bar):
  @news-agent              → top 5 stories
  @news-agent 10           → top 10 stories
  @news-agent top          → same as above
  @news-agent new          → newest HN stories
  @news-agent best         → all-time best
  @news-agent ask          → Ask HN
  @news-agent show         → Show HN
  @news-agent jobs         → job postings
  @news-agent help         → show usage

The agent does NOT poll; it only fetches when it receives a message.
"""

import asyncio
import logging
import time

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)

HN_API            = "https://hacker-news.firebaseio.com/v0"
DEFAULT_COUNT     = 5
MAX_COUNT         = 20
HTTP_TIMEOUT      = 12
USER_AGENT        = "AgentFlow-NewsAgent/1.0"

_HELP_TEXT = """\
**NewsAgent** — headlines via Hacker News (no API key needed)

```
@news-agent              # top 5 stories
@news-agent 10           # top 10 stories
@news-agent new          # newest
@news-agent best         # all-time best
@news-agent ask          # Ask HN
@news-agent show         # Show HN
@news-agent jobs         # job postings
@news-agent help         # this message
```"""

_FEED_LABELS = {
    "top":  "Top",
    "new":  "Newest",
    "best": "Best",
    "ask":  "Ask HN",
    "show": "Show HN",
    "job":  "Jobs",
}


class NewsAgent(Actor):
    """On-demand HackerNews headlines agent."""

    def __init__(self, **kwargs):
        kwargs.setdefault("name", "news-agent")
        super().__init__(**kwargs)
        self.protected = False

    async def on_start(self):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/spawn",
            {
                "agentId":   self.actor_id,
                "agentName": self.name,
                "agentType": "data",
                "timestamp": time.time(),
            },
        )
        logger.info(f"[{self.name}] NewsAgent started.")

    async def handle_message(self, msg: Message):
        payload = msg.payload or {}
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("content") or "")
        else:
            text = str(payload)
        text = text.strip()

        # Strip @news-agent prefix
        for prefix in ("@news-agent", "@news_agent"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].lstrip()
                break

        arg = text.lower().strip()
        parts = arg.split()
        first = parts[0] if parts else ""

        # Parse feed + count
        if first in ("", "top"):
            feed  = "top"
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_COUNT
        elif first == "new":
            feed  = "new"
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_COUNT
        elif first == "best":
            feed  = "best"
            count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else DEFAULT_COUNT
        elif first == "ask":
            feed  = "ask"
            count = DEFAULT_COUNT
        elif first == "show":
            feed  = "show"
            count = DEFAULT_COUNT
        elif first in ("jobs", "job"):
            feed  = "job"
            count = DEFAULT_COUNT
        elif first.isdigit():
            feed  = "top"
            count = int(first)
        elif first == "help":
            await self._reply(_HELP_TEXT)
            return
        else:
            feed  = "top"
            count = DEFAULT_COUNT

        count = min(count, MAX_COUNT)
        label = _FEED_LABELS.get(feed, feed)
        await self._reply(f"📰 Fetching top {count} {label} stories from Hacker News...")

        try:
            report = await self._fetch_hn(feed, count)
            await self._reply(report)
        except Exception as exc:
            await self._reply(f"⚠ Could not fetch news: {exc}")

        self.metrics.tasks_completed += 1

    async def _fetch_hn(self, feed: str, count: int) -> str:
        try:
            import aiohttp
        except ImportError:
            return "Error: `aiohttp` is not installed. Cannot fetch news."

        label = _FEED_LABELS.get(feed, feed)
        ids_url = f"{HN_API}/{feed}stories.json"

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        headers = {"User-Agent": USER_AGENT}

        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(ids_url) as resp:
                ids: list[int] = await resp.json(content_type=None)

            take = min(count, len(ids), MAX_COUNT)

            # Fetch stories concurrently
            tasks = [
                self._fetch_item(session, ids[i])
                for i in range(take)
            ]
            items = await asyncio.gather(*tasks, return_exceptions=True)

            lines = []
            for i, (item_id, item) in enumerate(zip(ids[:take], items)):
                if isinstance(item, Exception) or not item:
                    continue
                title = item.get("title", "(no title)")
                url   = item.get("url", "")
                score = item.get("score", 0)
                hn_url = f"https://news.ycombinator.com/item?id={item_id}"
                link  = url if url else hn_url
                lines.append(
                    f"{i + 1}. **[{title}]({link})** — ⬆ {score} · [HN]({hn_url})"
                )

            if not lines:
                return f"No {label} stories found right now."

            return (
                f"**Hacker News — {label} Stories** (top {take})\n\n"
                + "\n".join(lines)
                + "\n\n*Source: [news.ycombinator.com](https://news.ycombinator.com)*"
            )

    @staticmethod
    async def _fetch_item(session, item_id: int) -> dict | None:
        try:
            url = f"{HN_API}/item/{item_id}.json"
            async with session.get(url) as resp:
                return await resp.json(content_type=None)
        except Exception:
            return None

    async def _reply(self, content: str):
        await self._mqtt_publish(
            f"agents/{self.actor_id}/chat",
            {"from": self.name, "to": "user", "content": content, "timestamp": time.time()},
        )

    def _current_task_description(self) -> str:
        return "idle — fetches HN headlines on demand"
