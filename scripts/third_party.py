"""List every installed distribution with its version and license.

An image redistributes the packages installed in it, and most of their licenses
ask that whoever passes the software on says what it is and under which terms.
This writes that list for the environment it is run in:

    python3 scripts/third_party.py > THIRD_PARTY.txt

One line per distribution. The license is what the package declares: its SPDX
expression where it has one, else its license classifiers, else the first line
of its free-text field. The full texts stay where pip put them, in each
package's ``.dist-info`` folder, which the last column names when it holds any.
"""

import sys
from collections.abc import Iterable
from importlib.metadata import Distribution, distributions

#: Shown when a package declares nothing: said plainly, not left blank.
UNDECLARED = "not declared"

#: A free-text license field can be the whole license. One line of it names it.
LONGEST = 100


def field(distribution: Distribution, name: str) -> str:
    """One metadata field of ``distribution``, or nothing when it has none."""
    return ((distribution.metadata.get_all(name) or [""])[0] or "").strip()


def declared_license(distribution: Distribution) -> str:
    """What ``distribution`` says its license is, in the most exact form it gives."""
    metadata = distribution.metadata
    expression = field(distribution, "License-Expression")
    if expression:
        return expression
    classifiers = [
        value.split(" :: ")[-1]
        for value in metadata.get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    if classifiers:
        return ", ".join(classifiers)
    first = field(distribution, "License").splitlines()[:1]
    return first[0].strip()[:LONGEST] if first and first[0].strip() else UNDECLARED


def license_files(distribution: Distribution) -> list[str]:
    """The license texts ``distribution`` installed, as paths under its ``.dist-info``."""
    declared = distribution.metadata.get_all("License-File") or []
    return sorted(str(name) for name in declared)


def lines(found: Iterable[Distribution]) -> list[str]:
    """One line per distribution, by name, each once."""
    by_name: dict[str, str] = {}
    for distribution in found:
        name = distribution.metadata["Name"]
        if not name:
            continue
        line = f"{name} {distribution.version}: {declared_license(distribution)}"
        texts = license_files(distribution)
        if texts:
            line += f" (texts: {', '.join(texts)})"
        by_name.setdefault(name.lower(), line)
    return [by_name[name] for name in sorted(by_name)]


def main() -> int:
    sys.stdout.write("\n".join(lines(distributions())) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
