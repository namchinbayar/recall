"""Confluent Cloud clients for the events_raw topic.

The topic and its Avro schemas were created by Flink (CREATE TABLE events_raw),
so the serializers never register schemas: they use the latest registered
version of events_raw-key and events_raw-value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from confluent_kafka import Consumer, Producer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer, AvroSerializer
from confluent_kafka.serialization import MessageField, SerializationContext

from .. import config  # noqa: F401  (loads the env file)

SECRET_KEYS = ("KAFKA_API_KEY", "KAFKA_API_SECRET", "SCHEMA_REGISTRY_API_KEY", "SCHEMA_REGISTRY_API_SECRET")


@dataclass(frozen=True)
class KafkaSettings:
    bootstrap: str
    api_key: str
    api_secret: str
    sr_url: str
    sr_key: str
    sr_secret: str
    topic: str = "events_raw"
    inbox_topic: str = "events_inbox"
    digest_topic: str = "events_digest"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Run `recall stream setup` first.")
    return value


def load() -> KafkaSettings:
    return KafkaSettings(
        bootstrap=_require("KAFKA_BOOTSTRAP"),
        api_key=_require("KAFKA_API_KEY"),
        api_secret=_require("KAFKA_API_SECRET"),
        sr_url=_require("SCHEMA_REGISTRY_URL"),
        sr_key=_require("SCHEMA_REGISTRY_API_KEY"),
        sr_secret=_require("SCHEMA_REGISTRY_API_SECRET"),
        topic=os.environ.get("EVENTS_TOPIC", "events_raw"),
        inbox_topic=os.environ.get("EVENTS_INBOX_TOPIC", "events_inbox"),
        digest_topic=os.environ.get("EVENTS_DIGEST_TOPIC", "events_digest"),
    )


def _client_conf(ks: KafkaSettings) -> dict:
    return {
        "bootstrap.servers": ks.bootstrap,
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "PLAIN",
        "sasl.username": ks.api_key,
        "sasl.password": ks.api_secret,
    }


def _registry(ks: KafkaSettings) -> SchemaRegistryClient:
    return SchemaRegistryClient({"url": ks.sr_url, "basic.auth.user.info": f"{ks.sr_key}:{ks.sr_secret}"})


class EventProducer:
    """Serializes event dicts against the registered events_raw schemas and produces them."""

    def __init__(self, ks: KafkaSettings):
        self.topic = ks.topic
        registry = _registry(ks)
        conf = {"auto.register.schemas": False, "use.latest.version": True}
        self._key = AvroSerializer(registry, conf=conf)
        self._value = AvroSerializer(registry, conf=conf)
        self._producer = Producer(_client_conf(ks) | {"client.id": "recall-producer", "linger.ms": 200})

    def send(self, event: dict) -> None:
        key = self._key({"event_id": event["event_id"]}, SerializationContext(self.topic, MessageField.KEY))
        value = self._value(event, SerializationContext(self.topic, MessageField.VALUE))
        self._producer.produce(self.topic, key=key, value=value, on_delivery=_report)
        self._producer.poll(0)

    def flush(self, timeout: float = 10) -> int:
        return self._producer.flush(timeout)


def _report(err, msg) -> None:
    if err is not None:
        print(f"delivery failed: {err}", flush=True)


def consumer(ks: KafkaSettings, group: str) -> tuple[Consumer, AvroDeserializer]:
    """A consumer with manual commits on events_raw and events_digest (Avro) and events_inbox
    (plain JSON), plus an Avro deserializer."""
    c = Consumer(_client_conf(ks) | {
        "group.id": group,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
    })
    c.subscribe([ks.topic, ks.inbox_topic, ks.digest_topic])
    return c, AvroDeserializer(_registry(ks))
