"""kalshi_auth - the parsed private key is kept while the key file stays the same.

Every signed request used to read and parse the PEM three times. The cache must never
change an answer: a replaced key is picked up, a bad file is never remembered as good or
as bad, and can_sign() still needs the file and the API key to be there.

The cache tests run everywhere (the PEM parser is replaced by a counter). The tests that
sign for real need a working `cryptography`; they generate a throwaway key in tmp_path -
no key material is ever stored in the repo."""
import base64
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import kalshi_auth as ka  # noqa: E402


def _crypto_usable() -> bool:
    """Asked in a child process: a cryptography build without its backend does not raise
    ImportError, it panics - loudly, inside whichever process imported it."""
    probe = "from cryptography.hazmat.primitives.asymmetric import padding, rsa"
    try:
        return subprocess.run([sys.executable, "-c", probe], capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_crypto = pytest.mark.skipif(not _crypto_usable(),
                                  reason="cryptography is not usable in this interpreter")


class FakeKey:
    def __init__(self, pem: bytes):
        self.pem = pem


@pytest.fixture
def auth(tmp_path, monkeypatch):
    """A configured key file, an empty cache, and a PEM parser that counts its calls."""
    key = tmp_path / "throwaway_key.pem"
    key.write_bytes(b"KEY-A")
    monkeypatch.setenv("KALSHI_API_KEY", "throwaway-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))
    monkeypatch.setattr(ka, "_KEY_CACHE", {})
    reads = []

    def parse(path):
        data = Path(path).read_bytes()
        reads.append(data)
        if not data.startswith(b"KEY"):
            raise ValueError(f"KALSHI_PRIVATE_KEY_PATH={path} is not a valid PEM RSA key")
        return FakeKey(data)
    monkeypatch.setattr(ka, "_read_private_key", parse)
    return key, reads


def rewrite(path: Path, data: bytes, mtime_ns: int | None = None) -> None:
    """Replace the file's content; by default its modification time moves on a second."""
    before = os.stat(path)
    path.write_bytes(data)
    stamp = before.st_mtime_ns + 1_000_000_000 if mtime_ns is None else mtime_ns
    os.utime(path, ns=(stamp, stamp))


# ── the cache ─────────────────────────────────────────────────────────────────

def test_the_key_file_is_parsed_once_while_it_does_not_change(auth):
    key, reads = auth
    first = ka.load_private_key()
    assert first.pem == b"KEY-A"
    assert ka.load_private_key() is first and ka.can_sign() is True
    assert ka.can_sign() is True and ka.status()["private_key_loadable"] is True
    assert reads == [b"KEY-A"]                # one parse for six uses
    assert len(ka._KEY_CACHE) == 1


def test_a_replaced_key_file_is_loaded_again(auth, tmp_path, monkeypatch):
    key, reads = auth
    assert ka.load_private_key().pem == b"KEY-A"
    rewrite(key, b"KEY-B")                                  # new content, later mtime
    assert ka.load_private_key().pem == b"KEY-B"
    same_time = os.stat(key).st_mtime_ns
    rewrite(key, b"KEY-CC", mtime_ns=same_time)             # same timestamp, other size
    assert ka.load_private_key().pem == b"KEY-CC"
    rewrite(key, b"KEY-CC")                                 # only touched: loaded again
    assert ka.load_private_key().pem == b"KEY-CC"
    assert reads == [b"KEY-A", b"KEY-B", b"KEY-CC", b"KEY-CC"]
    other = tmp_path / "other_throwaway_key.pem"
    other.write_bytes(b"KEY-OTHER")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(other))    # another path
    assert ka.load_private_key().pem == b"KEY-OTHER"
    assert len(ka._KEY_CACHE) == 1                          # never more than one key held


def test_a_failed_load_is_never_cached(auth):
    key, reads = auth
    rewrite(key, b"not a key")
    for _ in range(2):                         # not remembered as bad: tried every time
        with pytest.raises(ValueError):
            ka.load_private_key()
        assert ka.can_sign() is False
        assert ka.status()["private_key_loadable"] is False
    assert len(reads) == 2 * 4 and ka._KEY_CACHE == {}
    rewrite(key, b"KEY-GOOD")                  # repaired: works at once
    assert ka.can_sign() is True and ka.load_private_key().pem == b"KEY-GOOD"
    rewrite(key, b"broken again")              # and a good key is not served for a file
    assert ka.can_sign() is False              # that no longer holds one
    with pytest.raises(ValueError):
        ka.load_private_key()
    assert ka._KEY_CACHE == {}


def test_can_sign_gives_the_same_answers_with_a_warm_cache(auth, monkeypatch):
    key, reads = auth
    assert ka.can_sign() is True               # cache is warm from here on
    monkeypatch.setenv("KALSHI_API_KEY", "  ")
    assert ka.can_sign() is False and ka.build_headers(method="GET", path="/x") == {}
    monkeypatch.setenv("KALSHI_API_KEY", "throwaway-key-id")
    assert ka.can_sign() is True
    key.unlink()                               # the file must still be there, every call
    assert ka.can_sign() is False and ka.load_private_key() is None
    st = ka.status()
    assert (st["private_key_file_exists"], st["private_key_loadable"], st["can_sign"]) == (
        False, False, False)
    with pytest.raises(RuntimeError):
        ka.signed_get("/portfolio/balance")
    key.write_bytes(b"KEY-A")
    assert ka.can_sign() is True
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH")
    assert ka.can_sign() is False and ka.load_private_key() is None
    assert ka.status()["private_key_path_set"] is False


def test_a_key_file_that_cannot_be_examined_is_read_as_before_and_not_cached(auth,
                                                                             monkeypatch):
    key, reads = auth
    monkeypatch.setattr(ka, "_key_file_identity", lambda path: None)
    assert ka.load_private_key().pem == b"KEY-A" and ka.load_private_key().pem == b"KEY-A"
    assert len(reads) == 2 and ka._KEY_CACHE == {}


def test_a_key_swapped_in_during_the_read_is_not_filed_under_the_old_file(auth,
                                                                         monkeypatch):
    """Two request threads at the moment the key is replaced: the one that examined the
    old file but read the new one must not leave 'old file -> new key' behind."""
    key, reads = auth
    slow = ka._read_private_key

    def swapped_mid_read(path):
        rewrite(key, b"KEY-NEW")
        return slow(path)
    monkeypatch.setattr(ka, "_read_private_key", swapped_mid_read)
    assert ka.load_private_key().pem == b"KEY-NEW" and ka._KEY_CACHE == {}
    monkeypatch.setattr(ka, "_read_private_key", slow)
    assert ka.load_private_key().pem == b"KEY-NEW" and len(ka._KEY_CACHE) == 1


# ── real signatures (a throwaway key, generated here, never stored in the repo) ──

@pytest.fixture
def real_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    def make(name):
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path = tmp_path / name
        path.write_bytes(priv.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()))
        return path, priv.public_key()
    path, public = make("throwaway_key.pem")
    monkeypatch.setenv("KALSHI_API_KEY", "throwaway-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(path))
    monkeypatch.setattr(ka, "_KEY_CACHE", {}, raising=False)
    return path, public, make


def verifies(public, signature_b64: bytes, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        public.verify(base64.b64decode(signature_b64), message,
                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                  salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


@pytest.fixture
def parses(monkeypatch):
    """How many times the real PEM parser runs."""
    from cryptography.hazmat.primitives import serialization
    real, n = serialization.load_pem_private_key, []

    def counting(*a, **k):
        n.append(1)
        return real(*a, **k)
    monkeypatch.setattr(serialization, "load_pem_private_key", counting)
    return n


@needs_crypto
def test_a_signed_request_parses_the_key_once_not_three_times(real_key, parses,
                                                              monkeypatch):
    """117 ms of CPU per order-book lookup with RSA-2048 (825 ms with RSA-4096) went on
    reading and parsing the same file three times; the signature itself takes 0.4 ms."""
    import requests
    path, public, _ = real_key
    sent = []

    class Answer:
        def raise_for_status(self):
            pass

        def json(self):
            return {"orderbook": {}}

    def fake_requests_get(url, params=None, headers=None, timeout=None):
        sent.append((url, headers))
        return Answer()
    monkeypatch.setattr(requests, "get", fake_requests_get)
    monkeypatch.setenv("KALSHI_API_BASE", "https://example.invalid/trade-api/v2")
    for _ in range(5):
        got = ka.signed_get("/markets/T/orderbook", params={"depth": 5})
        assert got == {"orderbook": {}}
    assert len(parses) == 1                    # was 15: three per request
    url, headers = sent[-1]
    assert url == "https://example.invalid/trade-api/v2/markets/T/orderbook"
    assert headers["KALSHI-ACCESS-KEY"] == "throwaway-key-id"
    signed = f"{headers['KALSHI-ACCESS-TIMESTAMP']}GET/trade-api/v2/markets/T/orderbook"
    assert verifies(public, headers["KALSHI-ACCESS-SIGNATURE"].encode(), signed.encode())


@needs_crypto
def test_a_replaced_real_key_signs_the_very_next_request(real_key, parses):
    path, public_a, make = real_key
    sig = ka.sign_request(method="get", path="/trade-api/v2/x", timestamp_ms=1)
    assert verifies(public_a, sig, b"1GET/trade-api/v2/x")
    new_path, public_b = make("replacement_throwaway_key.pem")
    rewrite(path, new_path.read_bytes())
    sig = ka.sign_request(method="GET", path="/trade-api/v2/x", timestamp_ms=2)
    assert verifies(public_b, sig, b"2GET/trade-api/v2/x")
    assert not verifies(public_a, sig, b"2GET/trade-api/v2/x")
    assert len(parses) == 2
    rewrite(path, b"-----BEGIN RSA PRIVATE KEY-----\nnot base64\n")
    assert ka.can_sign() is False              # the real parser's ValueError, as before
    with pytest.raises(ValueError, match="is not a valid PEM RSA key"):
        ka.sign_request(method="GET", path="/trade-api/v2/x", timestamp_ms=3)


@needs_crypto
def test_signatures_are_made_one_at_a_time_and_all_verify(real_key, monkeypatch):
    """The dashboards sign from request threads, now on one shared key object."""
    path, public, _ = real_key
    real = ka.load_private_key()
    held = []

    class Watched:
        def sign(self, *a, **k):
            held.append(ka._SIGN_LOCK.locked())
            return real.sign(*a, **k)
    monkeypatch.setattr(ka, "load_private_key", lambda: Watched())
    out = {}

    def worker(i):
        out[i] = ka.sign_request(method="GET", path="/trade-api/v2/x", timestamp_ms=i)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert held == [True] * 8
    assert all(verifies(public, out[i], f"{i}GET/trade-api/v2/x".encode())
               for i in range(8))
