"""did:swid integration layer — thin glue over the ``waldiez-swid`` library.

Provides the file-backed CEL registry, the minting service (encrypted keystore +
handle index), and the aiohttp resolver routes. Readable handles live separately
in :mod:`wactorz.core.handles`.
"""

from .registry import FileSWIDRegistry
from .routes import swid_routes
from .service import MintResult, SwidMinter

__all__ = ["FileSWIDRegistry", "MintResult", "SwidMinter", "swid_routes"]
