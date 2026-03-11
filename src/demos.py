from __future__ import annotations
import json
import struct
import textwrap
import uuid

from utilities import setup_logging
from schema_registry_client import SchemaRegistryClient
from dynamic_protobuf_helpers import ProtoMessage, ProtoField
from kafka_protobuf_serdes import KafkaProtobufSerializer, KafkaProtobufDeserializer
from kafka_helpers import kafka_produce, kafka_consume_one
from field_encryption import FieldEncryptor, get_encrypted_fields


__copyright__  = "Copyright (c) 2026 Jeffrey Jonathan Jennings"
__credits__    = ["Jeffrey Jonathan Jennings"]
__license__    = "MIT"
__maintainer__ = "Jeffrey Jonathan Jennings"
__email__      = "j3@thej3.com"
__status__     = "dev"


# Setup module logging
logger = setup_logging()


def section(title: str) -> None:
    separator = "─" * 80
    logger.info(f"\n{separator}\n  {title}\n{separator}")


# ── Demo 1 ─────────────────────────────────────────────────────────────────
def demo_basic(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "") -> None:
    section("1 · Basic Protobuf Serializer & Deserializer")

    other_record = ProtoMessage(
        name="OtherRecord",
        package="com.acme",
        fields=[ProtoField("other_id", "int32", 1)],
        file_name="other.proto",   # must match my_record.imports = ["other.proto"]
    )
    my_record = ProtoMessage(
        name="MyRecord",
        package="com.acme",
        imports=["other.proto"],
        fields=[
            ProtoField("f1", "string", 1),
            ProtoField("f2", "OtherRecord", 2),
        ],
    )

    logger.info("\nProto schemas:")
    logger.info("── other.proto ──")
    logger.info(other_record.to_schema_string())
    logger.info("\n── MyRecord.proto ──")
    logger.info(my_record.to_schema_string())

    if save_dir:
        for msg in (other_record, my_record):
            path = msg.save_schema(save_dir)
            logger.info(f"  Saved → {path}")

    # Register referenced schema first (dependency order)
    logger.info("\nRegistering schemas …")
    sr.register(f"other-{run_id}.proto", other_record.to_schema_string())

    topic = f"testproto-{run_id}"
    ser   = KafkaProtobufSerializer(sr)
    wire  = ser.serialize(
        topic, my_record, {"f1": "value1", "f2": {"other_id": 123}},
        references=[
            {"name": "other.proto", "subject": f"other-{run_id}.proto", "version": 1}
        ],
    )

    magic     = wire[0]
    schema_id = struct.unpack(">I", wire[1:5])[0]
    logger.info(f"\nWire format:  magic=0x{magic:02X}  schema_id={schema_id}  payload_len={len(wire)-5}")
    logger.info(f"Full hex: {wire.hex()}")

    deser   = KafkaProtobufDeserializer(sr, specific_type=my_record)
    decoded = deser.deserialize(wire)

    if kafka_cfg:
        kafka_produce(kafka_cfg, topic, "testkey", wire)
        raw = kafka_consume_one(kafka_cfg, topic, f"demo-basic-{run_id}")
        decoded = deser.deserialize(raw) if raw else decoded
        logger.info(f"\nConsumed from Kafka: {decoded}")
    else:
        logger.info(f"\nDecoded (wire-format only): {decoded}")

    logger.info(f"\nAll registered subjects (first 10): {sr.get_subjects()[:10]}")

    vs = sr.get_version(f"{topic}-value", "latest")
    logger.info(f"\n{topic}-value (latest):")
    logger.info(json.dumps(vs, indent=2))


# ── Demo 2 ─────────────────────────────────────────────────────────────────
def demo_delete_protection(sr: SchemaRegistryClient, run_id: str) -> None:
    section("2 · Reference-Deletion Protection")

    ref_subj = f"other-{run_id}.proto"
    top_subj = f"testproto-{run_id}-value"

    try:
        ref_vs  = sr.get_version(ref_subj, "latest")
        ref_ver = ref_vs["version"]
        logger.info(f"\nChecking referencedby for '{ref_subj}' v{ref_ver} …")
        refs = sr.referenced_by(ref_subj, ref_ver)
        logger.info(f"  Referencing schema IDs: {refs}")
        if refs:
            locs = sr.get_versions_for_schema(refs[0])
            logger.info(f"  Located in subjects: {locs}")
    except RuntimeError as exc:
        logger.info(f"  (Could not query referencedby: {exc})")

    logger.info(f"\nAttempting to delete '{ref_subj}' (expect rejection) …")
    try:
        sr.delete_version(ref_subj, 1)
        logger.info("  (deletion succeeded — SR did not block it)")
    except RuntimeError as exc:
        logger.info(f"  ✗ Blocked as expected: {exc}")

    logger.info(f"\nDeleting top-level subject '{top_subj}' first …")
    try:
        sr.delete_subject(top_subj)
    except RuntimeError as exc:
        logger.info(f"  Could not delete: {exc}")

    logger.info(f"\nNow deleting '{ref_subj}' …")
    try:
        sr.delete_subject(ref_subj)
        logger.info("  ✓ Succeeded")
    except RuntimeError as exc:
        logger.info(f"  ✗ Failed: {exc}")


# ── Demo 3 ─────────────────────────────────────────────────────────────────
def demo_evolution(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "") -> None:
    section("3 · Schema Evolution (Backward Compatibility)")

    subject = f"transactions-proto-{run_id}-value"
    topic   = f"transactions-proto-{run_id}"

    v1 = ProtoMessage(
        name="MyRecord",
        fields=[ProtoField("id", "string", 1), ProtoField("amount", "float", 2)],
    )
    v2 = ProtoMessage(
        name="MyRecord",
        fields=[
            ProtoField("id",          "string", 1),
            ProtoField("amount",      "float",  2),
            ProtoField("customer_id", "string", 3),
        ],
    )

    # Set recommended compatibility for Protobuf before first registration
    try:
        sr.set_compatibility("BACKWARD_TRANSITIVE", subject)
    except RuntimeError:
        pass  # subject doesn't exist yet; inherits global

    ser   = KafkaProtobufSerializer(sr)
    deser = KafkaProtobufDeserializer(sr)

    logger.info("\n── Schema v1 ──")
    logger.info(v1.to_schema_string())
    w1 = ser.serialize(topic, v1, {"id": "1000", "amount": 500.0})
    logger.info(f"Serialized → {deser.deserialize(w1)}")

    logger.info("\n── Schema v2 (customer_id added) ──")
    logger.info(v2.to_schema_string())

    if save_dir:
        from pathlib import Path
        evo_dir = Path(save_dir) / "evolution"
        v1.file_name = "MyRecord_v1.proto"
        v2.file_name = "MyRecord_v2.proto"
        for msg in (v1, v2):
            path = msg.save_schema(evo_dir)
            logger.info(f"  Saved → {path}")

    # Test compatibility before registering
    try:
        compat = sr.test_compatibility(subject, v2.to_schema_string())
        logger.info(f"Compatibility check: {'✓ COMPATIBLE' if compat else '✗ INCOMPATIBLE'}")
    except RuntimeError as exc:
        logger.info(f"Compatibility check skipped ({exc})")

    w2 = ser.serialize(topic, v2, {"id": "1001", "amount": 700.0, "customer_id": "1221"})
    logger.info(f"Serialized → {deser.deserialize(w2)}")

    if kafka_cfg:
        kafka_produce(kafka_cfg, topic, "k1", w1)
        kafka_produce(kafka_cfg, topic, "k2", w2)
        grp = f"demo-evo-{run_id}"
        for _ in range(2):
            raw = kafka_consume_one(kafka_cfg, topic, grp)
            if raw:
                logger.info(f"  Kafka consumed: {deser.deserialize(raw)}")

    logger.info("\nVersions under subject:")
    try:
        for ver in sr.get_versions(subject):
            vs = sr.get_version(subject, ver)
            logger.info(f"  v{ver} → schema_id={vs['id']}")
    except RuntimeError as exc:
        logger.info(f"  Could not list: {exc}")


# ── Demo 4 ─────────────────────────────────────────────────────────────────
def demo_oneof(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "") -> None:
    section("4 · Multiple Event Types in the Same Topic (oneOf)")

    customer = ProtoMessage(
        name="Customer", package="io.confluent.examples.proto",
        fields=[
            ProtoField("customer_id",      "int64",  1),
            ProtoField("customer_name",    "string", 2),
            ProtoField("customer_email",   "string", 3),
            ProtoField("customer_address", "string", 4),
        ],
    )
    product = ProtoMessage(
        name="Product", package="io.confluent.examples.proto",
        fields=[
            ProtoField("product_id",   "int32",  1),
            ProtoField("product_name", "string", 2),
        ],
    )
    order = ProtoMessage(
        name="Order", package="io.confluent.examples.proto",
        imports=["Product.proto", "Customer.proto"],
        fields=[
            ProtoField("order_id",     "int32",  1),
            ProtoField("order_date",   "string", 2),
            ProtoField("order_amount", "int32",  3),
        ],
    )
    all_types = ProtoMessage(
        name="AllTypes", package="io.confluent.examples.proto",
        imports=["Customer.proto", "Product.proto", "Order.proto"],
        oneofs={
            "oneof_type": [
                ProtoField("customer", "Customer", 1),
                ProtoField("product",  "Product",  2),
                ProtoField("order",    "Order",    3),
            ]
        },
    )

    logger.info("\nWrapper schema (AllTypes):")
    logger.info(all_types.to_schema_string())

    if save_dir:
        for msg in (customer, product, order, all_types):
            path = msg.save_schema(save_dir)
            logger.info(f"  Saved → {path}")

    cust_subj = f"Customer-{run_id}.proto"
    prod_subj = f"Product-{run_id}.proto"
    ord_subj  = f"Order-{run_id}.proto"

    sr.register(cust_subj, customer.to_schema_string())
    sr.register(prod_subj, product.to_schema_string())
    sr.register(ord_subj, order.to_schema_string(), references=[
        {"name": "Customer.proto", "subject": cust_subj, "version": 1},
        {"name": "Product.proto",  "subject": prod_subj, "version": 1},
    ])

    topic      = f"all-events-{run_id}"
    references = [
        {"name": "Customer.proto", "subject": cust_subj, "version": 1},
        {"name": "Product.proto",  "subject": prod_subj, "version": 1},
        {"name": "Order.proto",    "subject": ord_subj,  "version": 1},
    ]

    ser   = KafkaProtobufSerializer(sr)
    deser = KafkaProtobufDeserializer(sr)

    events = [
        {"customer": {"customer_id": 42, "customer_name": "Alice",
                      "customer_email": "alice@example.com", "customer_address": "123 Main St"}},
        {"product":  {"product_id": 7,   "product_name": "Widget"}},
        {"order":    {"order_id": 99,     "order_date": "2025-06-01", "order_amount": 3}},
    ]

    logger.info("\nProducing heterogeneous events:")
    wires = []
    for evt in events:
        wire = ser.serialize(topic, all_types, evt, references=references)
        wires.append(wire)

    if kafka_cfg:
        for wire in wires:
            kafka_produce(kafka_cfg, topic, str(uuid.uuid4()), wire)
        grp = f"demo-oneof-{run_id}"
        for _ in range(len(events)):
            raw = kafka_consume_one(kafka_cfg, topic, grp)
            if raw:
                d = deser.deserialize(raw)
                logger.info(f"  [{next(iter(d)):8s}] ← {d}")
    else:
        for wire in wires:
            d = deser.deserialize(wire)
            logger.info(f"  [{next(iter(d)):8s}] → {d}")


# ── Demo 5 ─────────────────────────────────────────────────────────────────
def demo_null_handling(sr: SchemaRegistryClient, run_id: str, save_dir: str = "") -> None:
    section("5 · Null-Value Handling with Optional Fields (recommended)")

    schema = ProtoMessage(
        name="ExampleMessage",
        fields=[
            ProtoField("name",     "string", 1, optional=True),
            ProtoField("age",      "int32",  2, optional=True),
            ProtoField("isActive", "bool",   3, optional=True),
        ],
    )

    logger.info("\nSchema with optional fields:")
    logger.info(schema.to_schema_string())

    if save_dir:
        path = schema.save_schema(save_dir)
        logger.info(f"  Saved → {path}")

    topic = f"nullables-{run_id}"
    ser   = KafkaProtobufSerializer(sr)
    deser = KafkaProtobufDeserializer(sr)

    w1 = ser.serialize(topic, schema, {"name": "Bob"})
    w2 = ser.serialize(topic, schema, {"name": "Carol", "age": 30, "isActive": True})

    logger.info(f"\nPartial (name only): {deser.deserialize(w1)}")
    logger.info(f"Full:                {deser.deserialize(w2)}")

    logger.info("\nPre-proto3 wrapper alternative (conceptual):")
    logger.info(textwrap.indent(textwrap.dedent("""\
        import "google/protobuf/wrappers.proto";

        message GreetRequest {
          google.protobuf.StringValue name = 1;   // nullable via wrapper
        }

        # Confluent Connect converter flag:
        # wrapper.for.nullable=true
    """), "  "))


# ── Demo 6 ─────────────────────────────────────────────────────────────────
def demo_compatibility(sr: SchemaRegistryClient) -> None:
    section("6 · Schema Compatibility Rules")

    try:
        level = sr.get_compatibility()
        logger.info(f"\n  Your SR global compatibility level: {level}")
    except RuntimeError as exc:
        logger.info(f"\n  Could not fetch global compatibility: {exc}")

    rules = [
        ("BACKWARD",             "New schema reads data from old schema."),
        ("BACKWARD_TRANSITIVE",  "BACKWARD vs ALL previous versions. ← Recommended for Protobuf"),
        ("FORWARD",              "Old schema reads data from new schema."),
        ("FORWARD_TRANSITIVE",   "FORWARD vs all previous versions."),
        ("FULL",                 "BACKWARD + FORWARD."),
        ("FULL_TRANSITIVE",      "BACKWARD_TRANSITIVE + FORWARD_TRANSITIVE."),
        ("NONE",                 "No compatibility checking."),
    ]
    logger.info("")
    for name, desc in rules:
        logger.info(f"  {name:<26} {desc}")

    logger.info("""
  ┌────────────────────────────────────────────────────────────────────┐
  │  Key Protobuf difference from Avro: adding a new *message type*    │
  │  (not just a field) breaks FORWARD compatibility.                  │
  │  → Default to BACKWARD_TRANSITIVE for Protobuf schemas.            │
  └────────────────────────────────────────────────────────────────────┘""")


# ── Demo 7 ─────────────────────────────────────────────────────────────────
def demo_types(sr: SchemaRegistryClient) -> None:
    section("7 · Supported Schema Types & Specific vs Generic Return Types")

    try:
        types = sr.get_types()
        logger.info(f"\n  Schema types on your SR: {types}")
    except RuntimeError as exc:
        logger.info(f"\n  Could not fetch types: {exc}")

    logger.info("""
  ┌──────────────┬──────────────────────────────────┬──────────────────────────────────┐
  │              │  Specific type                   │  Generic / Dynamic type          │
  ├──────────────┼──────────────────────────────────┼──────────────────────────────────┤
  │  Avro        │  SpecificRecord subclass          │  org.apache.avro.GenericRecord  │
  │  Protobuf    │  Message subclass (ProtoMessage)  │  DynamicMessage → dict (Python) │
  │  JSON Schema │  Jackson-compatible class         │  JsonNode → dict (Python)       │
  └──────────────┴──────────────────────────────────┴──────────────────────────────────┘

  Python config equivalents:
    specific.protobuf.value.type = MyRecord  →  specific_type=my_proto_msg
    derive.type = true                       →  derive_type=True
    (neither)                                →  returns plain dict  (DynamicMessage)

  latest.cache.size / latest.cache.ttl.sec  →  handled by self._cache in this demo
""")


# ── Demo 8 ─────────────────────────────────────────────────────────────────
def demo_strategies(sr: SchemaRegistryClient, run_id: str, save_dir: str = "") -> None:
    section("8 · Subject Name Strategies")

    schema = ProtoMessage(
        name="Payment",
        fields=[ProtoField("amount", "float", 1), ProtoField("currency", "string", 2)],
    )
    topic = f"payments-{run_id}"

    if save_dir:
        path = schema.save_schema(save_dir)
        logger.info(f"  Saved → {path}")

    configs: list[tuple[str, str]] = [
        ("TopicNameStrategy",       f"{topic}-value"),
        ("RecordNameStrategy",      schema.name),
        ("TopicRecordNameStrategy", f"{topic}-{schema.name}"),
    ]

    logger.info(f"\n  Schema: '{schema.name}'   Topic: '{topic}'\n")
    for strategy, expected_subject in configs:
        sid = sr.register(expected_subject, schema.to_schema_string())
        logger.info(f"  {strategy:<32} → subject='{expected_subject}' (id={sid})")

    logger.info("""
  Reference subject name strategies:
    DefaultReferenceSubjectNameStrategy   → subject = import path (e.g. "other.proto")
    QualifiedReferenceSubjectNameStrategy → subject = dotted     (e.g. "mypackage.myfile")
""")


# ── Demo 9 ─────────────────────────────────────────────────────────────────
def demo_csfle(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, aws_kms_key_arn: str, save_dir: str = "") -> None:
    section("9 · Client-Side Field Level Encryption (CSFLE)")

    # ── 1. Define a schema with sensitive fields ──────────────────────
    sensitive_record = ProtoMessage(
        name="SensitiveRecord",
        fields=[
            ProtoField("id",    "string", 1),
            ProtoField("name",  "string", 2),
            ProtoField("ssn",   "string", 3),
            ProtoField("email", "string", 4),
            ProtoField("amount", "float", 5),
        ],
    )

    logger.info("\nSchema with sensitive fields:")
    logger.info(sensitive_record.to_schema_string())

    if save_dir:
        path = sensitive_record.save_schema(save_dir)
        logger.info(f"  Saved → {path}")

    # ── 2. CSFLE metadata: tag fields for encryption ──────────────────
    metadata = {
        "properties": {
            "SensitiveRecord.ssn.tags":   "PII",
            "SensitiveRecord.email.tags": "PII",
        }
    }

    kek_name = f"demo-kek-{run_id}"

    rule_set = {
        "domainRules": [
            {
                "name":  "encryptPII",
                "kind":  "TRANSFORM",
                "type":  "ENCRYPT",
                "mode":  "WRITEREAD",
                "tags":  ["PII"],
                "params": {
                    "encrypt.kek.name":   kek_name,
                    "encrypt.kms.type":   "aws-kms",
                    "encrypt.kms.key.id": aws_kms_key_arn,
                },
            }
        ]
    }

    logger.info(f"\nMetadata (field tags):\n{json.dumps(metadata, indent=2)}")
    logger.info(f"\nRuleSet (encryption rules):\n{json.dumps(rule_set, indent=2)}")

    tagged_fields = get_encrypted_fields(metadata, rule_set)
    logger.info(f"\nFields targeted for encryption: {tagged_fields}")

    # ── 3. Register KEK in the DEK Registry and create FieldEncryptor ─
    try:
        sr.create_kek(kek_name, "aws-kms", aws_kms_key_arn)
    except RuntimeError:
        logger.info(f"  KEK '{kek_name}' already exists — reusing")

    encryptor = FieldEncryptor(sr, kek_name=kek_name, kms_key_id=aws_kms_key_arn)

    topic = f"csfle-{run_id}"

    # Serializer WITH encryption
    ser = KafkaProtobufSerializer(sr, field_encryptor=encryptor)
    # Deserializer WITHOUT encryption (reads ciphertext)
    deser_raw = KafkaProtobufDeserializer(sr, specific_type=sensitive_record)
    # Deserializer WITH encryption (auto-decrypts)
    deser_dec = KafkaProtobufDeserializer(
        sr, specific_type=sensitive_record, field_encryptor=encryptor,
    )

    # ── 4. Serialize (encrypts PII fields) ────────────────────────────
    original = {
        "id":     "user-001",
        "name":   "Alice Smith",
        "ssn":    "123-45-6789",
        "email":  "alice@example.com",
        "amount": 500.0,
    }
    logger.info(f"\nOriginal data:  {original}")

    wire = ser.serialize(
        topic, sensitive_record, original,
        metadata=metadata, rule_set=rule_set,
    )
    logger.info(f"Wire bytes:     {len(wire)} bytes")

    # ── 5. Deserialize WITHOUT decryption → encrypted values visible ──
    encrypted_view = deser_raw.deserialize(wire)
    logger.info(f"\nWithout decryption: {encrypted_view}")
    logger.info("  ↑ 'ssn' and 'email' are AES-256-GCM ciphertext (base64)")

    # ── 6. Deserialize WITH decryption → plaintext restored ───────────
    decrypted_view = deser_dec.deserialize(wire)
    logger.info(f"\nWith decryption:    {decrypted_view}")
    logger.info("  ↑ 'ssn' and 'email' restored to plaintext")

    # ── 7. Kafka round-trip (if --mode full) ──────────────────────────
    if kafka_cfg:
        kafka_produce(kafka_cfg, topic, "user-001", wire)
        raw = kafka_consume_one(kafka_cfg, topic, f"demo-csfle-{run_id}")
        if raw:
            enc = deser_raw.deserialize(raw)
            dec = deser_dec.deserialize(raw)
            logger.info(f"\nKafka (encrypted): {enc}")
            logger.info(f"Kafka (decrypted): {dec}")

    # ── 8. Key architecture notes ─────────────────────────────────────
    logger.info("""
  ┌────────────────────────────────────────────────────────────────────┐
  │  CSFLE Architecture (AWS KMS)                                      │
  │                                                                    │
  │  Schema Registry stores:                                           │
  │    • schema   — Protobuf definition (original field types)         │
  │    • metadata — field-level tags  (e.g. "ssn" → PII)               │
  │    • ruleSet  — ENCRYPT transform rules matching tags              │
  │                                                                    │
  │  DEK Registry (part of Schema Registry) stores:                    │
  │    • KEK metadata — name, KMS type, KMS key ARN                    │
  │    • DEK entries  — encrypted data keys per subject                │
  │                                                                    │
  │  AWS KMS provides:                                                 │
  │    • KEK management — symmetric encrypt/decrypt key                │
  │    • DEK unwrapping — boto3 kms.decrypt() for cached DEKs          │
  │                                                                    │
  │  Flow:                                                             │
  │    1. Register KEK in DEK Registry (name + ARN)                    │
  │    2. First encrypt → create_dek() → plaintext DEK returned        │
  │    3. Subsequent → get_dek() → encrypted DEK → KMS decrypt         │
  └────────────────────────────────────────────────────────────────────┘""")
