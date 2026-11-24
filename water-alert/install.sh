#!/usr/bin/env bash
# Installs water-alert on Ubuntu. Safe to re-run (to upgrade the script, keeps your config).
#
# Optional settings, passed as environment variables (sudo VAR=value ./install.sh):
#   MQTT_TLS=1           Add an encrypted MQTT listener on port 8883 for the sensors. Creates a private CA and
#                        server certificate (make-mqtt-certs.sh). Upload ca.crt to each sensor, see README step 5.
#   MQTT_PLAIN_LAN=0     Bind the plaintext listener (1883) to 127.0.0.1 only. The water-alert service uses it
#                        locally. Needs MQTT_TLS=1, and every sensor must already be switched to TLS.
#   MQTT_ALLOW_FROM=CIDR If ufw is active, allow this sensor subnet (e.g. 192.168.104.0/24) to the MQTT ports.
#   MQTT_TLS_SAN=...     Extra certificate names, comma-separated (e.g. DNS:mqtt.example.org,IP:10.0.0.5).
# Without these the broker keeps the original behavior: password login, plaintext, port 1883 on all interfaces.
set -euo pipefail

MQTT_TLS=${MQTT_TLS:-}
MQTT_PLAIN_LAN=${MQTT_PLAIN_LAN:-}
MQTT_ALLOW_FROM=${MQTT_ALLOW_FROM:-}
MQTT_TLS_SAN=${MQTT_TLS_SAN:-}

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./install.sh"; exit 1; }
if [[ $MQTT_PLAIN_LAN == 0 && $MQTT_TLS != 1 ]]; then
  echo "MQTT_PLAIN_LAN=0 would cut off the sensors. Use it together with MQTT_TLS=1."; exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/water-alert
ETC=/etc/water-alert
VAR=/var/lib/water-alert
SVC_USER=wateralert
MQ_CONF=/etc/mosquitto/conf.d/water-alert.conf
MQ_PASSWD=/etc/mosquitto/water-alert.passwd
CREDS=$ETC/mqtt-credentials.txt
TLS_SRC=$ETC/tls                 # CA private key lives here (root only)
TLS_DEST=/etc/mosquitto/certs    # what the broker reads

echo "==> Installing packages (python3-venv, mosquitto)"
apt-get update -qq
apt-get install -y -qq python3-venv mosquitto mosquitto-clients openssl >/dev/null

echo "==> Service user and folders"
id -u "$SVC_USER" &>/dev/null || useradd --system --home "$VAR" --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 755 "$APP"
install -d -m 750 -o root -g "$SVC_USER" "$ETC"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$VAR"

echo "==> Program and Python libraries"
install -m 755 "$SRC/water_alert.py" "$APP/water_alert.py"
[[ -d $APP/venv ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "requests>=2.28" "paho-mqtt>=2.0"

NEW_CONFIG=0
if [[ ! -f $ETC/water-alert.ini ]]; then
  install -m 640 -o root -g "$SVC_USER" "$SRC/water-alert.ini.example" "$ETC/water-alert.ini"
  NEW_CONFIG=1
fi

echo "==> Mosquitto (MQTT broker for the Shelly sensors)"
if [[ ! -f $MQ_PASSWD ]]; then
  SVC_PW=$(openssl rand -hex 16)
  SHELLY_PW=$(openssl rand -hex 12)
  mosquitto_passwd -c -b "$MQ_PASSWD" wateralert "$SVC_PW"
  mosquitto_passwd -b "$MQ_PASSWD" shelly "$SHELLY_PW"
  chown root:mosquitto "$MQ_PASSWD"; chmod 640 "$MQ_PASSWD"
  umask 077
  printf 'Service login:  wateralert / %s\nShelly login:   shelly / %s\n' "$SVC_PW" "$SHELLY_PW" > "$CREDS"
  chmod 600 "$CREDS"
  sed -i "s/__MQTT_SERVICE_PASSWORD__/$SVC_PW/" "$ETC/water-alert.ini"
fi
IP=$(hostname -I | awk '{print $1}')
if [[ $MQTT_TLS == 1 ]]; then
  echo "==> TLS certificates for the sensor listener"
  install -d -m 700 "$TLS_SRC"
  SAN_ARGS=(); [[ -n $MQTT_TLS_SAN ]] && IFS=, read -r -a SAN_ARGS <<< "$MQTT_TLS_SAN"
  "$SRC/make-mqtt-certs.sh" "$TLS_SRC" "$IP" "${SAN_ARGS[@]}"
  install -d -m 755 "$TLS_DEST"
  install -m 644 "$TLS_SRC/ca.crt" "$TLS_SRC/server.crt" "$TLS_DEST/"
  install -m 640 -o root -g mosquitto "$TLS_SRC/server.key" "$TLS_DEST/server.key"
fi

# Write the broker config on first install, or when MQTT_TLS / MQTT_PLAIN_LAN is given (old file is kept as .bak).
if [[ ! -f $MQ_CONF || -n $MQTT_TLS || -n $MQTT_PLAIN_LAN ]]; then
  [[ -f $MQ_CONF ]] && cp -p "$MQ_CONF" "$MQ_CONF.bak"
  PLAIN_BIND=""; [[ $MQTT_PLAIN_LAN == 0 ]] && PLAIN_BIND=" 127.0.0.1"
  {
    echo "# Written by water-alert install.sh"
    echo "allow_anonymous false"
    echo "password_file $MQ_PASSWD"
    echo
    echo "listener 1883$PLAIN_BIND"
    if [[ $MQTT_TLS == 1 ]]; then
      printf '\nlistener 8883\ncafile %s/ca.crt\ncertfile %s/server.crt\nkeyfile %s/server.key\ntls_version tlsv1.2\n' \
        "$TLS_DEST" "$TLS_DEST" "$TLS_DEST"
    fi
  } > "$MQ_CONF"
fi
systemctl enable -q mosquitto
systemctl restart mosquitto

echo "==> systemd service and 'water-alert' command"
install -m 644 "$SRC/water-alert.service" /etc/systemd/system/water-alert.service
cat > /usr/local/bin/water-alert <<'EOF'
#!/bin/sh
# Runs water-alert commands as the service user. Usage: sudo water-alert <command>
exec sudo -u wateralert /opt/water-alert/venv/bin/python /opt/water-alert/water_alert.py \
  -c /etc/water-alert/water-alert.ini "$@"
EOF
chmod 755 /usr/local/bin/water-alert
systemctl daemon-reload

echo
echo "Installed."
[[ $NEW_CONFIG -eq 1 ]] && echo "  Config:       $ETC/water-alert.ini  (fill in Zoom, Flume, recipients, sensors)"
echo "  MQTT logins:  sudo cat $CREDS"
if [[ $MQTT_TLS == 1 ]]; then
  echo "  Shelly MQTT server to enter on each sensor:  $IP:8883   (TLS, user CA: $TLS_DEST/ca.crt)"
else
  echo "  Shelly MQTT server to enter on each sensor:  $IP:1883"
  echo "  WARNING: port 1883 is plaintext. Anyone who can see that network traffic can read the sensors'"
  echo "           MQTT password. Keep the sensors on an isolated VLAN with a firewall rule limited to this port,"
  echo "           or re-run with MQTT_TLS=1 (see README step 5)."
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  PORTS=1883; [[ $MQTT_TLS == 1 ]] && PORTS="1883 8883"; [[ $MQTT_PLAIN_LAN == 0 ]] && PORTS=8883
  if [[ -n $MQTT_ALLOW_FROM ]]; then
    for p in $PORTS; do ufw allow from "$MQTT_ALLOW_FROM" to any port "$p" proto tcp; done
  else
    echo "  ufw is on. Allow only the sensor subnet in, for example:"
    for p in $PORTS; do echo "    sudo ufw allow from 192.168.1.0/24 to any port $p proto tcp"; done
  fi
fi
echo
echo "Next: follow README.md steps 2-6, then: sudo systemctl enable --now water-alert"
