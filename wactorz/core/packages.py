"""Which packages may be installed without anyone being asked.

A package an agent asks for is chosen by a model from a description, and a
plausible guess at a name is exactly what a typosquat is published to catch:
whatever it is, its install runs with this process's privileges, on main and on
every node the agent is sent to. So a package is installed without asking only
when it is on the list: the packages the catalogue and the examples install,
the ones the installer knows by their import names, those the operator names in
``WACTORZ_PIP_ALLOW``, and those the operator has allowed since. Anything else
waits for the operator to say yes.

Names are compared as PEP 503 normalises them, so ``Duckduckgo_Search``,
``duckduckgo.search`` and ``duckduckgo-search`` are one package, and a version
specifier or extras do not change which package it is.
"""

import os
import re
from collections.abc import Iterable

#: How long an install waits for the operator to allow a package not on the
#: list, in seconds. A yes that comes later is still remembered, so asking again
#: then succeeds. Whoever waits on an install waits this long on top of pip.
APPROVAL_TIMEOUT_S = 120

#: The variable naming further packages that may be installed without asking,
#: separated by commas or whitespace.
ALLOW_ENV = "WACTORZ_PIP_ALLOW"

#: pip package name → the module it is imported as, where the two differ or
#: where an agent is known to ask for it.
PACKAGE_TO_IMPORT = {
    "opencv-python": "cv2",
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "beautifulsoup4": "bs4",
    "pymupdf": "fitz",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pdfplumber": "pdfplumber",
    "httpx": "httpx",
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
    "torch": "torch",
    "transformers": "transformers",
    "ultralytics": "ultralytics",
    "pyserial": "serial",
    "duckduckgo-search": "duckduckgo_search",
    "ddgs": "duckduckgo_search",
    "asyncssh": "asyncssh",
    "rich": "rich",
    "tqdm": "tqdm",
    "lxml": "lxml",
    "aiohttp": "aiohttp",
}

#: Importable module name → pip package name, for a request that names the import.
IMPORT_TO_PACKAGE = {
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "fitz": "pymupdf",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "pdfplumber": "pdfplumber",
    "httpx": "httpx",
    "requests": "requests",
    "numpy": "numpy",
    "pandas": "pandas",
    "torch": "torch",
    "transformers": "transformers",
    "ultralytics": "ultralytics",
    "serial": "pyserial",
    "duckduckgo_search": "duckduckgo-search",
    "ddgs": "duckduckgo-search",
    "asyncssh": "asyncssh",
    "rich": "rich",
    "tqdm": "tqdm",
    "lxml": "lxml",
    "aiohttp": "aiohttp",
}

#: What the catalogue's recipes and the examples install, beyond the maps above.
_CATALOGUE_AND_EXAMPLES = (
    "aiomqtt",
    "edge-tts",
    "faster-whisper",
    "reachy-mini",
    "webrtcvad-wheels",
)

#: The name everything else is compared by: a version specifier or extras cut off.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def normalized(package: str) -> str:
    """``package``'s name as PEP 503 compares it, without version or extras."""
    match = _NAME.match((package or "").strip())
    if match is None:
        return ""
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


#: The packages that may be installed without asking, as `normalized` names.
DEFAULT_ALLOWED = frozenset(
    normalized(name) for name in (*PACKAGE_TO_IMPORT, *_CATALOGUE_AND_EXAMPLES)
)


def allowed_by_env() -> frozenset[str]:
    """The packages ``WACTORZ_PIP_ALLOW`` adds, as `normalized` names."""
    raw = os.environ.get(ALLOW_ENV, "")
    return frozenset(filter(None, (normalized(p) for p in raw.replace(",", " ").split())))


def unlisted(packages: Iterable[str], allowed: Iterable[str]) -> list[str]:
    """The packages in ``packages`` whose names are not in ``allowed``, in order.

    ``allowed`` holds `normalized` names; ``packages`` are as asked for, and are
    returned that way.
    """
    names = frozenset(allowed)
    return [p for p in packages if normalized(p) not in names]
