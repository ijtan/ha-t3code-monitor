"""T3 Connect DPoP proof format checks."""

import base64
import json

from custom_components.t3code.t3_connect import T3Connect


def _decode(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def test_dpop_proof_is_es256_and_bound_to_request() -> None:
    client = T3Connect(None)  # proof generation does not use the HTTP session

    proof = client._proof("POST", "https://relay.t3.codes/v1/client/dpop-token")
    header, payload, signature = proof.split(".")

    assert _decode(header)["typ"] == "dpop+jwt"
    assert _decode(header)["alg"] == "ES256"
    assert _decode(header)["jwk"] == client.jwk
    assert _decode(payload)["htm"] == "POST"
    assert _decode(payload)["htu"] == "https://relay.t3.codes/v1/client/dpop-token"
    assert len(base64.urlsafe_b64decode(signature + "==")) == 64


def test_dpop_key_can_be_restored_for_existing_entry() -> None:
    original = T3Connect(None)
    restored = T3Connect(None, original.private_key_pem)

    assert restored.thumbprint == original.thumbprint
    assert (
        restored._proof("GET", "https://example.invalid/").split(".")[0]
        == original._proof("GET", "https://example.invalid/").split(".")[0]
    )
