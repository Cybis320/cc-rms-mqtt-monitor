"""MQTT publishing: retained plain-JSON state + host Last-Will.

Topic layout (with default prefix):

    stations/<host>/status      retained, "online"/"offline" (LWT)
    stations/<host>/health      retained, JSON host (OS) state blob
    stations/<station>/health   retained, JSON per-station state blob
"""

import json

import paho.mqtt.client as mqtt


def _make_client(client_id):
    """Construct a paho Client across the 1.x / 2.x API split."""
    try:
        # paho-mqtt >= 2.0
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


class Publisher:
    def __init__(self, config, announce=True):
        # announce=False: a transient publisher (e.g. --test) that must NOT touch
        # the host status topic or collide with the running service -- so no
        # Last-Will, no online/offline, and a distinct client id.
        self.config = config
        self.announce = announce
        self.host_status_topic = "%s/%s/status" % (
            config.topic_prefix, config.host_name)
        client_id = "%s-%s" % (config.broker.client_id_prefix, config.host_name)
        if not announce:
            client_id += "-test"
        self.client = _make_client(client_id)
        if config.broker.username:
            self.client.username_pw_set(
                config.broker.username, config.broker.password)
        if config.broker.tls:
            self.client.tls_set()
        if announce:
            # Last Will: if we drop uncleanly, the broker marks us down.
            self.client.will_set(self.host_status_topic, "offline", qos=1, retain=True)
            # Re-assert "online" on every (re)connect (see _on_connect).
            self.client.on_connect = self._on_connect
        self._pending = []

    def _on_connect(self, client, userdata, flags, rc, *args):
        # Republish "online" on every successful (re)connect, so the status topic
        # is correct again after a reconnect -- the broker will have published our
        # Last-Will "offline" while we were gone. (Retained health messages
        # survive on the broker and refresh next cycle, so nothing else needs
        # re-sending; HA discovery was removed.)
        if rc == 0:
            client.publish(self.host_status_topic, "online", qos=1, retain=True)

    def connect(self):
        self.client.connect(
            self.config.broker.host,
            self.config.broker.port,
            keepalive=self.config.broker.keepalive,
        )
        self.client.loop_start()
        # "online" is published by _on_connect, which fires on this initial
        # connect and on every automatic reconnect.

    def _state_topic(self, station_id):
        return "%s/%s/health" % (self.config.topic_prefix, station_id)

    def _host_state_topic(self):
        return "%s/%s/health" % (self.config.topic_prefix, self.config.host_name)

    def _publish(self, topic, payload, retain=True):
        """Publish QoS-1 and track the message so flush() can confirm delivery."""
        info = self.client.publish(topic, payload, qos=1, retain=retain)
        self._pending.append(info)
        return info

    def flush(self, timeout=10.0):
        """Block until every queued message has actually been sent to the broker.

        Essential for --once and clean shutdown: without it, loop_start()'s
        background thread may be torn down before the QoS-1 messages leave.
        """
        for info in self._pending:
            try:
                info.wait_for_publish(timeout)
            except (ValueError, RuntimeError):
                pass
        self._pending = []

    def publish_state(self, state):
        station_id = state["station_id"]
        self._publish(self._state_topic(station_id), json.dumps(state, default=str))

    def publish_host_state(self, state):
        self._publish(self._host_state_topic(), json.dumps(state, default=str))

    def publish_test(self, state):
        # Non-retained so it doesn't linger on the broker; routed by payload.
        self._publish(self._state_topic(state["station_id"]),
                      json.dumps(state, default=str), retain=False)
        self.flush()

    def disconnect(self):
        self.flush()
        # Only the announcing (long-running) publisher owns the host status.
        if self.announce:
            self._publish(self.host_status_topic, "offline")
            self.flush()
        self.client.loop_stop()
        self.client.disconnect()
