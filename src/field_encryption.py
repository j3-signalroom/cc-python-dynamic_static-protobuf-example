"""Field-level encryption for Confluent Client-Side Field Level Encryption (CSFLE).

Uses AWS KMS for KEK management and the Confluent DEK Registry for
Data Encryption Key storage.

Encrypted field values use the envelope:
    magic(1) + version(1) + nonce(12) + ciphertext+tag(N)
and are base64-encoded before being stored in the Protobuf string field.
"""
from __future__ import annotations
import base64
import os
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from utilities import setup_logging


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Setup module logging
logger = setup_logging()

CSFLE_MAGIC   = 0xC0
CSFLE_VERSION = 0x01


# ── Helpers ────────────────────────────────────────────────────────────────

def get_encrypted_fields(metadata: dict | None, rule_set: dict | None) -> list[str]:
    """Return field names that should be encrypted based on metadata tags and encryption rules.

    Metadata format::

        {"properties": {"MessageName.field_name.tags": "PII"}}

    RuleSet format::

        {"domainRules": [{"type": "ENCRYPT", "tags": ["PII"], ...}]}
    """
    if not metadata or not rule_set:
        return []

    encrypt_tags: set[str] = set()
    for rule in rule_set.get("domainRules", []):
        if rule.get("type") == "ENCRYPT":
            encrypt_tags.update(rule.get("tags", []))

    fields: list[str] = []
    for key, value in metadata.get("properties", {}).items():
        if key.endswith(".tags") and value in encrypt_tags:
            parts = key.split(".")
            field_name = parts[-2]  # "Msg.field.tags" → "field"
            fields.append(field_name)
    return fields


# ── FieldEncryptor ─────────────────────────────────────────────────────────

class FieldEncryptor:
    """AES-256-GCM field-level encryption for Confluent CSFLE via AWS KMS.

    Parameters
    ----------
    sr : SchemaRegistryClient
        Required for DEK Registry access.
    kek_name : str
        Name of the Key Encryption Key registered in the DEK Registry.
    kms_key_id : str
        AWS KMS key ARN (e.g. ``arn:aws:kms:us-east-1:123456789:key/...``).
    """

    def __init__(
        self,
        sr,
        kek_name: str,
        kms_key_id: str,
    ) -> None:
        self.sr = sr
        self.kek_name = kek_name
        self.kms_key_id = kms_key_id
        self._dek_cache: dict[str, bytes] = {}

    # ── DEK management ─────────────────────────────────────────────────

    def _get_or_create_dek(self, subject: str) -> bytes:
        """Return a 256-bit AES data encryption key for *subject*.

        On first call for a subject, creates a new DEK via the DEK Registry.
        On subsequent calls, fetches the encrypted DEK and decrypts it via
        AWS KMS.  Results are cached in-process.
        """
        if subject in self._dek_cache:
            return self._dek_cache[subject]

        try:
            dek_info = self.sr.get_dek(self.kek_name, subject)
            encrypted_key = base64.b64decode(dek_info["encryptedKeyMaterial"])
            dek = self._decrypt_dek_via_kms(encrypted_key)
        except RuntimeError:
            # Generate a new 256-bit DEK, encrypt it via KMS, and register it.
            dek = os.urandom(32)
            encrypted_key = self._encrypt_dek_via_kms(dek)
            encrypted_key_b64 = base64.b64encode(encrypted_key).decode("ascii")
            dek_info = self.sr.create_dek(
                self.kek_name, subject, encrypted_key_material=encrypted_key_b64,
            )

        self._dek_cache[subject] = dek
        return dek

    def _encrypt_dek_via_kms(self, plaintext_key: bytes) -> bytes:
        """Encrypt a DEK using AWS KMS."""
        import boto3
        client = boto3.client("kms")
        resp = client.encrypt(Plaintext=plaintext_key, KeyId=self.kms_key_id)
        return resp["CiphertextBlob"]

    def _decrypt_dek_via_kms(self, encrypted_key: bytes) -> bytes:
        """Decrypt a DEK using AWS KMS."""
        import boto3
        client = boto3.client("kms")
        resp = client.decrypt(CiphertextBlob=encrypted_key, KeyId=self.kms_key_id)
        return resp["Plaintext"]

    # ── Single-value encrypt / decrypt ─────────────────────────────────

    def encrypt_value(self, value: str, subject: str) -> str:
        """Encrypt a string value → base64-encoded ciphertext."""
        dek = self._get_or_create_dek(subject)
        nonce = os.urandom(12)  # 96-bit nonce for AES-GCM
        ciphertext = AESGCM(dek).encrypt(nonce, value.encode("utf-8"), None)
        envelope = struct.pack("BB", CSFLE_MAGIC, CSFLE_VERSION) + nonce + ciphertext
        return base64.b64encode(envelope).decode("ascii")

    def decrypt_value(self, encrypted: str, subject: str) -> str:
        """Decrypt a base64-encoded encrypted value → original string."""
        raw = base64.b64decode(encrypted)
        magic = raw[0]
        if magic != CSFLE_MAGIC:
            return encrypted  # not encrypted — pass through
        dek = self._get_or_create_dek(subject)
        nonce, ciphertext = raw[2:14], raw[14:]
        plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")

    # ── Batch encrypt / decrypt on a data dict ─────────────────────────

    def encrypt_fields(self, data: dict, fields: list[str], subject: str) -> dict:
        """Return a copy of *data* with the named string fields encrypted."""
        result = dict(data)
        for field in fields:
            if field in result and isinstance(result[field], str):
                result[field] = self.encrypt_value(result[field], subject)
                logger.info(f"    [CSFLE] Encrypted field '{field}'")
        return result

    def decrypt_fields(self, data: dict, fields: list[str], subject: str) -> dict:
        """Return a copy of *data* with the named string fields decrypted."""
        result = dict(data)
        for field in fields:
            if field in result and isinstance(result[field], str):
                try:
                    result[field] = self.decrypt_value(result[field], subject)
                    logger.info(f"    [CSFLE] Decrypted field '{field}'")
                except Exception:
                    pass  # not encrypted or wrong key — leave as-is
        return result
