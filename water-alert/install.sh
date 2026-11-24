#!/usr/bin/env bash
# Installs water-alert on Ubuntu. Safe to re-run (to upgrade the script, keeps your config).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./install.sh"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/water-alert
ETC=/etc/water-alert
VAR=/var/lib/water-alert
SVC_USER=wateralert
MQ_CONF=/etc/mosquitto/conf.d/water-alert.conf
MQ_PASSWD=/etc/mosquitto/water-alert.passwd
CREDS=$ETC/mqtt-credentials.txt

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
if [[ ! -f $MQ_CONF ]]; then
  cat > "$MQ_CONF" <<EOF
# Added by water-alert
listener 1883
allow_anonymous false
password_file $MQ_PASSWD
EOF
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

IP=$(hostname -I | awk '{print $1}')
echo
echo "Installed."
[[ $NEW_CONFIG -eq 1 ]] && echo "  Config:       $ETC/water-alert.ini  (fill in Zoom, Flume, recipients, sensors)"
echo "  MQTT logins:  sudo cat $CREDS"
echo "  Shelly MQTT server to enter on each sensor:  $IP:1883"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  echo "  ufw is on. Allow the sensors in, for example:"
  echo "    sudo ufw allow from 192.168.1.0/24 to any port 1883 proto tcp"
fi
echo
echo "Next: follow README.md steps 2-6, then: sudo systemctl enable --now water-alert"
