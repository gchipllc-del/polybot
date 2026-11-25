"""
Kalshi RSA-PSS request signing.

Kalshi's current API (the one your fresh API key + RSA key target)
authenticates each request by signing ``timestamp + HTTP_method + path``
with RSA-PSS-SHA256, base64-encoding the signature, and sending three
headers:

  KALSHI-ACCESS-KEY        — your Key ID (the public identifier)
  KALSHI-ACCESS-TIMESTAMP  — request time in milliseconds
  KALSHI-ACCESS-SIGNATURE  — base64(rsa-pss-sha256(<string-to-sign>))

This module owns auth ONLY. It does not import the broken legacy
``kalshi-python`` SDK in ``lib/kalshi_client.py`` — that path is
deprecated and we'll replace it once auth here is proven.

**Secrets handling — never paste these in chat or commit them:**

  1. ``.env``:
       KALSHI_API_KEY=<Key ID string>           # public identifier, not the secret
       KALSHI_PRIVATE_KEY_PATH=~/.polybot/kalshi_key.pem
       KALSHI_API_BASE=https://api.elections.kalshi.com/trade-api/v2

  2. RSA private key (PEM-encoded, the file Kalshi emailed you):
       mkdir -p ~/.polybot
       chmod 700 ~/.polybot
       # paste the PEM content into the file, then:
       chmod 600 ~/.polybot/kalshi_key.pem

The file path is read from the env var; the file itself never enters
git. ``.gitignore`` already covers ``.env`` so the Key ID is also safe.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# The parsed private key, kept while the key FILE stays the same. One signed request used
# to read and parse the PEM three times (can_sign, build_headers' can_sign, sign_request):
# about 40 ms each for RSA-2048 and 270 ms for RSA-4096, against 0.4 ms for the signature
# itself - 0.1 to 0.8 s of CPU between a quote and the order book it is checked against.
#   * keyed on (path, modification time, size): a replaced or edited key file is loaded
#     again on the next call;
#   * a load that fails is never remembered - it is retried, and raises, every time;
#   * at most one key is held, and it stays in this process's memory, which adds nothing:
#     the file is readable by the same user.
_KEY_CACHE: dict[tuple, object] = {}
# The dashboards sign from request threads; one signature at a time on the shared key.
_SIGN_LOCK = threading.Lock()


def _resolve_private_key_path() -> Path | None:
    """Read KALSHI_PRIVATE_KEY_PATH, expand ~, validate the file exists.

    Returns ``None`` if unset / missing — caller decides whether that's
    fatal (signing) or fine (anonymous market browsing).
    """
    raw = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.exists():
        return None
    return p


def _read_private_key(path: Path):
    """Read and parse the PEM file - the expensive step the cache exists to skip.

    Raises ``ValueError`` if the file isn't a valid PEM key.
    """
    from cryptography.hazmat.primitives import serialization
    try:
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None,
            )
    except ValueError as e:
        raise ValueError(
            f"KALSHI_PRIVATE_KEY_PATH={path} is not a valid PEM RSA key: {e}"
        )


def _key_file_identity(path: Path) -> tuple | None:
    """(path, modification time, size) of the key file as it is now, or ``None`` when
    it cannot be examined - then nothing is cached and the read below raises whatever
    it always raised."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def load_private_key():
    """Load the RSA private key from disk into a ``cryptography`` object.

    Returns ``None`` when the path env var isn't set OR the file is
    missing — auth helpers treat that as "no auth available" and skip
    signed calls. The file's existence is checked on every call, before
    the cache is looked at.

    Raises ``ValueError`` if the file exists but isn't a valid PEM key
    (i.e. the user populated the path but the content is corrupted).

    The parsed key is reused for as long as the file's path, modification
    time and size stay the same (see ``_KEY_CACHE``).
    """
    path = _resolve_private_key_path()
    if path is None:
        return None

    ident = _key_file_identity(path)
    if ident is not None:
        cached = _KEY_CACHE.get(ident)
        if cached is not None:
            return cached
    _KEY_CACHE.clear()               # another file, or a changed one: forget the old key
    key = _read_private_key(path)    # raises on a bad file - and then nothing is cached
    # Remember it only if the file was the same before and after the read: a key swapped
    # in mid-read must not be filed under the old file's identity.
    if ident is not None and _key_file_identity(path) == ident:
        _KEY_CACHE[ident] = key
    return key


def sign_request(*, method: str, path: str, timestamp_ms: int) -> bytes:
    """Produce the base64-encoded RSA-PSS signature for one request.

    The string-to-sign is exactly ``<timestamp_ms><METHOD><path>`` —
    NO query string, NO body. Method is uppercase. Path is the URI
    path only (e.g. ``/trade-api/v2/portfolio/balance``), not the full
    URL.

    Returns the base64-encoded signature bytes ready to drop into the
    ``KALSHI-ACCESS-SIGNATURE`` header.

    Raises ``RuntimeError`` if no private key is configured (the caller
    should have checked ``can_sign()`` first).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    pk = load_private_key()
    if pk is None:
        raise RuntimeError(
            "Kalshi private key not configured. Set KALSHI_PRIVATE_KEY_PATH "
            "and place the PEM file there with mode 600."
        )

    msg = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    with _SIGN_LOCK:
        signature = pk.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    return base64.b64encode(signature)


def build_headers(*, method: str, path: str) -> dict:
    """Build the three Kalshi auth headers for one request.

    Use this for signed endpoints (``/portfolio/*``, order placement,
    etc.). Public endpoints (``/markets``, ``/events``) don't need
    headers and shouldn't pay the signature cost.

    Returns ``{}`` if auth isn't configured — caller decides whether to
    proceed (public endpoints only) or bail.
    """
    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    if not api_key or not can_sign():
        return {}

    ts_ms = int(time.time() * 1000)
    sig = sign_request(method=method, path=path, timestamp_ms=ts_ms)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
        "KALSHI-ACCESS-SIGNATURE": sig.decode("ascii"),
        "Accept": "application/json",
    }


def can_sign() -> bool:
    """Quick yes/no — is everything in place to make signed requests?"""
    if not os.environ.get("KALSHI_API_KEY", "").strip():
        return False
    if _resolve_private_key_path() is None:
        return False
    try:
        return load_private_key() is not None
    except ValueError:
        return False


def base_url() -> str:
    """Return the configured Kalshi base URL, defaulting to production."""
    return os.environ.get("KALSHI_API_BASE", DEFAULT_BASE_URL).rstrip("/")


def status() -> dict:
    """Diagnostic — what's wired and what's missing?

    Returned dict NEVER includes the API key or private key contents —
    only path/presence flags so this is safe to log.
    """
    pk_path = _resolve_private_key_path()
    pk_loadable = False
    if pk_path is not None:
        try:
            pk_loadable = load_private_key() is not None
        except ValueError:
            pk_loadable = False

    return {
        "api_key_present": bool(os.environ.get("KALSHI_API_KEY", "").strip()),
        "private_key_path_set": bool(
            os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        ),
        "private_key_file_exists": pk_path is not None,
        "private_key_loadable": pk_loadable,
        "base_url": base_url(),
        "can_sign": can_sign(),
    }


# ── Convenience: signed GET ──────────────────────────────────────────

def signed_get(path: str, *, params: dict | None = None, timeout: int = 15):
    """Make a signed GET request. ``path`` is the path part only,
    e.g. ``/portfolio/balance`` — the base URL gets prepended.

    Returns the parsed JSON on success or raises ``requests.HTTPError``
    on non-2xx. Caller is responsible for handling network errors.
    """
    import requests

    if not can_sign():
        raise RuntimeError(
            "Kalshi signed request attempted but auth isn't configured. "
            "Set KALSHI_API_KEY + KALSHI_PRIVATE_KEY_PATH."
        )

    # IMPORTANT: the SIGNATURE PATH must include the API prefix
    # (e.g. /trade-api/v2/portfolio/balance), not just the suffix.
    # We derive it from base_url + path.
    from urllib.parse import urlparse
    full_url = f"{base_url()}{path}"
    sign_path = urlparse(full_url).path

    headers = build_headers(method="GET", path=sign_path)
    r = requests.get(full_url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()
