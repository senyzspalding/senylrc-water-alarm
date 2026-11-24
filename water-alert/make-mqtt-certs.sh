#!/usr/bin/env bash
# Creates a private CA and a Mosquitto server certificate for the sensor MQTT listener.
# Usage: make-mqtt-certs.sh <output-dir> <server-ip> [extra SAN entries, e.g. DNS:mqtt.example.org IP:10.0.0.5]
# Safe to re-run: existing CA and server cert are kept. Delete server.crt to re-issue it (the CA, and so
# every sensor's trust, stays the same). ca.key never needs to leave this folder.
set -euo pipefail

OUT=${1:?output dir}; IP=${2:?server ip}; shift 2
EXTRA=("$@")
DAYS=3650

umask 077
mkdir -p "$OUT"

if [[ ! -f $OUT/ca.key || ! -f $OUT/ca.crt ]]; then
  openssl genrsa -out "$OUT/ca.key" 4096 2>/dev/null
  openssl req -x509 -new -key "$OUT/ca.key" -sha256 -days "$DAYS" \
    -subj "/CN=water-alert MQTT CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$OUT/ca.crt"
fi

if [[ ! -f $OUT/server.key || ! -f $OUT/server.crt ]]; then
  SAN="IP:$IP,IP:127.0.0.1,DNS:localhost,DNS:$(hostname)"
  for e in "${EXTRA[@]}"; do SAN+=",$e"; done
  openssl genrsa -out "$OUT/server.key" 2048 2>/dev/null
  openssl req -new -key "$OUT/server.key" -subj "/CN=$IP" -out "$OUT/server.csr"
  printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN" > "$OUT/server.ext"
  openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
    -days "$DAYS" -sha256 -extfile "$OUT/server.ext" -out "$OUT/server.crt" 2>/dev/null
  rm -f "$OUT/server.csr" "$OUT/server.ext"
fi

openssl verify -CAfile "$OUT/ca.crt" "$OUT/server.crt" >/dev/null
echo "CA certificate (upload to each sensor): $OUT/ca.crt"
