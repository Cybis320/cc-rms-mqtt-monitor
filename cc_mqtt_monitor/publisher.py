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
        self._using_fallback = False
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

    def _fallback_broker(self):
        """A WebSocket(/TLS)-on-fallback_port variant of the primary broker, for
        networks that block 1883 but allow 443 (WSS looks like HTTPS)."""
        b = copy.copy(self.config.broker)
        b.transport = "websockets"
        b.port = self.config.broker.fallback_port
        b.tls = self.config.broker.fallback_tls
        b.ws_path = self.config.broker.fallback_ws_path
        return b

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
        """Connect, auto-falling back to WebSockets/443 if the primary fails.

        A station behind a firewall that blocks 1883 but allows 443 connects with
        no config change. We stick to the fallback for the rest of the session
        only after it actually succeeds, so a transient 1883 hiccup doesn't lock a
        normal station onto WSS."""
        if self._using_fallback:
            self._build_client(self._fallback_broker())
            self._connect_to(self._fallback_broker())
            return

        primary = self.config.broker
        self._build_client(primary)
        try:
            self._connect_to(primary)
            return
        except Exception as exc:
            if not (primary.auto_fallback and primary.transport == "tcp"):
                raise
            log.warning("Broker connect to %s:%d (%s) failed: %s; trying WebSocket "
                        "fallback on :%d", primary.host, primary.port,
                        primary.transport, exc, primary.fallback_port)

        fb = self._fallback_broker()
        self._build_client(fb)
        self._connect_to(fb)            # raises -> propagates to caller's retry/backoff
        self._using_fallback = True     # stick only after a successful WSS connect
        log.info("Connected to %s:%d over WebSockets; using it for this session",
                 fb.host, fb.port)

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
