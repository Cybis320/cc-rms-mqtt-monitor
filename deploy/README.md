# Broker setup (mqtt.contrailcast.com)

Hardened Mosquitto config for the open RMS health broker. Anonymous publishing
stays frictionless; this adds a namespace ACL and resource limits.

Files:
- `mosquitto.conf` → `/etc/mosquitto/conf.d/contrailcast.conf`
- `aclfile` → `/etc/mosquitto/aclfile`

> **TLS is optional and off by default.** The health feed is non-sensitive and
> world-readable, and the broker uses no credentials, so TLS would add a
> cert-expiry outage mode without protecting anything. The `mosquitto.conf`
> here includes a ready 8883 TLS listener and steps 1–2 below cover certbot —
> but for the current open feed you can **skip the TLS listener and the certbot
> steps** and run with the 1883 listener, ACL, and limits only. Enable TLS when
> you add authentication or transmit sensitive data (precise location, PII).

## 1. Install Mosquitto + certbot

```bash
sudo apt update
sudo apt install -y mosquitto certbot
```

## 2. TLS certificate (Let's Encrypt)

`mqtt.contrailcast.com` must resolve to this host and port 80 must be reachable.

```bash
sudo certbot certonly --standalone -d mqtt.contrailcast.com
```

Mosquitto runs as the unprivileged `mosquitto` user and cannot read
`/etc/letsencrypt/live` directly. Copy the cert into a place it can read, and
re-copy automatically on renewal:

```bash
sudo mkdir -p /etc/mosquitto/certs

# certbot deploy hook: copy + chown + reload on every renewal
sudo tee /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh >/dev/null <<'EOF'
#!/bin/bash
set -e
LIVE=/etc/letsencrypt/live/mqtt.contrailcast.com
DEST=/etc/mosquitto/certs
cp "$LIVE/fullchain.pem" "$DEST/fullchain.pem"
cp "$LIVE/privkey.pem"   "$DEST/privkey.pem"
chown mosquitto:mosquitto "$DEST"/*.pem
chmod 640 "$DEST"/*.pem
systemctl reload mosquitto
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh

# Run it once now to seed the initial copy
sudo /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh
```

## 3. Install the broker config

```bash
sudo cp mosquitto.conf /etc/mosquitto/conf.d/contrailcast.conf
sudo cp aclfile /etc/mosquitto/aclfile
sudo chown mosquitto:mosquitto /etc/mosquitto/aclfile
sudo systemctl enable --now mosquitto
sudo systemctl restart mosquitto
```

## 4. Firewall

```bash
sudo ufw allow 8883/tcp     # TLS MQTT
sudo ufw allow 1883/tcp     # plaintext (remove after migration)
sudo ufw allow 80/tcp       # certbot renewal (standalone)
```

Optional: `fail2ban` on the mosquitto log to ban connection-flood IPs.

## 5. Verify

```bash
# TLS round-trip (system CA must trust the Let's Encrypt cert)
mosquitto_sub -h mqtt.contrailcast.com -p 8883 --capath /etc/ssl/certs -t 'stations/#' -v &
mosquitto_pub -h mqtt.contrailcast.com -p 8883 --capath /etc/ssl/certs -t 'stations/_selftest/health' -m ok

# Confirm the ACL blocks topics outside stations/ (should NOT deliver)
mosquitto_pub -h mqtt.contrailcast.com -p 8883 --capath /etc/ssl/certs -t 'denied/x' -m nope
```

## Migration to 8883-only

Once every station's `config.yaml` uses `port: 8883` / `tls: true`, remove the
`listener 1883` block from `/etc/mosquitto/conf.d/contrailcast.conf`, drop the
ufw rule for 1883, and restart mosquitto.
