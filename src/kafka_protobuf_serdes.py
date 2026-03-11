from __future__ import annotations
from schema_registry_client import SchemaRegistryClient
from proto_schema import ProtoSchema
from field_encryption import FieldEncryptor, get_encrypted_fields
from utilities import setup_logging


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Setup module logging
logger = setup_logging()


# schema_id → ProtoSchema: lets the deserializer find the right class
_schema_id_to_message: dict[int, "ProtoSchema"] = {}

# schema_id → (metadata, rule_set, subject): CSFLE context for the deserializer
_schema_id_to_csfle: dict[int, tuple[dict | None, dict | None, str]] = {}



class KafkaProtobufSerializer:
    """
    Python equivalent of:
      io.confluent.kafka.serializers.protobuf.KafkaProtobufSerializer

    Supports all three SubjectNameStrategies, both
    ReferenceSubjectNameStrategies, and Confluent CSFLE.
    """

    def __init__(
        self,
        sr: SchemaRegistryClient,
        subject_name_strategy: str = "TopicNameStrategy",
        auto_register: bool = True,
        reference_subject_name_strategy: str = "DefaultReferenceSubjectNameStrategy",
        field_encryptor: FieldEncryptor | None = None,
    ) -> None:
        self.sr = sr
        self.subject_name_strategy = subject_name_strategy
        self.auto_register = auto_register
        self.ref_strategy = reference_subject_name_strategy
        self.field_encryptor = field_encryptor

    def _subject(self, topic: str, message: ProtoSchema, is_key: bool) -> str:
        suffix = "key" if is_key else "value"
        match self.subject_name_strategy:
            case "TopicNameStrategy":
                return f"{topic}-{suffix}"
            case "RecordNameStrategy":
                return message.name
            case "TopicRecordNameStrategy":
                return f"{topic}-{message.name}"
            case _:
                raise ValueError(f"Unknown SubjectNameStrategy: {self.subject_name_strategy}")

    def _ref_subject(self, ref_name: str) -> str:
        match self.ref_strategy:
            case "DefaultReferenceSubjectNameStrategy":
                return ref_name          # e.g. "other.proto"
            case "QualifiedReferenceSubjectNameStrategy":
                return ref_name.replace("/", ".").removesuffix(".proto")
            case _:
                raise ValueError(f"Unknown ReferenceSubjectNameStrategy: {self.ref_strategy}")

    def serialize(
        self,
        topic: str,
        message: ProtoSchema,
        data: dict,
        is_key: bool = False,
        references: list[dict] | None = None,
        metadata: dict | None = None,
        rule_set: dict | None = None,
    ) -> bytes:
        subject = self._subject(topic, message, is_key)
        schema_str = message.to_schema_string()

        if self.auto_register:
            schema_id = self.sr.register(
                subject, schema_str,
                references=references or [],
                metadata=metadata,
                rule_set=rule_set,
            )
        else:
            vs = self.sr.get_version(subject, "latest")
            schema_id = vs["id"]

        _schema_id_to_message[schema_id] = message
        _schema_id_to_csfle[schema_id] = (metadata, rule_set, subject)

        # CSFLE: encrypt tagged fields before protobuf serialization
        if self.field_encryptor and metadata and rule_set:
            enc_fields = get_encrypted_fields(metadata, rule_set)
            if enc_fields:
                data = self.field_encryptor.encrypt_fields(data, enc_fields, subject)

        return self.sr.encode(schema_id, message.serialize(data))


class KafkaProtobufDeserializer:
    """
    Python equivalent of:
      io.confluent.kafka.serializers.protobuf.KafkaProtobufDeserializer

    - specific_type set  → deserialize into that ProtoMessage (specific type)
    - specific_type None → return dict  (DynamicMessage equivalent)
    - derive_type=True   → mirrors derive.type=true Java config
    - field_encryptor    → enables CSFLE auto-decryption of tagged fields
    """

    def __init__(
        self,
        sr: SchemaRegistryClient,
        specific_type: ProtoSchema | None = None,
        derive_type: bool = False,
        field_encryptor: FieldEncryptor | None = None,
    ) -> None:
        self.sr = sr
        self.specific_type = specific_type
        self.derive_type = derive_type
        self.field_encryptor = field_encryptor

    def deserialize(self, raw_message: bytes) -> dict:
        schema_id, payload = self.sr.decode_header(raw_message)
        schema_info = self.sr.get_schema_by_id(schema_id)   # validate + warm cache

        if self.specific_type:
            data = self.specific_type.deserialize(payload)
        elif schema_id in _schema_id_to_message:
            data = _schema_id_to_message[schema_id].deserialize(payload)
        else:
            raise RuntimeError(
                f"No message class registered for schema_id={schema_id}. "
                "Pass specific_type= to KafkaProtobufDeserializer or serialize first."
            )

        # CSFLE: decrypt tagged fields after protobuf deserialization
        if self.field_encryptor:
            metadata = schema_info.get("metadata")
            rule_set = schema_info.get("ruleSet")
            subject = None
            # Fall back to in-process cache when SR response lacks metadata
            if (not metadata or not rule_set) and schema_id in _schema_id_to_csfle:
                metadata, rule_set, subject = _schema_id_to_csfle[schema_id]
            if metadata and rule_set:
                enc_fields = get_encrypted_fields(metadata, rule_set)
                if enc_fields:
                    if not subject:
                        versions = self.sr.get_versions_for_schema(schema_id)
                        subject = versions[0]["subject"] if versions else str(schema_id)
                    data = self.field_encryptor.decrypt_fields(data, enc_fields, subject)

        return data
