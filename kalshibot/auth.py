from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def expand_key_path(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path or ""))


def load_private_key(path: str):
    pem = Path(expand_key_path(path)).read_bytes()
    return serialization.load_pem_private_key(pem, password=None)


def signed_headers(key_id: str, private_key, method: str, sign_path: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{sign_path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


def sign_path_from_url(base_url: str, path: str) -> str:
    """Kalshi signs timestamp+METHOD+path including /trade-api/v2, no query string."""
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    prefix = parsed.path.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return f"{prefix}{path.split('?', 1)[0]}"
