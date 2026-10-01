"""P-256 DPoP key management and proof generation."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .errors import T3ClientError


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class DpopKey:
    """A P-256 key used to bind relay and environment access tokens."""

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(key.curve, ec.SECP256R1):
            raise T3ClientError("T3 Connect DPoP key must use P-256")
        self._key = key
        public_numbers = key.public_key().public_numbers()
        self._jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": _base64url(public_numbers.x.to_bytes(32, "big")),
            "y": _base64url(public_numbers.y.to_bytes(32, "big")),
        }
        canonical_jwk = json.dumps(self._jwk, separators=(",", ":"))
        self.thumbprint = _base64url(hashlib.sha256(canonical_jwk.encode()).digest())

    @classmethod
    def generate(cls) -> DpopKey:
        """Create a fresh client proof key."""
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_pem(cls, pem: str) -> DpopKey:
        """Restore a config-entry proof key."""
        try:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
        except (TypeError, ValueError) as err:
            raise T3ClientError("Stored T3 Connect DPoP key is invalid") from err
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise T3ClientError("Stored T3 Connect DPoP key is not an EC key")
        return cls(key)

    @property
    def private_key_pem(self) -> str:
        """Serialize this key for Home Assistant config-entry storage."""
        return self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    def create_proof(
        self, method: str, url: str, access_token: str | None = None
    ) -> str:
        """Create a compact ES256 DPoP proof for an HTTP request."""
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self._jwk}
        payload: dict[str, Any] = {
            "htm": method.upper(),
            "htu": url,
            "jti": str(uuid.uuid4()),
            "iat": int(time.time()),
        }
        if access_token is not None:
            payload["ath"] = _base64url(hashlib.sha256(access_token.encode()).digest())
        signing_input = ".".join(
            _base64url(json.dumps(part, separators=(",", ":")).encode())
            for part in (header, payload)
        )
        der_signature = self._key.sign(
            signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())
        )
        r, s = decode_dss_signature(der_signature)
        signature = _base64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"{signing_input}.{signature}"
