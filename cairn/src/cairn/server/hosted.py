"""Running Cairn as one owner's hosted deployment.

In a hosted deployment a front door in the same machine signs the owner in before any
request reaches Cairn, and Cairn listens only on a private network that the front door
and Cairn's own dispatcher can reach. Cairn then has exactly one user, the owner, so its
own accounts are switched off: nobody can register, and requests need no Cairn token.

Never set this where Cairn's port is reachable by anything else.
"""

from __future__ import annotations

import os

FRONT_DOOR_ENV = "CAIRN_FRONT_DOOR"
VERIFIER_URL_ENV = "CAIRN_VERIFIER_URL"


def front_door() -> bool:
    return os.environ.get(FRONT_DOOR_ENV, "").strip() == "1"


def verifier_url() -> str:
    """The evidence verifier the attestation page uses by default. Set when the image is built."""

    return os.environ.get(VERIFIER_URL_ENV, "").strip()
