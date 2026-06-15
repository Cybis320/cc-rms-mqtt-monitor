# Broker setup (mqtt.contrailcast.com)

Hardening for the open RMS health broker. The broker is intentionally open and
anonymous; the only protections that matter at zero client friction are the
**`stations/#` namespace ACL** and a few **resource limits**.

> **Additive, not greenfield.** A running mosquitto already has a base config
> (persistence, logging) and a listener defined. Apply the hardening *on top* of
> that — do **not** drop in a standalone full config. Re-declaring
> `persistence_location`, or adding a second `listener` on a port already in
> use, is a duplicate-value/duplicate-listener error that crash-loops mosquitto.
> Likewise skip `per_listener_settings true` (only relevant with multiple
> listeners of differing security). See `hardening.conf` for the exact lines and
> what to avoid.

## 1. Namespace ACL (the security feature)

Ensure the existing listener references an ACL that confines anonymous clients
to the `stations/` tree. Install `aclfile`:

```bash
sudo cp aclfile /etc/mosquitto/aclfile
sudo chown mosquitto:mosquitto /etc/mosquitto/aclfile
```

and confirm the listener config has both:

```conf
allow_anonymous true
acl_file /etc/mosquitto/aclfile
```

`aclfile` grants anonymous clients only `topic readwrite stations/#` (broad
during development; see the file for the zero-friction `%u`-pattern tightening
to apply later).

## 2. Resource limits

Append the six lines from `hardening.conf` to the existing conf.d file that
defines the stations listener (after checking none are already set elsewhere).
Then:

```bash
sudo systemctl restart mosquitto
journalctl -u mosquitto -n 30 --no-pager   # confirm a clean start (no parse errors)
```

## 3. Firewall

```bash
sudo ufw allow 1883/tcp
```

Optional: `fail2ban` on the mosquitto log to ban connection-flood IPs.

## 4. Verify

```bash
mosquitto_sub -h localhost -t 'stations/#' -v &
mosquitto_pub -h localhost -t 'stations/_selftest/health' -m ok
# Confirm a publish OUTSIDE stations/ is rejected (should NOT be delivered):
mosquitto_pub -h localhost -t 'denied/x' -m nope
# Clean up the retained self-test if you used -r anywhere:
mosquitto_pub -h localhost -t 'stations/_selftest/health' -r -n
```

## Optional: TLS later (only with authentication)

TLS is **off by default** and not needed for the current open, non-sensitive,
credential-free feed (it would add a cert-expiry outage mode without protecting
anything). Add it the day you introduce authentication or transmit sensitive
data (precise location, PII). At that point, add a **second listener on 8883**
(a different port, so no duplicate-listener conflict) reusing the same
`allow_anonymous`/`acl_file`, with a Let's Encrypt cert:

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d mqtt.contrailcast.com   # port 80 must be free
```

Use a certbot deploy-hook to copy `fullchain.pem`/`privkey.pem` into a
mosquitto-readable dir (e.g. `/etc/mosquitto/certs`, `chown mosquitto`, `chmod
640`) and `systemctl reload mosquitto` on renewal. Add to the listener:

```conf
listener 8883
allow_anonymous true
acl_file /etc/mosquitto/aclfile
certfile /etc/mosquitto/certs/fullchain.pem
keyfile  /etc/mosquitto/certs/privkey.pem
tls_version tlsv1.2
```

Then set `tls: true` / `port: 8883` in the station `config.yaml`.
