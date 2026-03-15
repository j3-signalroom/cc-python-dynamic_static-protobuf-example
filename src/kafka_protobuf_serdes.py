from __future__ import annotations
from schema_registry_client import SchemaRegistryClient
from proto_schema import ProtoSchema
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
    ) -> None:
        self.sr = sr
        self.subject_name_strategy = subject_name_strategy
        self.auto_register = auto_register
        self.ref_strategy = reference_subject_name_strategy

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

        return self.sr.encode(schema_id, message.serialize(data))


class KafkaProtobufDeserializer:
    """
    Python equivalent of:
      io.confluent.kafka.serializers.protobuf.KafkaProtobufDeserializer

    - specific_type set  → deserialize into that ProtoMessage (specific type)
    - specific_type None → return dict  (DynamicMessage equivalent)
    - derive_type=True   → mirrors derive.type=true Java config
    """

    def __init__(
        self,
        sr: SchemaRegistryClient,
        specific_type: ProtoSchema | None = None,
        derive_type: bool = False,
    ) -> None:
        self.sr = sr
        self.specific_type = specific_type
        self.derive_type = derive_type

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

        return data
