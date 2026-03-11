from __future__ import annotations
from typing import Protocol, runtime_checkable
from pathlib import Path


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


@runtime_checkable
class ProtoSchema(Protocol):
    """Common interface for both dynamic (ProtoMessage) and compiled (CompiledProtoMessage)
    Protobuf schema objects.

    Any object that exposes these attributes and methods can be used interchangeably
    by the SerDes layer (KafkaProtobufSerializer / KafkaProtobufDeserializer).
    """
    name: str
    file_name: str

    def to_schema_string(self) -> str:
        """Return the .proto schema text for Schema Registry registration."""
        ...

    def serialize(self, data: dict) -> bytes:
        """Encode a dict to Protobuf binary."""
        ...

    def deserialize(self, raw: bytes) -> dict:
        """Decode Protobuf binary to a plain dict."""
        ...

    def save_schema(self, directory: str | Path) -> Path:
        """Write the .proto schema text to *directory*/{file_name}."""
        ...
