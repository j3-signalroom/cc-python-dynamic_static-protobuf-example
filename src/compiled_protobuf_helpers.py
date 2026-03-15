from __future__ import annotations
import glob
import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from google.protobuf.json_format import MessageToDict, ParseDict

from utilities import setup_logging


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Setup module logging
logger = setup_logging()

GENERATED_DIR = "src/generated_pb2"


def _patch_pool_duplicate(pb2_path: Path, proto_file_name: str) -> None:
    """Wrap AddSerializedFile in a generated _pb2.py to survive duplicate pool entries.

    confluent_kafka registers confluent/meta.proto in the global descriptor pool when
    its encryption modules are first imported.  Our generated meta_pb2.py then fails
    with TypeError("duplicate file name …").  This patch makes it fall back to
    FindFileByName() so the descriptor is always available regardless of import order.
    Idempotent — safe to call on an already-patched file.
    """
    if not pb2_path.exists():
        return
    content = pb2_path.read_text()
    marker = "_descriptor_pool.Default().AddSerializedFile("
    if f"try:\n    DESCRIPTOR = {marker}" in content:
        return  # already patched
    if f"DESCRIPTOR = {marker}" not in content:
        return  # unexpected format — skip
    lines = content.splitlines(keepends=True)
    result = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith(f"DESCRIPTOR = {marker}"):
            result.append(f"{indent}try:\n")
            result.append(f"{indent}    {stripped}")
            result.append(f"{indent}except TypeError:  # already registered (e.g. by confluent_kafka)\n")
            result.append(f"{indent}    DESCRIPTOR = _descriptor_pool.Default().FindFileByName({proto_file_name!r})\n")
        else:
            result.append(line)
    pb2_path.write_text("".join(result))


def compile_protos(proto_dir: str = "src/schemas", output_dir: str = GENERATED_DIR) -> None:
    """Compile all .proto files under *proto_dir* into Python _pb2.py stubs.

    Requires the ``protoc`` binary on the PATH.  Install it via your package
    manager (e.g. ``brew install protobuf``) or download from
    https://github.com/protocolbuffers/protobuf/releases.
    """
    protoc_bin = shutil.which("protoc")
    if protoc_bin is None:
        raise FileNotFoundError(
            "protoc binary not found on PATH. "
            "Install it with: brew install protobuf  (macOS) or "
            "download from https://github.com/protocolbuffers/protobuf/releases"
        )

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "__init__.py").touch()

    proto_files = glob.glob(f"{proto_dir}/**/*.proto", recursive=True)
    if not proto_files:
        logger.warning(f"No .proto files found in {proto_dir}")
        return

    for proto_file in proto_files:
        result = subprocess.run(
            [protoc_bin, f"--proto_path={proto_dir}", f"--python_out={output_dir}", proto_file],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"protoc failed for {proto_file}: {result.stderr.strip()}")

    # Ensure __init__.py exists in all subdirectories for Python imports
    for subdir in out.rglob("*"):
        if subdir.is_dir():
            (subdir / "__init__.py").touch()

    # confluent_kafka's encryption modules register confluent/meta.proto in the
    # global descriptor pool before our generated meta_pb2.py is imported.
    # Patch the generated file so AddSerializedFile falls back to FindFileByName
    # when the descriptor is already present, avoiding "duplicate file name".
    _patch_pool_duplicate(out / "confluent" / "meta_pb2.py", "confluent/meta.proto")

    logger.info(f"Compiled {len(proto_files)} .proto file(s) → {output_dir}/")


def load_compiled_message(
    proto_file: str,
    message_name: str,
    proto_dir: str = "src/schemas",
    generated_dir: str = GENERATED_DIR,
) -> CompiledProtoMessage:
    """Load a protoc-generated _pb2 module and return a CompiledProtoMessage wrapper.

    Args:
        proto_file:     File name of the .proto (e.g. "MyRecord.proto").
        message_name:   The Protobuf message name inside the file (e.g. "MyRecord").
        proto_dir:      Directory containing the original .proto files.
        generated_dir:  Directory containing the generated _pb2.py modules.
    """
    schema_text = (Path(proto_dir) / proto_file).read_text()

    # Handle subdirectory .proto files (e.g. "evolution/MyRecord_v1.proto")
    # protoc preserves directory structure, so the generated file is at the same
    # relative path under generated_dir
    relative = Path(proto_file).with_suffix("")
    module_stem = relative.name + "_pb2"
    module_path = Path(generated_dir) / relative.parent / f"{module_stem}.py"

    if not module_path.exists():
        raise FileNotFoundError(
            f"Generated module not found: {module_path}. "
            "Run compile_protos() first or check proto_file name."
        )

    # Dynamically import the _pb2 module
    spec = importlib.util.spec_from_file_location(module_stem, module_path)
    module = importlib.util.module_from_spec(spec)

    # Ensure generated_dir (and subdirectory) is on sys.path so cross-file imports resolve
    abs_gen_dir = str(Path(generated_dir).resolve())
    if abs_gen_dir not in sys.path:
        sys.path.insert(0, abs_gen_dir)
    abs_module_dir = str(module_path.parent.resolve())
    if abs_module_dir not in sys.path:
        sys.path.insert(0, abs_module_dir)

    spec.loader.exec_module(module)

    message_class = getattr(module, message_name, None)
    if message_class is None:
        raise AttributeError(
            f"Message '{message_name}' not found in {module_path}. "
            f"Available: {[n for n in dir(module) if not n.startswith('_')]}"
        )

    return CompiledProtoMessage(
        name=message_name,
        file_name=proto_file,
        _message_class=message_class,
        _schema_text=schema_text,
    )


@dataclass
class CompiledProtoMessage:
    """Wrapper around a protoc-generated _pb2 message class that satisfies the
    ProtoSchema protocol.

    Provides the same serialize/deserialize/to_schema_string interface as
    ProtoMessage, but uses pre-compiled stubs for better performance and
    compile-time type safety.
    """
    name: str
    file_name: str
    _message_class: type
    _schema_text: str

    def to_schema_string(self) -> str:
        """Return the original .proto schema text."""
        return self._schema_text

    def serialize(self, data: dict) -> bytes:
        """Encode *data* to Protobuf binary using the compiled message class."""
        return ParseDict(data, self._message_class()).SerializeToString()

    def deserialize(self, raw: bytes) -> dict:
        """Decode Protobuf binary to a plain dict."""
        msg = self._message_class()
        msg.ParseFromString(raw)
        return MessageToDict(msg, preserving_proto_field_name=True)

    def save_schema(self, directory: str | Path) -> Path:
        """Write the .proto schema text to *directory*/{file_name}."""
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / self.file_name
        out.write_text(self._schema_text)
        return out
