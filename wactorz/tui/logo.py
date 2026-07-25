"""ASCII wordmarks for the Wactorz TUI, with a width-aware picker."""

# ANSI-Shadow style — used as the home-tab header on wide terminals.
LOGO_FULL = r"""
██╗    ██╗ █████╗  ██████╗████████╗ ██████╗ ██████╗ ███████╗
██║    ██║██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗╚══███╔╝
██║ █╗ ██║███████║██║        ██║   ██║   ██║██████╔╝  ███╔╝
██║███╗██║██╔══██║██║        ██║   ██║   ██║██╔══██╗ ███╔╝
╚███╔███╔╝██║  ██║╚██████╗   ██║   ╚██████╔╝██║  ██║███████╗
 ╚══╝╚══╝ ╚═╝  ╚═╝ ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚══════╝
""".strip("\n")

# Compact fallback for narrow terminals.
LOGO_COMPACT = r"""
╦ ╦╔═╗╔═╗╔╦╗╔═╗╦═╗╔═╗
║║║╠═╣║   ║ ║ ║╠╦╝╔═╝
╚╩╝╩ ╩╚═╝ ╩ ╚═╝╩╚═╚═╝
""".strip("\n")

# Widest line of LOGO_FULL (kept in sync with the art above).
_FULL_WIDTH = max(len(line) for line in LOGO_FULL.splitlines())


def pick_logo(width: int) -> str:
    """Return the full wordmark when it fits, else the compact one."""
    return LOGO_FULL if width >= _FULL_WIDTH else LOGO_COMPACT
