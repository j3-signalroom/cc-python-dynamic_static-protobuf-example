from __future__ import annotations
from pathlib import Path
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.json_format import MessageToDict, ParseDict
from dataclasses import dataclass, field


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Maps proto scalar type names → FieldDescriptorProto type enum values
_PROTO_SCALAR_TYPES: dict[str, int] = {
    "double":   descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE,
    "float":    descriptor_pb2.FieldDescriptorProto.TYPE_FLOAT,
    "int32":    descriptor_pb2.FieldDescriptorProto.TYPE_INT32,
    "int64":    descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    "uint32":   descriptor_pb2.FieldDescriptorProto.TYPE_UINT32,
    "uint64":   descriptor_pb2.FieldDescriptorProto.TYPE_UINT64,
    "sint32":   descriptor_pb2.FieldDescriptorProto.TYPE_SINT32,
    "sint64":   descriptor_pb2.FieldDescriptorProto.TYPE_SINT64,
    "fixed32":  descriptor_pb2.FieldDescriptorProto.TYPE_FIXED32,
    "fixed64":  descriptor_pb2.FieldDescriptorProto.TYPE_FIXED64,
    "sfixed32": descriptor_pb2.FieldDescriptorProto.TYPE_SFIXED32,
    "sfixed64": descriptor_pb2.FieldDescriptorProto.TYPE_SFIXED64,
    "bool":     descriptor_pb2.FieldDescriptorProto.TYPE_BOOL,
    "string":   descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    "bytes":    descriptor_pb2.FieldDescriptorProto.TYPE_BYTES,
}

# name → ProtoMessage: used to resolve message-type references across schemas
_message_registry: dict[str, "ProtoMessage"] = {}

@dataclass
class ProtoField:
    name: str
    type: str
    number: int
    optional: bool = False
    repeated: bool = False


@dataclass
class ProtoMessage:
    """Protobuf message schema builder with real binary SerDes via dynamic descriptors.

    No protoc or generated stubs required.  Each instance builds its own
    google.protobuf.descriptor_pb2.FileDescriptorProto at runtime, registers it
    in a private DescriptorPool, and uses message_factory.GetMessageClass() to
    obtain a real Message class for binary encode/decode.
    """
    name: str
    syntax: str = "proto3"
    package: str = ""
    imports: list[str] = field(default_factory=list)
    fields: list[ProtoField] = field(default_factory=list)
    oneofs: dict[str, list[ProtoField]] = field(default_factory=dict)
    # The file name used in the descriptor pool and in import statements of
    # other ProtoMessages.  Defaults to "{name}.proto" when left empty.
    file_name: str = ""

    def __post_init__(self) -> None:
        if not self.file_name:
            self.file_name = f"{self.name}.proto"
        self._cls: type | None = None          # lazily built message class
        _message_registry[self.name] = self    # register for cross-schema type resolution

    def to_schema_string(self) -> str:
        lines = [f'syntax = "{self.syntax}";', ""]
        if self.package:
            lines += [f"package {self.package};", ""]
        for imp in self.imports:
            lines.append(f'import "{imp}";')
        if self.imports:
            lines.append("")
        lines.append(f"message {self.name} {{")
        for f in self.fields:
            qualifier = (
                "optional " if f.optional else ("repeated " if f.repeated else "")
            )
            lines.append(f"  {qualifier}{f.type} {f.name} = {f.number};")
        for oneof_name, oneof_fields in self.oneofs.items():
            lines.append(f"  oneof {oneof_name} {{")
            for f in oneof_fields:
                lines.append(f"    {f.type} {f.name} = {f.number};")
            lines.append("  }")
        lines.append("}")
        return "\n".join(lines)

    def save_schema(self, directory: str | Path) -> Path:
        """Write the proto3 schema string to *directory*/{file_name}.

        Creates the directory if it does not exist.  Returns the path written.
        """
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / self.file_name
        out.write_text(self.to_schema_string())
        return out

    # ── Dynamic descriptor building ────────────────────────────────────────

    def _full_type_name(self, type_name: str) -> str:
        """Return a fully-qualified Protobuf type name for a message-type field.

        Looks up *type_name* in the global message registry to find its package,
        then returns '.package.TypeName' (or '.TypeName' when there is no package).
        """
        pm = _message_registry.get(type_name)
        if pm and pm.package:
            return f".{pm.package}.{type_name}"
        return f".{type_name}"

    def _to_fdp(self) -> descriptor_pb2.FileDescriptorProto:
        """Build a FileDescriptorProto that exactly mirrors to_schema_string()."""
        fdp = descriptor_pb2.FileDescriptorProto()
        fdp.name   = self.file_name
        fdp.syntax = self.syntax
        if self.package:
            fdp.package = self.package
        for imp in self.imports:
            fdp.dependency.append(imp)

        msg_dp   = fdp.message_type.add()
        msg_dp.name = self.name

        LABEL_OPT = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        LABEL_REP = descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
        TYPE_MSG  = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE

        # Named oneofs must be declared BEFORE synthetic oneofs (proto spec).
        oneof_name_to_idx: dict[str, int] = {}
        for oneof_name in self.oneofs:
            od = msg_dp.oneof_decl.add()
            od.name = oneof_name
            oneof_name_to_idx[oneof_name] = len(msg_dp.oneof_decl) - 1

        # Regular fields — track proto3-optional ones for synthetic oneofs later.
        optional_field_names: list[str] = []
        for f in self.fields:
            fp        = msg_dp.field.add()
            fp.name   = f.name
            fp.number = f.number
            if f.type in _PROTO_SCALAR_TYPES:
                fp.type = _PROTO_SCALAR_TYPES[f.type]
            else:
                fp.type      = TYPE_MSG
                fp.type_name = self._full_type_name(f.type)
            if f.repeated:
                fp.label = LABEL_REP
            elif f.optional:
                fp.label          = LABEL_OPT
                fp.proto3_optional = True
                optional_field_names.append(f.name)
            else:
                fp.label = LABEL_OPT

        # Fields that belong to named oneofs.
        for oneof_name, oneof_fields in self.oneofs.items():
            oi = oneof_name_to_idx[oneof_name]
            for f in oneof_fields:
                fp             = msg_dp.field.add()
                fp.name        = f.name
                fp.number      = f.number
                fp.oneof_index = oi
                fp.label       = LABEL_OPT
                if f.type in _PROTO_SCALAR_TYPES:
                    fp.type = _PROTO_SCALAR_TYPES[f.type]
                else:
                    fp.type      = TYPE_MSG
                    fp.type_name = self._full_type_name(f.type)

        # Synthetic oneofs for proto3-optional fields MUST come last (proto spec).
        synthetic_base = len(msg_dp.oneof_decl)
        for i, fname in enumerate(optional_field_names):
            od      = msg_dp.oneof_decl.add()
            od.name = f"__{fname}"
            for fp in msg_dp.field:
                if fp.name == fname:
                    fp.oneof_index = synthetic_base + i
                    break

        return fdp

    def _ensure_in_pool(self, pool: descriptor_pool.DescriptorPool) -> None:
        """Add this file and all its imported dependencies to *pool* (recursively)."""
        for imp in self.imports:
            dep = next(
                (pm for pm in _message_registry.values() if pm.file_name == imp),
                None,
            )
            if dep:
                dep._ensure_in_pool(pool)
        pool.Add(self._to_fdp())

    def message_class(self) -> type:
        """Return (and cache) the dynamic Protobuf message class for this schema.

        On first call a fresh DescriptorPool is created, all dependencies are
        registered in dependency order, and GetMessageClass() returns the class.
        Subsequent calls return the cached class.
        """
        if self._cls is None:
            pool = descriptor_pool.DescriptorPool()
            self._ensure_in_pool(pool)
            full_name = f"{self.package}.{self.name}" if self.package else self.name
            desc      = pool.FindMessageTypeByName(full_name)
            self._cls = message_factory.GetMessageClass(desc)
        return self._cls

    # ── SerDes ────────────────────────────────────────────────────────────

    def serialize(self, data: dict) -> bytes:
        """Serialize *data* to real Protobuf binary using the dynamic message class."""
        return ParseDict(data, self.message_class()()).SerializeToString()

    def deserialize(self, raw: bytes) -> dict:
        """Deserialize Protobuf binary to a plain dict."""
        return MessageToDict(
            self.message_class().FromString(raw),
            preserving_proto_field_name=True,
        )
