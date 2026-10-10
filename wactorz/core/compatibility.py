"""Which versions of Wactorz can work together as a server and a node.

A node runs the same package the server does, and what passes between them --
a spawn's fields, the heartbeat, the code an agent is built from -- is a
contract neither side can check field by field. The version number stands in
for it: two installs of one release series, the same major and minor number,
keep that contract, and a patch release only fixes what the series already
does. So a server and a node whose versions differ in the patch number alone
work together, and a node does not have to be deployed again for every fix.

Anything else is not compatible: another series, or a version this cannot
read, which is only ever equal to itself.
"""

import re

#: The major and minor number a version starts with.
_SERIES = re.compile(r"(\d+)\.(\d+)(?!\d)")


def series(version: str) -> tuple[int, int] | None:
    """The release series ``version`` belongs to, or None if it names none."""
    found = _SERIES.match(version.strip())
    return (int(found.group(1)), int(found.group(2))) if found else None


def compatible(one: str, other: str) -> bool:
    """Whether a server on ``one`` and a node on ``other`` can work together."""
    if one == other:
        return True
    first = series(one)
    return first is not None and first == series(other)
