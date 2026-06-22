"""MQTT publishing: retained plain-JSON state + host Last-Will.

Topic layout (with default prefix):

    stations/<host>/status      retained, "online"/"offline" (LWT)
    stations/<host>/health      retained, JSON host (OS) state blob
    stations/<station>/health   retained, JSON per-station state blob
"""

import copy
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger("cc_mqtt_monitor")


def _make_client(client_id, transport="tcp"):
    """Construct a paho Client across the 1.x / 2.x API split."""
    try:
        # paho-mqtt >= 2.0
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=client_id,
                           transport=transport)
    except (AttributeError, TypeError):
        # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id, transport=transport)


class Publisher:
    def __init__(self, config, announce=True):
        # announce=False: a transient publisher (e.g. --test) that must NOT touch
        # the host status topic or collide with the running service -- so no
        # Last-Will, no online/offline, and a distinct client id.
        self.config = config
        self.announce = announce
        self.host_status_topic = "%s/%s/status" % (
            config.topic_prefix, config.host_name)
        self._client_id = "%s-%s" % (config.broker.client_id_prefix, config.host_name)
        if not announce:
            self._client_id += "-test"
        self._pending = []
        self._winning_broker = None   # set to a fallback endpoint once one works
        self._build_client(config.broker)

    def _build_client(self, broker):
        """(Re)build the paho client for a given broker endpoint (transport/TLS/
        WebSocket path all depend on it), re-applying the Last-Will + on_connect."""
        self.client = _make_client(self._client_id, broker.transport)
        if broker.transport == "websockets":
            # Path of the broker's WebSocket listener; lets MQTT ride 443 like HTTPS.
            self.client.ws_set_options(path=broker.ws_path)
        if broker.username:
            self.client.username_pw_set(broker.username, broker.password)
        if broker.tls:
            self.client.tls_set()
        if self.announce:
            # Last Will: if we drop uncleanly, the broker marks us down.
            self.client.will_set(self.host_status_topic, "offline", qos=1, retain=True)
            # Re-assert "online" on every (re)connect (see _on_connect).
            self.client.on_connect = self._on_connect

    def _fallback_brokers(self):
        """Broker variants for each configured fallback endpoint (same host/creds),
        tried in order when the primary fails (e.g. wss/443, then mqtts/8883)."""
        out = []
        for f in (self.config.broker.fallbacks or []):
            b = copy.copy(self.config.broker)
            b.transport = f.get("transport", "tcp")
            b.port = f.get("port")
            b.tls = f.get("tls", False)
            b.ws_path = f.get("ws_path", b.ws_path)
            out.append(b)
        return out

    @staticmethod
    def _endpoint_label(b):
        return "%s:%s/%s%s" % (b.host, b.port, b.transport, "+tls" if b.tls else "")

    def _on_connect(self, client, userdata, flags, rc, *args):
        # Republish "online" on every successful (re)connect, so the status topic
        # is correct again after a reconnect -- the broker will have published our
        # Last-Will "offline" while we were gone. (Retained health messages
        # survive on the broker and refresh next cycle, so nothing else needs
        # re-sending; HA discovery was removed.)
        if rc == 0:
            client.publish(self.host_status_topic, "online", qos=1, retain=True)

    def _connect_to(self, broker):
        self.client.connect(broker.host, broker.port, keepalive=broker.keepalive)
        self.client.loop_start()
        # "online" is published by _on_connect, which fires on this initial
        # connect and on every automatic reconnect.

    def connect(self):
        """Connect, trying fallback endpoints in order if the primary fails.

        Lets a station behind a firewall (e.g. a school) that blocks 1883 connect
        with no config change -- via wss/443 or mqtts/8883. The first endpoint
        that works sticks for the session, so a transient primary hiccup doesn't
        lock a normal station onto a fallback, and a confirmed fallback isn't
        re-probed every cycle."""
        if self._winning_broker is not None:
            self._build_client(self._winning_broker)
            self._connect_to(self._winning_broker)
            return

        attempts = [self.config.broker]
        if self.config.broker.auto_fallback and self.config.broker.transport == "tcp":
            attempts += self._fallback_brokers()

        last_exc = None
        for i, b in enumerate(attempts):
            self._build_client(b)
            try:
                self._connect_to(b)
                if i > 0:   # a fallback won -> remember it for the session
                    self._winning_broker = b
                    log.info("Connected via fallback %s; using it for this session",
                             self._endpoint_label(b))
                return
            except Exception as exc:
                last_exc = exc
                more = "trying next" if i < len(attempts) - 1 else "no endpoints left"
                log.warning("Broker connect %s failed: %s; %s",
                            self._endpoint_label(b), exc, more)
        raise last_exc            # all endpoints failed -> caller retries/backs off

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

    def publish_test_host(self, state):
        # A host-level test alert -> the host health topic, non-retained so the
        # real retained host record is left untouched.
        self._publish(self._host_state_topic(),
                      json.dumps(state, default=str), retain=False)
        self.flush()

    def clear_station(self, station_id):
        """Remove a station's retained record (empty retained payload), e.g. when
        it is newly opted out of publishing so its old data doesn't linger."""
        self._publish(self._state_topic(station_id), "", retain=True)
        self.flush()

    def go_silent(self, station_ids):
        """Wipe everything this host published (status, host record, the given
        station records) and disconnect cleanly WITHOUT an 'offline' marker -- so
        a fully opted-out host leaves nothing at all on the broker."""
        self._publish(self.host_status_topic, "", retain=True)
        self._publish(self._host_state_topic(), "", retain=True)
        for sid in station_ids:
            self._publish(self._state_topic(sid), "", retain=True)
        self.flush()
        self.client.loop_stop()
        self.client.disconnect()   # clean disconnect -> Last-Will does not fire

    def disconnect(self):
        self.flush()
        # Only the announcing (long-running) publisher owns the host status.
        if self.announce:
            self._publish(self.host_status_topic, "offline")
            self.flush()
        self.client.loop_stop()
        self.client.disconnect()
