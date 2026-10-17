"""Server-only Ed25519 key; clients pin its public key."""
import base64
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from app.config import settings


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@lru_cache(maxsize=1)
def private_key():
    path = Path(settings.SIGNING_KEY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        candidate = Ed25519PrivateKey.generate()
        pem = candidate.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        try:
            with open(path, "xb") as stream:
                os.chmod(path, 0o600)
                stream.write(pem)
        except FileExistsError:
            pass
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("Signing key must be Ed25519")
    return key


def public_key_info():
    key = private_key().public_key()
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"algorithm": "Ed25519", "key_id": hashlib.sha256(raw).hexdigest()[:16],
            "public_key": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


def signed_envelope(payload):
    info = public_key_info()
    signature = private_key().sign(canonical_json(payload))
    return {"payload": payload, "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
            "algorithm": info["algorithm"], "key_id": info["key_id"]}
