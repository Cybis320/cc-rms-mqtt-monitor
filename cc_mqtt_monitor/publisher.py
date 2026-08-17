"""MQTT publishing: retained plain-JSON state + host Last-Will.

Topic layout (with default prefix):

    stations/<host>/status      retained, "online"/"offline" (LWT)
    stations/<host>/health      retained, JSON host (OS) state blob
    stations/<station>/health   retained, JSON per-station state blob
"""

import binascii
import copy
import json
import logging
import os
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("cc_mqtt_monitor")

# Cap on messages paho may hold while the broker is unreachable. These are RETAINED
# STATE SNAPSHOTS, not events: only the newest matters, so replaying a backlog is
# worthless -- and harmful. A 5.4-day server outage left one host flushing ~72 MB of
# week-old snapshots on reconnect, drowning its current state; the dashboard showed it
# offline for hours while the station was capturing normally. Bounded, an outage of any
# length costs the same handful of superseded snapshots.
MAX_QUEUED_MESSAGES = 200

# Where the per-install client-id suffix lives (see _instance_suffix).
CLIENT_ID_FILE = "/var/lib/cc-rms-monitor/client_suffix"


def _instance_suffix():
    """A short id that is stable for this install and unique across machines.

    MQTT client ids must be unique: a second client connecting with an id already in
    use gets the incumbent kicked off, so two such hosts evict each other forever. The
    id was host_name-derived, and hostnames are NOT unique in this fleet -- four
    machines that kept the default `raspberrypi` fought at ~5 reconnects/second, each
    eviction re-publishing the LWT and the retained status topic.

    Deliberately NOT /etc/machine-id: the fleet is imaged from a common source, so
    clones share one machine-id (the same reason powered-off boxes show as "online"
    in RustDesk). A persisted random value is unique even among clones.
    """
    path = os.environ.get("CC_CLIENT_ID_FILE", CLIENT_ID_FILE)
    try:
        with open(path) as fh:
            got = fh.read().strip()
        if got:
            return got
    except (IOError, OSError):
        pass
    suffix = binascii.hexlify(os.urandom(3)).decode("ascii")
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(suffix + "\n")
    except (IOError, OSError):
        # Read-only /var or no permission: still unique per process, which is all
        # that is needed to stop the eviction war. It just changes on restart.
        log.warning("could not persist client-id suffix at %s; using a per-run id", path)
    return suffix


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
        self._client_id = "%s-%s-%s" % (config.broker.client_id_prefix,
                                        config.host_name, _instance_suffix())
        if not announce:
            self._client_id += "-test"
        self._pending = []
        self._winning_broker = None   # set to a fallback endpoint once one works
        self._connected = False
        # Seeded at connect: "no successful publish since" is only meaningful once
        # there has been a connection to publish over.
        self._last_publish_ok = None
        self._dropped_offline = 0
        self._build_client(config.broker)

    def _build_client(self, broker):
        """(Re)build the paho client for a given broker endpoint (transport/TLS/
        WebSocket path all depend on it), re-applying the Last-Will + on_connect."""
        self.client = _make_client(self._client_id, broker.transport)
        self._connected = False           # a fresh client starts disconnected
        try:
            self.client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        except (AttributeError, TypeError):
            pass                          # very old paho: the _publish guard still bounds us
        # Track the live connection state and PUBACKs, so we neither queue into a dead
        # socket nor keep "running" while nothing we send is landing.
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
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
        # Always tracked (even for a --test publisher): _on_connect is what marks us
        # connected. Re-asserting "online" inside it stays announce-only.
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
            self._connected = True
            if self._last_publish_ok is None:
                self._last_publish_ok = time.time()   # start the watchdog clock
            if self.announce:
                client.publish(self.host_status_topic, "online", qos=1, retain=True)

    def _on_disconnect(self, client, userdata, rc, *args):
        """Mark us offline so _publish stops feeding paho's queue.

        paho reconnects on its own; what must not happen meanwhile is a cycle-per-minute
        of retained snapshots piling into the outgoing queue for the whole outage."""
        self._connected = False

    def _on_publish(self, client, userdata, mid, *args):
        # QoS-1 PUBACK: proof the broker actually took a message. This -- not "the
        # process is alive" -- is what the watchdog measures.
        self._last_publish_ok = time.time()

    def is_connected(self):
        """True when the broker link is up. Prefers paho's own view; falls back to the
        callback-maintained flag on paho versions without is_connected()."""
        try:
            return bool(self.client.is_connected())
        except AttributeError:
            return self._connected

    def seconds_since_publish(self):
        """Age of the last confirmed publish, or None before the first connection."""
        if self._last_publish_ok is None:
            return None
        return time.time() - self._last_publish_ok

    def _connect_to(self, broker):
        self.client.connect(broker.host, broker.port, keepalive=broker.keepalive)
        self.client.loop_start()
        # connect() returns once the TCP socket is up, but CONNACK arrives on the
        # network thread a moment later. Wait for it, or the first cycle's publishes
        # are dropped by the offline guard for a connection that is about to be fine.
        deadline = time.time() + 5.0
        while not self.is_connected() and time.time() < deadline:
            time.sleep(0.05)
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
            self.clear_renamed_host()
            return

        attempts = [self.config.broker]
        if self.config.broker.auto_fallback and self.config.broker.transport == "tcp":
            attempts += self._fallback_brokers()

        last_exc = None
        for i, b in enumerate(attempts):
            self._build_client(b)
            try:
                self._connect_to(b)
                self.clear_renamed_host()
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

    HOSTNAME_FILE = "/var/lib/cc-rms-monitor/last_host_name"

    def clear_renamed_host(self):
        """Tombstone the retained topics of the host name we used LAST time, if it changed.

        Host records are retained, so renaming a host (new hostname, or an edited
        host_name in config.yaml) leaves the OLD topic published forever with frozen
        data. Consumers then see a host that has been "dead for weeks" while every one
        of its cameras is happily reporting under the new name -- which is exactly how
        one renamed host was misread as a 37-day outage. An empty retained payload is
        the documented tombstone (the bridge already treats it as stop-tracking).

        Runs on connect, so it covers a rename made any way at all -- reinstall,
        config edit, or the machine's hostname changing under us.
        """
        path = os.environ.get("CC_HOSTNAME_FILE", self.HOSTNAME_FILE)
        now = self.config.host_name
        prev = None
        try:
            with open(path) as fh:
                prev = fh.read().strip() or None
        except (IOError, OSError):
            pass
        if prev and prev != now:
            for leaf in ("health", "status"):
                topic = "%s/%s/%s" % (self.config.topic_prefix, prev, leaf)
                try:
                    self.client.publish(topic, payload=b"", qos=1, retain=True)
                    log.info("Host renamed %s -> %s; cleared stale retained %s", prev, now, topic)
                except Exception:
                    log.exception("could not clear retained topic %s", topic)
        if prev != now:
            try:
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(path, "w") as fh:
                    fh.write(now + "\n")
            except (IOError, OSError):
                log.warning("could not record host name at %s; a future rename will "
                            "leave a stale retained record", path)

    def _state_topic(self, station_id):
        return "%s/%s/health" % (self.config.topic_prefix, station_id)

    def _host_state_topic(self):
        return "%s/%s/health" % (self.config.topic_prefix, self.config.host_name)

    def _publish(self, topic, payload, retain=True):
        """Publish QoS-1 and track the message so flush() can confirm delivery.

        Drops instead of queueing while the broker is unreachable: a retained snapshot
        that never left is superseded by the next cycle's, so the only thing queueing
        buys is a flood of stale state to replay later (see MAX_QUEUED_MESSAGES)."""
        if not self.is_connected():
            self._dropped_offline += 1
            if self._dropped_offline in (1, 100) or self._dropped_offline % 1000 == 0:
                log.warning("Broker offline; dropped %d superseded snapshot(s) rather "
                            "than queueing them", self._dropped_offline)
            return None
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
        it is newly opted out of publishing so its old data doesn't linger.

        Returns True only if the tombstone actually went out: a tombstone is a
        ONE-SHOT the caller records as done, so one dropped during an outage would
        otherwise leave the opted-out station's data retained forever."""
        sent = self._publish(self._state_topic(station_id), "", retain=True) is not None
        self.flush()
        return sent

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
