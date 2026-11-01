"""What the command bar offers as you type: commands, agents, and what you said before."""

from textual.suggester import Suggester


class Offers(Suggester):
    """Ghost text for the command bar, accepted with the right arrow.

    ``/`` offers the server's commands, ``@`` the agents, and anything else the
    newest thing typed that starts the same way, as the dashboard's box does.
    """

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.commands: list[str] = []
        self.agents: list[str] = []
        self.typed: list[str] = []

    async def get_suggestion(self, value: str) -> str | None:
        """The offer for ``value``, or None when there is nothing to offer."""
        if not value:
            return None
        if value.startswith("/"):
            found = self.commands
        elif value.startswith("@"):
            found = [f"@{agent} " for agent in self.agents]
        else:
            found = list(reversed(self.typed))
        return next((text for text in found if text.startswith(value) and text != value), None)
