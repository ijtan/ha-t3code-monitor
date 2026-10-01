"""T3 Connect DPoP proof format checks."""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.t3code.dpop import DpopKey, _base64url
from custom_components.t3code.errors import T3ClientError
from custom_components.t3code.t3_connect import _oauth_tokens


def _decode(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_dpop_proof_is_es256_and_bound_to_request() -> None:
    key = DpopKey.generate()

    proof = key.create_proof("POST", "https://relay.t3.codes/v1/client/dpop-token")
    header, payload, signature = proof.split(".")

    assert _decode(header)["typ"] == "dpop+jwt"
    assert _decode(header)["alg"] == "ES256"
    assert _decode(header)["jwk"]["crv"] == "P-256"
    assert _decode(payload)["htm"] == "POST"
    assert _decode(payload)["htu"] == "https://relay.t3.codes/v1/client/dpop-token"
    assert len(base64.urlsafe_b64decode(signature + "==")) == 64


def test_dpop_key_can_be_restored_for_existing_entry() -> None:
    original = DpopKey.generate()
    restored = DpopKey.from_pem(original.private_key_pem)

    assert restored.thumbprint == original.thumbprint
    assert (
        restored.create_proof("GET", "https://example.invalid/").split(".")[0]
        == original.create_proof("GET", "https://example.invalid/").split(".")[0]
    )


def test_dpop_proof_hashes_the_access_token() -> None:
    key = DpopKey.generate()
    token = "relay-access-token"

    proof = key.create_proof("POST", "https://relay.t3.codes/connect", token)
    payload = _decode(proof.split(".")[1])

    assert payload["ath"] == _base64url(hashlib.sha256(token.encode()).digest())


def test_dpop_key_rejects_non_p256_curve() -> None:
    key = ec.generate_private_key(ec.SECP384R1())

    with pytest.raises(T3ClientError, match="must use P-256"):
        DpopKey(key)


def test_oauth_response_requires_a_valid_access_token() -> None:
    assert _oauth_tokens({"access_token": "access", "refresh_token": "refresh"}) == {
        "access_token": "access",
        "refresh_token": "refresh",
    }
    with pytest.raises(T3ClientError, match="invalid OAuth token response"):
        _oauth_tokens({"access_token": ""})
