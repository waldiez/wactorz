"""Which packages an agent may have installed without the operator being asked.

The list is the operator's: a slash command is read from what was typed, never
from a reply the model wrote, and the social channels refuse this one. The
installer keeps it and asks on the dashboard about anything not on it; see
`wactorz.core.packages`.
"""

from ....core.packages import normalized
from .dispatch import CommandContext, command
from .groups import AGENTS

USAGE = "Usage: /packages · /packages allow <name> … · /packages forget <name> …"

#: The verbs, and the installer action each is.
_ACTIONS = {"allow": "packages_allow", "forget": "packages_forget"}


def _listed(result: dict) -> str:
    """The list as the operator reads it, in the order it is consulted."""

    def names(key: str) -> str:
        return ", ".join(result.get(key) or []) or "none"

    return "\n".join(
        (
            "Packages installed without asking:",
            f"  allowed by you: {names('allowed')}",
            f"  from WACTORZ_PIP_ALLOW: {names('from_env')}",
            f"  built in: {names('default')}",
            "Anything else is asked about on the dashboard first.",
            USAGE,
        )
    )


def _forgetting(names: list[str], result: dict) -> list[str]:
    """What became of each name ``/packages forget`` was given.

    Said from what the installer took off, not from what was typed: a misspelt
    name forgets nothing, and saying it was forgotten would hide the typo.
    """
    gone = set(result.get("forgotten") or [])
    kept = set(result.get("default") or []) | set(result.get("from_env") or [])
    forgotten = [p for p in names if normalized(p) in gone]
    staying = [p for p in names if normalized(p) in kept]
    absent = [p for p in names if p not in forgotten and p not in staying]
    lines = []
    if forgotten:
        lines.append(
            f"Forgotten: {', '.join(forgotten)}. Agents already saved keep what they were "
            "placed with; this asks again for agents placed from now on."
        )
    if staying:
        lines.append(
            f"Still allowed, built in or named in WACTORZ_PIP_ALLOW: {', '.join(staying)}."
        )
    if absent:
        lines.append(f"Not on your list: {', '.join(absent)}.")
    return lines


@command(
    "/packages",
    exact=("/packages",),
    prefixes=("/packages ",),
    summary="list, allow or forget packages installed without asking",
    group=AGENTS,
    forms=(
        ("/packages", "list the packages agents may install without asking"),
        ("/packages allow <package> …", "add packages to that list"),
        ("/packages forget <package> …", "take off packages you added"),
    ),
)
async def manage_packages(ctx: CommandContext, argument: str) -> str:
    """Show the list, or allow or forget packages on it."""
    if not argument.strip():
        result = await ctx.actor.delegate_to_installer({"action": "packages_list"})
        return result["error"] if result.get("error") else _listed(result)
    verb, _, rest = argument.strip().partition(" ")
    packages = rest.replace(",", " ").split()
    if verb not in _ACTIONS or not packages:
        return USAGE
    result = await ctx.actor.delegate_to_installer({"action": _ACTIONS[verb], "packages": packages})
    if result.get("error"):
        return f"Could not change the list: {result['error']}"
    lines = []
    refused = result.get("refused") or []
    taken = [p for p in packages if p not in refused]
    if verb == "allow" and taken:
        lines.append(f"Allowed: {', '.join(taken)}.")
    if verb == "forget":
        lines += _forgetting(taken, result)
    if refused:
        lines.append(f"Not package names: {', '.join(refused)}.")
    lines.append(f"Allowed by you: {', '.join(result.get('allowed') or []) or 'none'}.")
    return "\n".join(lines)
