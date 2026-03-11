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
def demo_basic(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("1 · Basic Protobuf Serializer & Deserializer")

    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        other_record = load_compiled_message("other.proto", "OtherRecord")
        my_record = load_compiled_message("MyRecord.proto", "MyRecord")
    else:
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
def demo_evolution(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("3 · Schema Evolution (Backward Compatibility)")

    subject = f"transactions-proto-{run_id}-value"
    topic   = f"transactions-proto-{run_id}"

    # Schema evolution requires two versions of the same message name ('MyRecord')
    # in one process.  protoc-generated stubs register in a global descriptor pool
    # that rejects duplicate names, so we always use the dynamic approach here.
    if use_protoc:
        logger.info("  (using dynamic descriptors — protoc cannot load two 'MyRecord' versions)")

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
        if not use_protoc:
            v1.file_name = "MyRecord_v1.proto"
            v2.file_name = "MyRecord_v2.proto"
        for msg in (v1, v2):
            # For protoc mode, file_name already includes "evolution/" prefix,
            # so save directly to save_dir to avoid double nesting
            dest = save_dir if use_protoc else str(evo_dir)
            path = msg.save_schema(dest)
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
def demo_oneof(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("4 · Multiple Event Types in the Same Topic (oneOf)")

    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        customer  = load_compiled_message("Customer.proto", "Customer")
        product   = load_compiled_message("Product.proto", "Product")
        order     = load_compiled_message("Order.proto", "Order")
        all_types = load_compiled_message("AllTypes.proto", "AllTypes")
    else:
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
def demo_null_handling(sr: SchemaRegistryClient, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("5 · Null-Value Handling with Optional Fields (recommended)")

    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        schema = load_compiled_message("ExampleMessage.proto", "ExampleMessage")
    else:
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
def demo_strategies(sr: SchemaRegistryClient, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("8 · Subject Name Strategies")

    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        schema = load_compiled_message("Payment.proto", "Payment")
    else:
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
def demo_csfle(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, aws_kms_key_arn: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("9 · Client-Side Field Level Encryption (CSFLE) — Confluent Native")

    import os
    from confluent_kafka.schema_registry import SchemaRegistryClient as ConfluentSRClient
    from confluent_kafka.schema_registry.protobuf import ProtobufSerializer, ProtobufDeserializer
    from confluent_kafka.schema_registry.rules.encryption.awskms.aws_driver import AwsKmsDriver
    from confluent_kafka.schema_registry.rules.encryption.encrypt_executor import FieldEncryptionExecutor
    from confluent_kafka.serialization import MessageField, SerializationContext
    from google.protobuf.json_format import ParseDict, MessageToDict

    # ── 1. Register Confluent CSFLE drivers ───────────────────────────
    AwsKmsDriver.register()
    FieldEncryptionExecutor.register()

    # ── 2. Define a schema with sensitive fields ───────────────────────
    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        sensitive_record = load_compiled_message("SensitiveRecord.proto", "SensitiveRecord")
    else:
        sensitive_record = ProtoMessage(
            name="SensitiveRecord",
            fields=[
                ProtoField("id",     "string", 1),
                ProtoField("name",   "string", 2),
                ProtoField("ssn",    "string", 3, tags="PII"),
                ProtoField("email",  "string", 4, tags="PII"),
                ProtoField("amount", "float",  5),
            ],
        )

    logger.info("\nSchema with sensitive fields:")
    logger.info(sensitive_record.to_schema_string())

    if save_dir:
        path = sensitive_record.save_schema(save_dir)
        logger.info(f"  Saved → {path}")

    kek_name = f"demo-kek-{run_id}"
    topic    = f"csfle-{run_id}"
    subject  = f"{topic}-value"

    # ── 3. CSFLE rule set: tags are embedded in the schema via
    #       (confluent.field_meta).tags = "PII", so no separate metadata needed.
    rule_set = {
        "domainRules": [
            {
                "name":      "encrypt-pii",
                "kind":      "TRANSFORM",
                "type":      "ENCRYPT",
                "mode":      "WRITEREAD",
                "tags":      ["PII"],
                "params": {
                    "encrypt.kek.name":   kek_name,
                    "encrypt.kms.type":   "aws-kms",
                    "encrypt.kms.key.id": aws_kms_key_arn,
                },
                "onFailure": "ERROR,NONE",
                "disabled":  False,
            }
        ]
    }

    logger.info(f"\nRuleSet (encryption rules):\n{json.dumps(rule_set, indent=2)}")

    # ── 4. Ensure PII tag exists, then register KEK + schema with rules ──
    sr.create_tag("PII")

    try:
        sr.create_kek(kek_name, "aws-kms", aws_kms_key_arn)
        logger.info(f"  KEK '{kek_name}' created")
    except RuntimeError:
        logger.info(f"  KEK '{kek_name}' already exists — reusing")

    sr.register(subject, sensitive_record.to_schema_string(), rule_set=rule_set)

    # ── 5. Build Confluent native SR client for ProtobufSerializer ─────
    confluent_sr = ConfluentSRClient({
        'url': os.environ['SCHEMA_REGISTRY_URL'],
        'basic.auth.user.info': f"{os.environ['SR_API_KEY']}:{os.environ['SR_API_SECRET']}",
    })

    # ── 6. Get protobuf message class ─────────────────────────────────
    msg_cls = sensitive_record._message_class if use_protoc else sensitive_record.message_class()

    # ── 7. Create Confluent native serializer / deserializer ──────────
    # Schema is already registered — fetch latest and apply its rules
    ser_conf = {
        'auto.register.schemas': False,
        'use.latest.version':    True,
        'use.deprecated.format': False,
    }
    protobuf_serializer   = ProtobufSerializer(msg_cls, confluent_sr, ser_conf)
    protobuf_deserializer = ProtobufDeserializer(msg_cls, {'use.deprecated.format': False}, confluent_sr)

    # ── 8. Serialize — FieldEncryptionExecutor encrypts PII fields ────
    original = {
        "id":     "user-001",
        "name":   "Alice Smith",
        "ssn":    "123-45-6789",
        "email":  "alice@example.com",
        "amount": 500.0,
    }

    logger.info(f"\nOriginal data:  {original}")

    ser_ctx      = SerializationContext(topic, MessageField.VALUE)
    msg_instance = ParseDict(original, msg_cls())
    wire         = protobuf_serializer(msg_instance, ser_ctx)
    logger.info(f"Wire bytes:     {len(wire)} bytes  (ssn + email AES-256-GCM encrypted)")

    # ── 9. Deserialize — FieldEncryptionExecutor decrypts PII fields ──
    deser_ctx      = SerializationContext(topic, MessageField.VALUE)
    decrypted_msg  = protobuf_deserializer(wire, deser_ctx)
    decrypted_view = MessageToDict(decrypted_msg, preserving_proto_field_name=True)
    logger.info(f"\nDecrypted:      {decrypted_view}")
    logger.info("  ↑ 'ssn' and 'email' restored to plaintext by WRITEREAD rule")

    # ── 10. Kafka round-trip (if --mode full) ─────────────────────────
    if kafka_cfg:
        kafka_produce(kafka_cfg, topic, "user-001", wire)
        raw = kafka_consume_one(kafka_cfg, topic, f"demo-csfle-{run_id}")
        if raw:
            dec = protobuf_deserializer(raw, SerializationContext(topic, MessageField.VALUE))
            logger.info(f"\nKafka decrypted: {MessageToDict(dec, preserving_proto_field_name=True)}")

    # ── 11. Architecture notes ────────────────────────────────────────
    logger.info("""
  ┌────────────────────────────────────────────────────────────────────┐
  │  Confluent CSFLE Architecture (AWS KMS)                            │
  │                                                                    │
  │  Schema Registry stores:                                           │
  │    • schema   — Protobuf definition (original field types)         │
  │    • metadata — field-level tags  (e.g. "ssn" → PII)               │
  │    • ruleSet  — ENCRYPT WRITEREAD domain rule matching PII tags    │
  │                                                                    │
  │  ProtobufSerializer + FieldEncryptionExecutor (on write):          │
  │    1. Fetch schema + rules from SR (use.latest.version=true)       │
  │    2. Identify PII-tagged fields from schema metadata              │
  │    3. Encrypt field values via AWS KMS + DEK Registry              │
  │    4. Serialize to Confluent wire-format bytes                     │
  │                                                                    │
  │  ProtobufDeserializer + FieldEncryptionExecutor (on read):         │
  │    1. Strip Confluent wire-format header → schema_id               │
  │    2. Fetch schema + WRITEREAD rules from SR                       │
  │    3. Decrypt field values via AWS KMS + DEK Registry              │
  │    4. Return decrypted protobuf message                            │
  └────────────────────────────────────────────────────────────────────┘""")


# ── Demo 10 ────────────────────────────────────────────────────────────────
def demo_no_auto_register(sr: SchemaRegistryClient, kafka_cfg: dict | None, run_id: str, save_dir: str = "", use_protoc: bool = False) -> None:
    section("10 · Manual Schema Registration (auto_register=False)")

    if use_protoc:
        from compiled_protobuf_helpers import load_compiled_message
        invoice = load_compiled_message("Invoice.proto", "Invoice")
    else:
        invoice = ProtoMessage(
            name="Invoice",
            fields=[
                ProtoField("invoice_id", "string", 1),
                ProtoField("vendor",     "string", 2),
                ProtoField("total",      "float",  3),
            ],
        )

    logger.info("\nSchema:")
    logger.info(invoice.to_schema_string())

    if save_dir:
        path = invoice.save_schema(save_dir)
        logger.info(f"  Saved → {path}")

    topic   = f"invoices-{run_id}"
    subject = f"{topic}-value"

    # ── Step 1: Manually register the schema ───────────────────────────
    logger.info(f"\n[Step 1] Manually registering schema under subject '{subject}' …")
    schema_id = sr.register(subject, invoice.to_schema_string())
    logger.info(f"  Registered → schema_id={schema_id}")

    # ── Step 2: Create serializer with auto_register=False ─────────────
    logger.info("\n[Step 2] Creating serializer with auto_register=False …")
    ser = KafkaProtobufSerializer(sr, auto_register=False)

    data = {"invoice_id": "INV-2026-001", "vendor": "Acme Corp", "total": 1250.99}
    logger.info(f"  Serializing: {data}")

    wire = ser.serialize(topic, invoice, data)
    logger.info(f"  Wire bytes: {len(wire)} bytes")

    # ── Step 3: Deserialize to verify round-trip ───────────────────────
    deser   = KafkaProtobufDeserializer(sr, specific_type=invoice)
    decoded = deser.deserialize(wire)

    if kafka_cfg:
        kafka_produce(kafka_cfg, topic, "inv-001", wire)
        raw = kafka_consume_one(kafka_cfg, topic, f"demo-no-auto-{run_id}")
        decoded = deser.deserialize(raw) if raw else decoded
        logger.info(f"\n  Consumed from Kafka: {decoded}")
    else:
        logger.info(f"\n  Decoded (wire-format only): {decoded}")

    # ── Step 4: Show that unregistered subjects fail ───────────────────
    logger.info("\n[Step 3] Demonstrating failure when subject is not pre-registered …")
    unknown_topic = f"unknown-{run_id}"
    ser_no_auto   = KafkaProtobufSerializer(sr, auto_register=False)
    try:
        ser_no_auto.serialize(unknown_topic, invoice, data)
        logger.info("  (serialize succeeded unexpectedly)")
    except RuntimeError as exc:
        logger.info(f"  Expected error: {exc}")

    logger.info("""
  ┌────────────────────────────────────────────────────────────────────┐
  │  auto_register=False                                               │
  │                                                                    │
  │  When disabled, the serializer does NOT register schemas.          │
  │  Instead it calls GET /subjects/{subject}/versions/latest to       │
  │  look up the schema_id for an already-registered schema.           │
  │                                                                    │
  │  Use this in production when:                                      │
  │    • Schemas are managed via CI/CD pipelines                       │
  │    • Producers should not have write access to Schema Registry     │
  │    • You want strict governance over schema changes                │
  └────────────────────────────────────────────────────────────────────┘""")
