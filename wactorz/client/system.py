"""What the server says about its own running: spend and logs."""

from dataclasses import dataclass, field
from typing import Any

from .transport import Transport


@dataclass(frozen=True)
class Cost:
    """Model spend in the current period, against a limit if one is set."""

    spend_usd: float
    limit_usd: float | None
    period: str
    raw: dict[str, Any] = field(default_factory=dict)


class System:
    """The server itself."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def cost(self) -> Cost:
        """This period's spend and its limit."""
        raw = await self._transport.request("GET", "/api/cost")
        limit = raw.get("limit_usd")
        return Cost(
            spend_usd=float(raw.get("spend_usd") or 0.0),
            limit_usd=None if limit is None else float(limit),
            period=str(raw.get("period") or ""),
            raw=raw,
        )

    async def logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """The newest application log lines, oldest first."""
        answer = await self._transport.request("GET", "/api/logs", params={"limit": limit})
        return list(answer.get("entries") or ())
