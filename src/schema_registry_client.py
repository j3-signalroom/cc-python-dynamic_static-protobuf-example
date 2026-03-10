from __future__ import annotations
from utilities import setup_logging
import requests
from typing import Any
import struct


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Setup module logging
logger = setup_logging()


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode one unsigned varint from *data* at *offset*. Returns (value, new_offset)."""
    result, shift = 0, 0
    while True:
        b = data[offset]; offset += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, offset


class SchemaRegistryClient:
    """
    Thin wrapper around the Confluent Schema Registry REST API.
    Mirrors the Java SchemaRegistryClient surface used by the SerDes.
    """

    MAGIC_BYTE = 0x00  # Confluent wire-format magic byte

    def __init__(self, url: str, api_key: str, api_secret: str) -> None:
        self.url = url.rstrip("/")
        self._session = requests.Session()
        self._session.auth = (api_key, api_secret)
        self._session.headers.update(
            {"Content-Type": "application/vnd.schemaregistry.v1+json"}
        )
        # Local cache: schema_id → schema_obj
        self._cache: dict[int, dict] = {}

    # ── Schema types ──────────────────────────────────────────────────────

    def get_types(self) -> list[str]:
        """GET /schemas/types"""
        return self._get("/schemas/types")

    # ── Subjects ──────────────────────────────────────────────────────────

    def get_subjects(self) -> list[str]:
        """GET /subjects"""
        return self._get("/subjects")

    def get_versions(self, subject: str) -> list[int]:
        """GET /subjects/{subject}/versions"""
        return self._get(f"/subjects/{subject}/versions")

    def get_version(self, subject: str, version: int | str = "latest") -> dict:
        """GET /subjects/{subject}/versions/{version}"""
        return self._get(f"/subjects/{subject}/versions/{version}")

    def get_schema_by_id(self, schema_id: int) -> dict:
        """GET /schemas/ids/{id}   (cached)"""
        if schema_id not in self._cache:
            self._cache[schema_id] = self._get(f"/schemas/ids/{schema_id}")
        return self._cache[schema_id]

    def get_versions_for_schema(self, schema_id: int) -> list[dict]:
        """GET /schemas/ids/{id}/versions"""
        return self._get(f"/schemas/ids/{schema_id}/versions")

    def referenced_by(self, subject: str, version: int) -> list[int]:
        """GET /subjects/{subject}/versions/{version}/referencedby"""
        return self._get(f"/subjects/{subject}/versions/{version}/referencedby")

    # ── Registration ──────────────────────────────────────────────────────

    def register(
        self,
        subject: str,
        schema: str,
        schema_type: str = "PROTOBUF",
        references: list[dict] | None = None,
        metadata: dict | None = None,
        rule_set: dict | None = None,
    ) -> int:
        """
        POST /subjects/{subject}/versions
        Returns the schema ID.

        Parameters
        ----------
        metadata : dict, optional
            CSFLE field-level tags, e.g.
            {"properties": {"MyMsg.ssn.tags": "PII"}}
        rule_set : dict, optional
            CSFLE encryption rules, e.g.
            {"domainRules": [{"name": "enc", "kind": "TRANSFORM",
             "type": "ENCRYPT", "mode": "WRITEREAD", "tags": ["PII"],
             "params": {"encrypt.kek.name": "my-kek", ...}}]}
        """
        body: dict[str, Any] = {"schema": schema, "schemaType": schema_type}
        if references:
            body["references"] = references
        if metadata:
            body["metadata"] = metadata
        if rule_set:
            body["ruleSet"] = rule_set

        result = self._post(f"/subjects/{subject}/versions", body)
        schema_id = result["id"]
        logger.info(f"  [SR] Registered '{subject}' → schema_id={schema_id}")
        return schema_id

    # ── Deletion ──────────────────────────────────────────────────────────

    def delete_version(self, subject: str, version: int | str = "latest") -> int:
        """DELETE /subjects/{subject}/versions/{version}"""
        result = self._delete(f"/subjects/{subject}/versions/{version}")
        logger.info(f"  [SR] Deleted '{subject}' v{result}")
        return result

    def delete_subject(self, subject: str) -> list[int]:
        """DELETE /subjects/{subject}  (all versions, soft delete)"""
        result = self._delete(f"/subjects/{subject}")
        logger.info(f"  [SR] Soft-deleted all versions of '{subject}': {result}")
        return result

    def delete_subject_permanent(self, subject: str) -> list[int]:
        """DELETE /subjects/{subject}?permanent=true"""
        result = self._delete(f"/subjects/{subject}?permanent=true")
        logger.info(f"  [SR] Permanently deleted '{subject}': {result}")
        return result

    # ── Compatibility ─────────────────────────────────────────────────────

    def test_compatibility(
        self,
        subject: str,
        schema: str,
        schema_type: str = "PROTOBUF",
        version: str = "latest",
    ) -> bool:
        """POST /compatibility/subjects/{subject}/versions/{version}"""
        body: dict[str, Any] = {"schema": schema, "schemaType": schema_type}
        result = self._post(
            f"/compatibility/subjects/{subject}/versions/{version}", body
        )
        return result.get("is_compatible", False)

    def get_compatibility(self, subject: str | None = None) -> str:
        if subject:
            result = self._get(f"/config/{subject}")
        else:
            result = self._get("/config")
        return result.get("compatibilityLevel", result.get("compatibility", "UNKNOWN"))

    def set_compatibility(self, level: str, subject: str | None = None) -> None:
        body = {"compatibility": level}
        endpoint = f"/config/{subject}" if subject else "/config"
        self._put(endpoint, body)
        scope = f"subject '{subject}'" if subject else "global"
        logger.info(f"  [SR] Set compatibility={level} for {scope}")

    # ── DEK Registry (CSFLE) ─────────────────────────────────────────────

    def create_kek(
        self,
        name: str,
        kms_type: str,
        kms_key_id: str,
        shared: bool = False,
    ) -> dict:
        """POST /dek-registry/v1/keks — register a Key Encryption Key."""
        body = {
            "name": name,
            "kmsType": kms_type,
            "kmsKeyId": kms_key_id,
            "shared": shared,
        }
        result = self._post("/dek-registry/v1/keks", body)
        logger.info(f"  [DEK] Created KEK '{name}' (kmsType={kms_type})")
        return result

    def get_kek(self, name: str) -> dict:
        """GET /dek-registry/v1/keks/{name}"""
        return self._get(f"/dek-registry/v1/keks/{name}")

    def create_dek(
        self,
        kek_name: str,
        subject: str,
        algorithm: str = "AES256_GCM",
        encrypted_key_material: str | None = None,
    ) -> dict:
        """POST /dek-registry/v1/keks/{name}/deks — create a Data Encryption Key.

        Parameters
        ----------
        encrypted_key_material : str, optional
            Base64-encoded ciphertext of the DEK, encrypted by the KEK via KMS.
        """
        body: dict[str, Any] = {"subject": subject, "algorithm": algorithm}
        if encrypted_key_material:
            body["encryptedKeyMaterial"] = encrypted_key_material
        result = self._post(f"/dek-registry/v1/keks/{kek_name}/deks", body)
        logger.info(f"  [DEK] Created DEK for subject='{subject}' under KEK '{kek_name}'")
        return result

    def get_dek(
        self,
        kek_name: str,
        subject: str,
        algorithm: str = "AES256_GCM",
    ) -> dict:
        """GET /dek-registry/v1/keks/{name}/deks/{subject}"""
        return self._get(
            f"/dek-registry/v1/keks/{kek_name}/deks/{subject}?algorithm={algorithm}"
        )

    # ── Wire format ───────────────────────────────────────────────────────

    def encode(self, schema_id: int, payload: bytes) -> bytes:
        """Confluent wire format: magic(1) + schema_id(4 BE) + msg_index(varint) + payload.

        The message-index array identifies which message in the .proto file is encoded.
        An empty array (encoded as the single byte 0x00) means 'first/only message'.
        """
        return struct.pack(">bI", self.MAGIC_BYTE, schema_id) + b"\x00" + payload

    def decode_header(self, data: bytes) -> tuple[int, bytes]:
        """Return (schema_id, raw_payload) after stripping the Confluent wire-format header."""
        if not data or data[0] != self.MAGIC_BYTE:
            raise ValueError(
                f"Not a Confluent wire-format message "
                f"(expected magic 0x00, got 0x{data[0]:02X})"
            )
        schema_id = struct.unpack(">I", data[1:5])[0]
        # Skip message-index varint array: first varint is array length,
        # followed by that many index varints.  For a single top-level message
        # the array is empty (length = 0x00) so this is just one byte to skip.
        array_len, offset = _read_varint(data, 5)
        for _ in range(array_len):
            _, offset = _read_varint(data, offset)
        return schema_id, data[offset:]

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _get(self, path: str) -> Any:
        r = self._session.get(self.url + path)
        self._raise(r)
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        r = self._session.post(self.url + path, json=body)
        self._raise(r)
        return r.json()

    def _put(self, path: str, body: dict) -> Any:
        r = self._session.put(self.url + path, json=body)
        self._raise(r)
        return r.json()

    def _delete(self, path: str) -> Any:
        r = self._session.delete(self.url + path)
        self._raise(r)
        return r.json()

    @staticmethod
    def _raise(r: requests.Response) -> None:
        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f"SR HTTP {r.status_code}: {detail}")
