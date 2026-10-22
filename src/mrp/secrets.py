"""Customer key management: envelope encryption, rotation, and keeping secrets out of logs.

A customer's provider key is sealed with a fresh 256-bit data key (AES-GCM); the data key is
wrapped by a key-encryption key held in a key management service. Both seals carry the workspace
and provider as associated data, so a ciphertext copied into another workspace's row cannot be
opened. Rotating the key-encryption key re-wraps data keys without ever decrypting the secrets.

`LocalKms` keeps versioned key-encryption keys from configuration for development and tests; in
production the same interface is backed by the cloud provider's KMS, so the key-encryption key
never leaves it.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Kms(Protocol):
    current_version: str

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]: ...
    def unwrap(self, version: str, wrapped: bytes, aad: bytes) -> bytes: ...


class LocalKms:
    """Versioned key-encryption keys, e.g. MRP_KEKS="v2:<base64>,v1:<base64>" (first is current)."""

    def __init__(self, keys: dict[str, bytes], current: str):
        if current not in keys:
            raise ValueError("current KEK version missing")
        for v, k in keys.items():
            if len(k) != 32:
                raise ValueError(f"KEK {v} must be 32 bytes")
        self.keys, self.current_version = keys, current

    @classmethod
    def from_env(cls, value: str | None = None) -> LocalKms:
        value = value or os.environ.get("MRP_KEKS", "")
        if not value:
            raise RuntimeError("MRP_KEKS is not set")
        pairs = [p.split(":", 1) for p in value.split(",") if p.strip()]
        keys = {v.strip(): base64.b64decode(k) for v, k in pairs}
        return cls(keys, pairs[0][0].strip())

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        return self.current_version, nonce + AESGCM(self.keys[self.current_version]).encrypt(nonce, dek, aad)

    def unwrap(self, version: str, wrapped: bytes, aad: bytes) -> bytes:
        return AESGCM(self.keys[version]).decrypt(wrapped[:12], wrapped[12:], aad)


class ScalewayKms:
    """Key Manager on Scaleway: data keys are wrapped and unwrapped by the service, so the
    key-encryption key never leaves it. Rotation is Key Manager's own (the key's rotation policy);
    the ciphertext carries the key version, so `kek_version` here only names the key.

    The request shapes follow Scaleway's Key Manager API (v1alpha1); the adapter is tested against
    recorded responses, not a live account.
    """

    def __init__(self, key_id: str, secret_key: str, region: str = "fr-par", transport: Any = None):
        import httpx

        self.key_id = key_id
        self.current_version = f"scw:{key_id}"
        self.http = httpx.Client(
            base_url=f"https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys/{key_id}",
            headers={"X-Auth-Token": secret_key},
            timeout=10,
            transport=transport,
        )

    def wrap(self, dek: bytes, aad: bytes) -> tuple[str, bytes]:
        r = self.http.post(
            "/encrypt",
            json={
                "plaintext": base64.b64encode(dek).decode(),
                "associated_data": base64.b64encode(aad).decode(),
            },
        )
        r.raise_for_status()
        return self.current_version, base64.b64decode(r.json()["ciphertext"])

    def unwrap(self, version: str, wrapped: bytes, aad: bytes) -> bytes:
        if version != self.current_version:
            raise ValueError(f"data key was wrapped by {version}, not {self.current_version}")
        r = self.http.post(
            "/decrypt",
            json={
                "ciphertext": base64.b64encode(wrapped).decode(),
                "associated_data": base64.b64encode(aad).decode(),
            },
        )
        r.raise_for_status()
        return base64.b64decode(r.json()["plaintext"])


def kms_from_env() -> Kms:
    if os.environ.get("MRP_KMS_KEY_ID"):
        return ScalewayKms(
            os.environ["MRP_KMS_KEY_ID"],
            os.environ["SCW_SECRET_KEY"],
            os.environ.get("SCW_DEFAULT_REGION", "fr-par"),
        )
    return LocalKms.from_env()


@dataclass(frozen=True)
class Sealed:
    kek_version: str
    wrapped_dek: bytes
    nonce: bytes
    ciphertext: bytes
    fingerprint: str


def _aad(workspace_id: str, provider: str) -> bytes:
    return f"mrp/provider-key/{workspace_id}/{provider}".encode()


def seal(kms: Kms, workspace_id: str, provider: str, secret: str) -> Sealed:
    dek = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aad = _aad(workspace_id, provider)
    ct = AESGCM(dek).encrypt(nonce, secret.encode(), aad)
    version, wrapped = kms.wrap(dek, aad)
    return Sealed(version, wrapped, nonce, ct, "…" + secret[-4:])


def open_sealed(kms: Kms, workspace_id: str, provider: str, s: Sealed) -> str:
    aad = _aad(workspace_id, provider)
    dek = kms.unwrap(s.kek_version, s.wrapped_dek, aad)
    return AESGCM(dek).decrypt(s.nonce, s.ciphertext, aad).decode()


def rewrap(kms: Kms, workspace_id: str, provider: str, s: Sealed) -> Sealed:
    """Rotation: unwrap the data key with its old KEK, wrap it with the current one. The secret
    itself stays sealed throughout."""
    aad = _aad(workspace_id, provider)
    dek = kms.unwrap(s.kek_version, s.wrapped_dek, aad)
    version, wrapped = kms.wrap(dek, aad)
    return Sealed(version, wrapped, s.nonce, s.ciphertext, s.fingerprint)


# ---- logs never carry secrets or customer content ----------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"\bmrp_[A-Za-z0-9_-]{16,}"),  # our API keys
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}"),  # Anthropic / OpenAI style keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    for p in SECRET_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs secrets from every record and drops fields that could hold customer content.

    Content is kept out by construction (log calls pass ids, counts and costs, never messages);
    this filter is the second line of defence for mistakes, and a test asserts both.
    """

    CONTENT_FIELDS = ("messages", "prompt", "content", "completion", "response_text")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = (
                tuple(redact(str(a)) for a in record.args) if isinstance(record.args, tuple) else record.args
            )
        for f in self.CONTENT_FIELDS:
            if hasattr(record, f):
                setattr(record, f, "[OMITTED]")
        return True
