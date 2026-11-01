"""What can go wrong talking to a server, each reason a class of its own."""

import asyncio


class ClientError(Exception):
    """Anything a client call can fail with."""


class NoServerError(ClientError):
    """`Client.from_env` found no server to talk to."""

    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is not set: it names the server, as http://host:8888")


class EmptyMessageError(ClientError, ValueError):
    """A message with no words, which the server would drop without a reply."""

    def __init__(self) -> None:
        super().__init__("a message needs some text, even one that carries files")


class UnauthorizedError(ClientError):
    """The server wants a key, and this one is missing or wrong."""

    def __init__(self, url: str) -> None:
        super().__init__(f"{url} refused the key: check WACTORZ_API_KEY against its API_KEY")


class RefusedError(ClientError):
    """The server will not act here, whoever is asking: a host or origin it does not allow."""

    def __init__(self, url: str, said: str) -> None:
        super().__init__(
            f"{url} refused the request: {said}. Is this host in WACTORZ_ALLOWED_HOSTS?"
        )


class NotFoundError(ClientError):
    """The route exists, and what it was asked for does not."""

    def __init__(self, path: str, said: str) -> None:
        super().__init__(f"{path}: {said}")


class NotOfferedError(ClientError):
    """This server has no such route: it is older, or the feature is turned off."""

    def __init__(self, path: str) -> None:
        super().__init__(f"this server does not offer {path}")


class RejectedError(ClientError):
    """The server understood the request and said no, in its own words."""

    def __init__(self, status: int, said: str) -> None:
        super().__init__(said)
        self.status = status


class UnavailableError(ClientError):
    """The server could not be reached, or could not answer."""

    def __init__(self, url: str, why: object) -> None:
        super().__init__(f"{url} is not available: {why}")


class TimedOutError(ClientError, asyncio.TimeoutError):
    """A wait ran out. A change may still have happened, so it is not called a failure."""

    def __init__(self, method: str, path: str, seconds: float) -> None:
        unknown = "" if method == "GET" else "; it may still have happened"
        super().__init__(f"{method} {path} had no answer in {seconds:g}s{unknown}")
        self.may_have_happened = method != "GET"


class TurnInterruptedError(ClientError):
    """The socket went during a turn. The turn may finish; its answer is in the history."""

    def __init__(self) -> None:
        super().__init__(
            "the connection dropped during this turn; it may still finish, and its "
            "answer will be in the chat history"
        )
