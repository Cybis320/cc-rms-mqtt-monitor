"""MQTT publishing: plain-JSON state + Home Assistant discovery + LWT.

Topic layout (with default prefixes):

    contrailcast/rms/<host>/status            retained, "online"/"offline" (LWT)
    contrailcast/rms/<station>/health         retained, JSON state blob
    homeassistant/<component>/<station>/<key>/config   retained discovery

The host status topic doubles as the HA availability source, so every entity
flips to "unavailable" if this agent crashes or the host goes offline.
"""

import json

import paho.mqtt.client as mqtt

from . import hadiscovery


def _make_client(client_id):
    """Construct a paho Client across the 1.x / 2.x API split."""
    try:
        # paho-mqtt >= 2.0
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


class Publisher:
    def __init__(self, config):
        self.config = config
        self.host_status_topic = "%s/%s/status" % (
            config.topic_prefix, config.host_name)
        self.client = _make_client(
            "%s-%s" % (config.broker.client_id_prefix, config.host_name))
        if config.broker.username:
            self.client.username_pw_set(
                config.broker.username, config.broker.password)
        if config.broker.tls:
            self.client.tls_set()
        # Last Will: if we drop without a clean disconnect, broker marks us down.
        self.client.will_set(self.host_status_topic, "offline", qos=1, retain=True)
        self._discovery_sent = set()

    def connect(self):
        self.client.connect(
            self.config.broker.host,
            self.config.broker.port,
            keepalive=self.config.broker.keepalive,
        )
        self.client.loop_start()
        self.client.publish(self.host_status_topic, "online", qos=1, retain=True)

    def _state_topic(self, station_id):
        return "%s/%s/health" % (self.config.topic_prefix, station_id)

    def _send_discovery(self, station_id):
        if not self.config.ha_discovery_enabled:
            return
        if station_id in self._discovery_sent:
            return
        for topic, payload in hadiscovery.discovery_messages(
            station_id,
            self._state_topic(station_id),
            self.host_status_topic,
            self.config.ha_discovery_prefix,
        ):
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self._discovery_sent.add(station_id)

    def publish_state(self, state):
        station_id = state["station_id"]
        self._send_discovery(station_id)
        self.client.publish(
            self._state_topic(station_id),
            json.dumps(state, default=str),
            qos=1,
            retain=True,
        )

    def disconnect(self):
        # Clean shutdown: explicitly mark offline before closing.
        self.client.publish(self.host_status_topic, "offline", qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()
