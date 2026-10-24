"""Turning written units into words, for a synthesiser that reads what it is given.

A hosted service normalises text before it speaks it, so ``21 °C`` is read as
degrees whatever is sent. A self-hosted one reads the characters: the degree sign
goes unsaid and the solidus in ``5 m/s`` is read out as "slash". The difference
only shows once the speech is made somewhere other than a vendor's cloud, which
is exactly what this branch is for.

Units are expanded **only where a number stands in front of them**, because the
same letters are ordinary words everywhere else -- "the m in metres", a sensor
called "W", a path with a slash in it. That rule is what keeps this from
rewriting prose it was never meant to touch.
"""

from __future__ import annotations

import re

#: Units, as Home Assistant reports them, and how to say them. Singular and
#: plural, because "1 degrees" is worse than the symbol was.
UNITS: dict[str, tuple[str, str]] = {
    "°C": ("degree Celsius", "degrees Celsius"),
    "°F": ("degree Fahrenheit", "degrees Fahrenheit"),
    "°": ("degree", "degrees"),
    "%": ("percent", "percent"),
    "m/s": ("metre per second", "metres per second"),
    "km/h": ("kilometre per hour", "kilometres per hour"),
    "mph": ("mile per hour", "miles per hour"),
    "hPa": ("hectopascal", "hectopascals"),
    "mbar": ("millibar", "millibars"),
    "kWh": ("kilowatt hour", "kilowatt hours"),
    "Wh": ("watt hour", "watt hours"),
    "kW": ("kilowatt", "kilowatts"),
    "µg/m³": ("microgram per cubic metre", "micrograms per cubic metre"),
    "mm": ("millimetre", "millimetres"),
    "cm": ("centimetre", "centimetres"),
    "km": ("kilometre", "kilometres"),
}

#: Longest first, so ``km/h`` is matched before ``km`` and ``°C`` before ``°``.
_UNITS = "|".join(re.escape(unit) for unit in sorted(UNITS, key=len, reverse=True))

#: A number, then a unit. The space between them is optional because a reading
#: is written both ways, and nothing after the unit may be a letter -- otherwise
#: the "m" of "5 metres" would be expanded and the rest left dangling.
_MEASUREMENT = re.compile(rf"(?<![\w.])(\d+(?:[.,]\d+)?)\s?({_UNITS})(?![\w])")


def speakable(text: str) -> str:
    """Rewrite the measurements in `text` as they would be said aloud."""

    def say(match: re.Match[str]) -> str:
        amount, unit = match.group(1), match.group(2)
        singular, plural = UNITS[unit]
        # Compared as a number, not trimmed as text: stripping the zeros off
        # "100" leaves "1", which made a hundred of something singular. "1.0" is
        # still one of them, and "0" is plural in English.
        one = float(amount.replace(",", ".")) == 1
        return f"{amount} {singular if one else plural}"

    return _MEASUREMENT.sub(say, text)
