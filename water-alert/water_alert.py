#!/usr/bin/env python3
"""
water-alert - leak and flow alerting for an Ubuntu server.

Watches
  * Shelly Flood Gen4 sensors over MQTT (local Mosquitto broker), plus optional HTTP webhooks
  * a Flume water monitor (Flume 2 / F2X) through the Flume cloud API

Sends
  * text messages from your Zoom Phone number (Zoom Phone SMS API) for critical alerts
  * email through the local mail relay (postfix) for everything

Commands
  run            run the monitor (this is what systemd starts)
  zoom-auth      one-time Zoom sign-in; tokens then refresh on their own
  test-email     send a test email
  test-sms       send a test text to every recipient (or --to NUMBER)
  test-flume     sign in to Flume and print devices, recent flow and leak status
  simulate-leak  push a fake Shelly leak through the full alert path (text + email)
  status         show active alerts and when each sensor last checked in
"""

import argparse
import base64
import configparser
import datetime as dt
import email.utils
import fcntl
import hmac
import json
import logging
import os
import re
import signal
import smtplib
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
from contextlib import contextmanager
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:  # only needed when the Shelly monitor runs
    mqtt = None

log = logging.getLogger("water-alert")

DEFAULT_CONFIG = os.environ.get("WATER_ALERT_CONFIG", "/etc/water-alert/water-alert.ini")
ZOOM_OAUTH = "https://zoom.us/oauth"
ZOOM_API = "https://api.zoom.us/v2"
FLUME_API = "https://api.flumetech.com"
HTTP_TIMEOUT = 30

CRITICAL, WARNING, OFF = "critical", "warning", "off"
DEFAULT_ROUTING = {
    "shelly_flood": CRITICAL,
    "flume_high_flow": CRITICAL,
    "flume_continuous_flow": WARNING,
    "flume_leak": WARNING,
    "flume_quiet_hours": WARNING,
    "flume_offline": WARNING,
    "flume_low_battery": WARNING,
    "flume_api": WARNING,
    "shelly_silent": WARNING,
    "shelly_low_battery": WARNING,
    "mqtt_down": WARNING,
    "zoom_auth": WARNING,
}


# ----------------------------------------------------------------------------- helpers

def fmt_time(ts=None):
    """Format a Unix timestamp (default: now) as local, human-readable text for messages."""
    t = dt.datetime.fromtimestamp(ts).astimezone() if ts else dt.datetime.now().astimezone()
    return t.strftime("%a %b %d %Y, %I:%M %p %Z").replace(" 0", " ")


def fmt_duration(sec):
    """Format a number of seconds as '2h 5m' or '12m'."""
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def ascii_only(text):
    """Keep texts in the plain GSM character set so they aren't split or mangled."""
    text = text.replace("—", "-").replace("–", "-")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def write_json_atomic(path, obj, mode=0o600):
    """Write obj as JSON via a temp file + rename, so a crash never leaves a half-written file.
    File mode defaults to 0600 (tokens/state are private).
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    """Load JSON from path; return default if the file is missing or corrupt (corruption is
    logged).
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        log.error("%s is not valid JSON; ignoring it", path)
        return default


@contextmanager
def file_lock(path):
    """Cross-process lock, so the service and a CLI test never refresh Zoom tokens at once."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_number(raw):
    """Convert a US-style phone number to E.164 (+1XXXXXXXXXX). Raises ValueError if it isn't
    valid.
    """
    s = re.sub(r"[^\d+]", "", raw.strip())
    if not s.startswith("+"):
        if len(s) == 10:
            s = "+1" + s
        elif len(s) == 11 and s.startswith("1"):
            s = "+" + s
    if not E164.match(s):
        raise ValueError(f"not a valid phone number: {raw!r}")
    return s


def parse_hhmm(s):
    """Parse 'HH:MM' into a datetime.time."""
    h, m = s.strip().split(":")
    return dt.time(int(h), int(m))


def in_window(t, start, end):
    """True if time t is in [start, end). Handles windows that cross midnight (e.g. 23:00-05:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


# ----------------------------------------------------------------------------- config / state

class Config:
    """INI config wrapper. Every getter takes a code default, so new keys never break an older
    water-alert.ini. Interpolation is off and key case is kept (sensor prefixes are case-
    sensitive); don't use inline comments, since '#' may be part of a password.
    """
    def __init__(self, path):
        """Read the INI file at path; exit with a clear message if it can't be read."""
        self.path = path
        self.cp = configparser.ConfigParser(interpolation=None)
        self.cp.optionxform = str  # keep the case of sensor prefixes and names
        if not self.cp.read(path):
            raise SystemExit(f"Config file not found or not readable: {path}")

    def get(self, section, key, default=""):
        """Return the stripped string value, or default if the section/key is missing."""
        return self.cp.get(section, key, fallback=default).strip()

    def getint(self, section, key, default):
        """Integer value; default if unset or blank."""
        v = self.get(section, key)
        return int(v) if v else default

    def getfloat(self, section, key, default):
        """Float value; default if unset or blank."""
        v = self.get(section, key)
        return float(v) if v else default

    def getbool(self, section, key, default):
        """Boolean value (1/true/yes/on); default if unset or blank."""
        v = self.get(section, key).lower()
        if not v:
            return default
        return v in ("1", "true", "yes", "on")

    def section(self, name):
        """Return a whole section as {key: value}, or {} if the section doesn't exist."""
        if not self.cp.has_section(name):
            return {}
        return {k: v.strip() for k, v in self.cp.items(name)}


class State:
    """Small JSON store for alert state and sensor check-ins. path=None keeps it in memory."""

    def __init__(self, path=None):
        """Load state from path (None = in-memory only, used by CLI test commands so they never
        touch live state). Layout: 'alerts' {key: {active, since, kind, subject, last_sent}}
        and 'last_seen' {sensor prefix: timestamp}.
        """
        self.path = path
        self.lock = threading.RLock()
        self.data = read_json(path, {}) if path else {}
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault("alerts", {})
        self.data.setdefault("last_seen", {})
        self._saved = None

    def save(self):
        """Persist state atomically. Skips the write when nothing changed."""
        if not self.path:
            return
        with self.lock:
            snapshot = json.dumps(self.data, sort_keys=True)
            if snapshot == self._saved:
                return
            try:
                write_json_atomic(self.path, self.data)
                self._saved = snapshot
            except OSError as e:
                log.error("could not save state: %s", e)


# ----------------------------------------------------------------------------- email

class Mailer:
    """Sends email through an SMTP server (the local postfix relay by default)."""
    def __init__(self, cfg):
        """Read [email] settings: smtp_host, smtp_port, from, to (comma-separated),
        subject_prefix.
        """
        self.host = cfg.get("email", "smtp_host", "localhost")
        self.port = cfg.getint("email", "smtp_port", 25)
        self.sender = cfg.get("email", "from", "noreply@localhost")
        self.to = [a.strip() for a in cfg.get("email", "to").split(",") if a.strip()]
        self.prefix = cfg.get("email", "subject_prefix", "[Water Alert]")

    def send(self, subject, body):
        """Send a plain-text email to all recipients. Raises on SMTP errors; callers decide how
        to handle that.
        """
        if not self.to:
            log.warning("no email recipients configured; skipping email: %s", subject)
            return
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.to)
        msg["Subject"] = f"{self.prefix} {subject}".strip()
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=HTTP_TIMEOUT) as s:
            s.send_message(msg)
        log.info("email sent: %s", subject)


# ----------------------------------------------------------------------------- Zoom SMS

class ZoomAuthError(Exception):
    """Raised when Zoom sign-in is missing, expired or rejected. The fix is always: run 'water-
    alert zoom-auth'.
    """
    pass


class ZoomSMS:
    """Sends texts as one Zoom Phone user (the one who ran zoom-auth), from their own number."""

    def __init__(self, cfg, state_dir):
        """Read [zoom] and [sms_recipients]. Tokens live in <state_dir>/zoom_tokens.json, guarded
        by a lock file.
        """
        self.client_id = cfg.get("zoom", "client_id")
        self.client_secret = cfg.get("zoom", "client_secret")
        self.redirect_uri = cfg.get("zoom", "redirect_uri")
        raw_sender = cfg.get("zoom", "sender_number")
        self.sender_number = normalize_number(raw_sender) if raw_sender else ""
        self.sender_user_id_cfg = cfg.get("zoom", "sender_user_id")
        self.recipients = []
        for name, num in cfg.section("sms_recipients").items():
            if num:
                self.recipients.append((name, normalize_number(num)))
        self.token_path = os.path.join(state_dir, "zoom_tokens.json")
        self.lock_path = self.token_path + ".lock"
        self._tlock = threading.Lock()

    # --- OAuth
    def authorize_url(self):
        """URL the user opens (signed in to Zoom as the sender) to approve the app."""
        q = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        })
        return f"{ZOOM_OAUTH}/authorize?{q}"

    def _token_request(self, data):
        """POST to Zoom's token endpoint (authorization-code or refresh). Stamps 'expires_at'
        with a 2-minute safety margin.
        """
        if not self.client_id or not self.client_secret:
            raise ZoomAuthError("client_id / client_secret are not set in [zoom]")
        try:
            r = requests.post(f"{ZOOM_OAUTH}/token", auth=(self.client_id, self.client_secret),
                              data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise ZoomAuthError(f"cannot reach Zoom: {e}")
        if r.status_code != 200:
            raise ZoomAuthError(f"Zoom token request failed ({r.status_code}): {r.text[:300]}")
        tok = r.json()
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
        return tok

    def exchange_code(self, code):
        """Trade the one-time authorization code from zoom-auth for tokens and save them."""
        tok = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        })
        with self._tlock, file_lock(self.lock_path):
            write_json_atomic(self.token_path, tok)

    def access_token(self, force_refresh=False):
        # Zoom refresh tokens are single-use, so always work from the copy on disk.
        """Return a valid access token, refreshing if expired or force_refresh. Zoom refresh
        tokens are SINGLE-USE, so this always re-reads the token file under a cross-process
        lock; the daemon and a CLI test can't invalidate each other's chain. Keep that pattern
        for every token read/refresh.
        """
        with self._tlock, file_lock(self.lock_path):
            tok = read_json(self.token_path, None)
            if not tok or "refresh_token" not in tok:
                raise ZoomAuthError("Zoom is not signed in yet - run: sudo water-alert zoom-auth")
            if force_refresh or time.time() >= tok.get("expires_at", 0):
                new = self._token_request({"grant_type": "refresh_token",
                                           "refresh_token": tok["refresh_token"]})
                for k in ("user_id", "user_email"):
                    if k in tok:
                        new.setdefault(k, tok[k])
                write_json_atomic(self.token_path, new)
                tok = new
                log.info("Zoom access token refreshed")
            return tok["access_token"]

    def stored(self, key):
        """Read one value from the saved token file (e.g. 'user_id'), or None."""
        return (read_json(self.token_path, {}) or {}).get(key)

    def api(self, method, path, **kw):
        """Call the Zoom API with auth. Retries: once with a forced token refresh on 401; up to 3
        times with backoff on network errors, 429 and 5xx. Returns the final Response.
        """
        force = False
        r = None
        for attempt in range(4):
            token = self.access_token(force_refresh=force)
            force = False
            try:
                r = requests.request(method, ZOOM_API + path, timeout=HTTP_TIMEOUT,
                                     headers={"Authorization": f"Bearer {token}"}, **kw)
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                force = True
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            return r
        return r

    def whoami(self):
        """Look up the signed-in Zoom user, save their id/email to the token file, and list their
        Zoom Phone numbers. Returns (user_dict, [numbers]).
        """
        r = self.api("GET", "/users/me")
        if r.status_code != 200:
            raise ZoomAuthError(f"could not read your Zoom user ({r.status_code}): {r.text[:300]}")
        me = r.json()
        with self._tlock, file_lock(self.lock_path):
            tok = read_json(self.token_path, {})
            tok["user_id"] = me["id"]
            tok["user_email"] = me.get("email", "")
            write_json_atomic(self.token_path, tok)
        numbers = []
        r2 = self.api("GET", f"/phone/users/{me['id']}")
        if r2.status_code == 200:
            numbers = [n.get("number") for n in r2.json().get("phone_numbers", []) if n.get("number")]
        return me, numbers

    def user_id(self):
        """Zoom user id used as the SMS sender (config override, else the one saved by zoom-
        auth).
        """
        uid = self.sender_user_id_cfg or self.stored("user_id")
        if not uid:
            raise ZoomAuthError("Zoom sender user id unknown - run: sudo water-alert zoom-auth")
        return uid

    # --- sending
    def send(self, text, recipients=None):
        """Text each recipient separately (no group thread). Returns [(name, number, ok, detail)]."""
        if not self.sender_number:
            raise ZoomAuthError("sender_number is not set in [zoom]")
        self.access_token()  # clear "not signed in" error before anything else
        uid = self.user_id()
        targets = self.recipients if recipients is None else recipients
        results = []
        for i, (name, number) in enumerate(targets):
            if i:
                time.sleep(1)
            body = {
                "message": text,
                "sender": {"user_id": uid, "phone_number": self.sender_number},
                "to_members": [{"phone_number": number}],
            }
            try:
                r = self.api("POST", "/phone/sms/messages", json=body)
                ok = r.status_code in (200, 201)
                detail = "sent" if ok else f"HTTP {r.status_code}: {r.text[:200]}"
            except (requests.RequestException, ZoomAuthError) as e:
                ok, detail = False, str(e)
            (log.info if ok else log.error)("SMS to %s (%s): %s", name, number, detail)
            results.append((name, number, ok, detail))
        return results


# ----------------------------------------------------------------------------- alert routing

class Alerter:
    """Decides what each alert does. fire()/clear() are de-duplicated by KEY, so one ongoing
    condition alerts once and then repeats on a timer. [routing] maps each alert kind to
    critical (text + email), warning (email only) or off. State is persisted, so alerts
    survive restarts.
    """
    def __init__(self, cfg, state, mailer, sms):
        """Load [routing] overrides and [alerts] timers (critical_repeat_minutes,
        warning_repeat_minutes, sms_on_clear).
        """
        self.state, self.mailer, self.sms = state, mailer, sms
        self.routing = dict(DEFAULT_ROUTING)
        for k, v in cfg.section("routing").items():
            v = v.lower()
            if v not in (CRITICAL, WARNING, OFF):
                log.warning("ignoring [routing] %s = %s (use critical, warning or off)", k, v)
                continue
            self.routing[k] = v
        self.critical_repeat = cfg.getint("alerts", "critical_repeat_minutes", 30) * 60
        self.warning_repeat = cfg.getint("alerts", "warning_repeat_minutes", 240) * 60
        self.sms_on_clear = cfg.getbool("alerts", "sms_on_clear", True)
        self.host = socket.gethostname()
        self.site = site_name(cfg)
        self.lock = threading.RLock()

    def severity(self, kind):
        """Routing for an alert kind: critical, warning or off (unknown kinds are warning)."""
        return self.routing.get(kind, WARNING)

    def is_active(self, key):
        """True if the alert with this key is currently active."""
        with self.lock:
            return bool(self.state.data["alerts"].get(key, {}).get("active"))

    def fire(self, key, kind, subject, detail=""):
        """Report that a condition is active. Sends when new, then repeats on a timer."""
        sev = self.severity(kind)
        now = time.time()
        with self.lock:
            alerts = self.state.data["alerts"]
            a = alerts.get(key)
            if a and a.get("active"):
                repeat = self.critical_repeat if sev == CRITICAL else self.warning_repeat
                if repeat <= 0 or now - a.get("last_sent", 0) < repeat:
                    return False
                title = f"STILL ACTIVE: {subject}"
            else:
                a = {"active": True, "since": now, "kind": kind}
                title = subject
            a.update(subject=subject, last_sent=now)
            alerts[key] = a
            self.state.save()
            since = a["since"]
        if sev == OFF:
            log.warning("alert (routing=off, not sent): %s", title)
            return True
        log.warning("ALERT [%s] %s", sev, title)
        self._dispatch(sev, title, detail, since)
        return True

    def clear(self, key, subject, detail=""):
        """Report that a condition ended. Sends a 'CLEARED' message only if the alert was active,
        and returns True when it did. A critical clear is downgraded to email-only if
        sms_on_clear is false.
        """
        with self.lock:
            a = self.state.data["alerts"].get(key)
            if not a or not a.get("active"):
                return False
            a["active"] = False
            a["cleared_at"] = time.time()
            kind, since = a.get("kind", ""), a.get("since", time.time())
            self.state.save()
        sev = self.severity(kind)
        log.info("CLEARED: %s", subject)
        if sev == OFF:
            return True
        if sev == CRITICAL and not self.sms_on_clear:
            sev = WARNING
        extra = f"Was active for {fmt_duration(time.time() - since)}."
        self._dispatch(sev, f"CLEARED: {subject}", f"{detail}\n{extra}".strip(), None)
        return True

    def _dispatch(self, sev, title, detail, since):
        """Build and send one alert. Critical: text every recipient first, then ALWAYS email (SMS
        failures are listed in the email body, and a texting bug never blocks the email).
        Texts are ASCII-folded and capped at 300 chars.
        """
        lines = [title, ""]
        if detail:
            lines += [detail, ""]
        if since:
            lines.append(f"First detected: {fmt_time(since)}")
        lines += [f"Location: {self.site}", f"Sent: {fmt_time()}", f"Server: {self.host}"]

        if sev == CRITICAL:
            if self.sms is None:
                lines += ["", "(Texting is turned off in the config.)"]
            else:
                n = dt.datetime.now()
                stamp = f"{n.month}/{n.day} {n.strftime('%I:%M%p').lstrip('0')}"
                text = ascii_only(f"{self.site} WATER ALERT: {title} ({stamp})")[:300]
                try:
                    results = self.sms.send(text)
                    sent = sum(1 for r in results if r[2])
                    lines += ["", f"Text message sent to {sent} of {len(results)} recipients."]
                    failed = [f"  {n} {num}: {d}" for n, num, ok, d in results if not ok]
                    if failed:
                        lines += ["TEXT FAILED for:"] + failed
                except ZoomAuthError as e:
                    lines += ["", f"TEXT NOT SENT: {e}"]
                except Exception as e:  # never let a texting bug stop the email
                    log.exception("SMS dispatch failed")
                    lines += ["", f"TEXT NOT SENT: {e}"]

        subject = f"URGENT - {title}" if sev == CRITICAL and not title.startswith("CLEARED") else title
        try:
            self.mailer.send(subject, "\n".join(lines))
        except Exception as e:
            log.error("email failed (%s): %s", subject, e)


# ----------------------------------------------------------------------------- Shelly Flood (MQTT)

class ShellyMonitor:
    """Listens to Shelly Flood Gen4 sensors over MQTT and turns water/battery/check-in events
    into alerts. Topics are '<prefix>/events/rpc', '<prefix>/status/<component>' and
    '<prefix>/online'. Each prefix must match the sensor's MQTT topic_prefix and its key in
    [shelly_sensors] exactly.
    """
    def __init__(self, cfg, state, alerter):
        """Read [mqtt], [shelly] and [shelly_sensors] (prefix -> friendly name)."""
        self.state, self.alerter = state, alerter
        self.sensors = cfg.section("shelly_sensors")  # mqtt prefix -> friendly name
        self.host = cfg.get("mqtt", "host", "localhost")
        self.port = cfg.getint("mqtt", "port", 1883)
        self.user = cfg.get("mqtt", "username")
        self.password = cfg.get("mqtt", "password")
        self.client_id = cfg.get("mqtt", "client_id", "water-alert")
        self.mqtt_down_after = cfg.getint("mqtt", "alert_after_minutes", 10) * 60
        self.silence = cfg.getfloat("shelly", "silence_hours", 25) * 3600
        self.low_batt = cfg.getint("shelly", "low_battery_percent", 20)
        self.started = time.time()
        self.connected = False
        self.disconnected_since = time.time()
        self.client = None

    def name_for(self, prefix):
        """Friendly name for a sensor prefix, or the prefix itself if it isn't listed."""
        return self.sensors.get(prefix) or prefix

    def _split(self, topic):
        """Split a topic into (sensor prefix, rest). Matches listed prefixes first (longest wins,
        so prefixes may contain '/'); otherwise the prefix is the first path segment, so an
        unlisted sensor still alerts.
        """
        for p in sorted(self.sensors, key=len, reverse=True):
            if topic.startswith(p + "/"):
                return p, topic[len(p) + 1:]
        head, _, rest = topic.partition("/")
        return head, rest

    # --- connection
    def start(self):
        """Connect to the broker in a background thread (auto-reconnects). Supports paho-mqtt 2.x
        and 1.x.
        """
        if mqtt is None:
            raise SystemExit("paho-mqtt is not installed (pip install paho-mqtt)")
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
        except AttributeError:  # paho-mqtt 1.x
            c = mqtt.Client(client_id=self.client_id)
        if self.user:
            c.username_pw_set(self.user, self.password)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.reconnect_delay_set(1, 60)
        c.connect_async(self.host, self.port, keepalive=60)
        c.loop_start()
        self.client = c

    def stop(self):
        """Disconnect and stop the MQTT thread."""
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """On (re)connect: subscribe to sensor topics and clear any 'MQTT down' alert."""
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if failed:
            log.error("MQTT broker refused the connection: %s (check [mqtt] username/password)", rc)
            return
        log.info("MQTT connected to %s:%s", self.host, self.port)
        self.connected = True
        self.disconnected_since = None
        topics = ["+/events/rpc", "+/status/+", "+/online"]
        topics += [f"{p}/#" for p in self.sensors if "/" in p]
        for t in topics:
            client.subscribe(t)
        self.alerter.clear("mqtt_down", "MQTT broker connection restored")

    def _on_disconnect(self, client, userdata, *args):
        """Record when the broker connection dropped; check() alerts if it stays down."""
        self.connected = False
        if self.disconnected_since is None:
            self.disconnected_since = time.time()
        log.warning("MQTT disconnected (%s); will keep retrying", args[-2] if len(args) >= 2 else args)

    def _on_message(self, client, userdata, msg):
        # Retained messages are old news the broker replays on connect. Counting them would make
        # a dead sensor look alive after every restart, and could replay a stale alarm.
        """MQTT message callback. Drops RETAINED messages: Shellys publish '<prefix>/online'
        retained, so the broker replays it on every subscribe. Counting it would make a dead
        sensor look alive after a restart, and a stale retained alarm could re-fire. Do not
        remove this check.
        """
        if msg.retain:
            log.debug("ignoring retained message on %s", msg.topic)
            return
        try:
            self.handle(msg.topic, msg.payload)
        except Exception:
            log.exception("error handling MQTT message on %s", msg.topic)

    # --- message handling
    def handle(self, topic, payload):
        """Process one message. Any status/online/rpc message counts as a check-in. Handles
        NotifyStatus / NotifyFullStatus (flood alarm, battery) and NotifyEvent 'alarm'.
        """
        prefix, rest = self._split(topic)
        if not (rest in ("online", "events/rpc") or rest.startswith("status/")):
            return
        self._seen(prefix)
        if rest == "online":
            return
        text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if rest == "events/rpc":
            method = data.get("method")
            params = data.get("params") or {}
            if method in ("NotifyStatus", "NotifyFullStatus"):
                self._status(prefix, params)
            elif method == "NotifyEvent":
                for ev in params.get("events") or []:
                    comp, name = str(ev.get("component", "")), str(ev.get("event", ""))
                    if comp.startswith("flood") and name == "alarm":
                        self.flood(prefix, comp, True, "event")
        else:
            self._status(prefix, {rest[len("status/"):]: data})

    def _status(self, prefix, params):
        """Act on component status: 'flood:N' alarm true/false and 'devicepower:N' battery
        percent.
        """
        for comp, val in params.items():
            if not isinstance(val, dict):
                continue
            if comp.startswith("flood") and "alarm" in val:
                self.flood(prefix, comp, bool(val["alarm"]), "status")
            elif comp.startswith("devicepower"):
                pct = (val.get("battery") or {}).get("percent")
                if isinstance(pct, (int, float)):
                    self._battery(prefix, pct)

    def _seen(self, prefix):
        """Record a check-in and clear a 'sensor silent' alert if one was active."""
        with self.state.lock:
            self.state.data["last_seen"][prefix] = time.time()
        key = f"shelly_silent:{prefix}"
        if self.alerter.is_active(key):
            self.alerter.clear(key, f"{self.name_for(prefix)} is checking in again")

    def flood(self, prefix, comp, alarm, source):
        """Fire the water-detected alert (alarm=True) or clear it (alarm=False) for one flood
        component. 'source' (event/status/webhook) only appears in the message.
        """
        name = self.name_for(prefix)
        key = f"shelly_flood:{prefix}:{comp}"
        if alarm:
            self.alerter.fire(key, "shelly_flood", f"Water detected - {name}",
                              f"Shelly Flood sensor '{name}' ({prefix}, {comp}) reports water (via {source}).")
        else:
            self.alerter.clear(key, f"Sensor dry again - {name}",
                               f"Shelly Flood sensor '{name}' no longer reports water.")

    def _battery(self, prefix, pct):
        """Low-battery alert at or below low_battery_percent; clears with 5 points of hysteresis
        to avoid flapping.
        """
        name = self.name_for(prefix)
        key = f"shelly_battery:{prefix}"
        if pct <= self.low_batt:
            self.alerter.fire(key, "shelly_low_battery", f"Low battery - {name} ({pct:.0f}%)",
                              "Replace the batteries soon. A dead sensor can't report leaks.")
        elif pct > self.low_batt + 5:
            self.alerter.clear(key, f"Battery OK - {name} ({pct:.0f}%)")

    def webhook(self, sensor, state):
        """Handle the optional HTTP webhook. state: alarm/wet/on/true/1 or clear/dry/off/false/0.
        Returns False for an unrecognized state.
        """
        prefix = sensor if sensor in self.sensors else next(
            (p for p, n in self.sensors.items() if n.lower() == sensor.lower()), sensor)
        if state in ("alarm", "wet", "on", "true", "1"):
            alarm = True
        elif state in ("clear", "dry", "off", "false", "0"):
            alarm = False
        else:
            return False
        self._seen(prefix)
        self.flood(prefix, "flood:0", alarm, "webhook")
        return True

    # --- periodic checks
    def check(self):
        """Periodic watchdog (called every 15 s): alerts for sensors silent longer than
        silence_hours (sensors sleep and wake about every 12 h, so the default is 25 h) and
        for the broker being unreachable too long.
        """
        now = time.time()
        last_seen = self.state.data["last_seen"]
        for prefix, name in self.sensors.items():
            seen = last_seen.get(prefix)
            last = seen or self.started
            if now - last > self.silence:
                when = fmt_time(seen) if seen else "nothing received since the service started"
                self.alerter.fire(
                    f"shelly_silent:{prefix}", "shelly_silent",
                    f"No check-in from {name} in {fmt_duration(now - last)}",
                    f"Last message: {when}.\nCheck the sensor's batteries and its Wi-Fi/MQTT settings.")
        if (not self.connected and self.disconnected_since
                and now - self.disconnected_since > self.mqtt_down_after):
            self.alerter.fire(
                "mqtt_down", "mqtt_down", "Cannot reach the MQTT broker",
                f"No connection to {self.host}:{self.port} since {fmt_time(self.disconnected_since)}.\n"
                "Shelly leak alerts will not arrive until this is fixed (sudo systemctl status mosquitto).")


def start_webhook(cfg, shelly):
    """Start the optional HTTP listener (/shelly?sensor=...&state=...&token=...) in a daemon
    thread. Requests need the shared token (constant-time compare); anything else gets 404.
    Returns the server, or None if no token is set.
    """
    token = cfg.get("webhook", "token")
    if not token:
        log.error("[webhook] is enabled but has no token; not starting it")
        return None
    listen = cfg.get("webhook", "listen", "0.0.0.0")
    port = cfg.getint("webhook", "port", 8765)

    class Handler(BaseHTTPRequestHandler):
        """HTTP handler for the webhook. GET and POST behave the same."""
        def do_GET(self):
            """Validate path and token, then pass sensor/state to ShellyMonitor.webhook()."""
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path.rstrip("/") != "/shelly" or not hmac.compare_digest(q.get("token", ""), token):
                self.send_response(404)
                self.end_headers()
                return
            ok = shelly.webhook(q.get("sensor", ""), q.get("state", "").lower())
            self.send_response(200 if ok else 400)
            self.end_headers()
            self.wfile.write(b"ok\n" if ok else b"unknown state\n")

        do_POST = do_GET

        def log_message(self, fmt, *args):
            """Route access logs to debug logging."""
            log.debug("webhook %s: %s", self.client_address[0], fmt % args)

    srv = ThreadingHTTPServer((listen, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("webhook listening on %s:%s", listen, port)
    return srv


# ----------------------------------------------------------------------------- Flume

class FlumeError(Exception):
    """Raised for Flume API/sign-in problems."""
    pass


class FlumeClient:
    """Minimal Flume cloud API client (password-grant OAuth). Unused at this site (no water
    meter) but kept for reuse.
    """
    def __init__(self, cfg):
        """Read Flume credentials and timezone from [flume]."""
        self.creds = {
            "grant_type": "password",
            "client_id": cfg.get("flume", "client_id"),
            "client_secret": cfg.get("flume", "client_secret"),
            "username": cfg.get("flume", "username"),
            "password": cfg.get("flume", "password"),
        }
        tz = cfg.get("flume", "timezone")
        self.tz = ZoneInfo(tz) if tz else None
        self.token = None
        self.expires = 0
        self.user_id = None

    def login(self):
        """Sign in; store the bearer token and read the user id from its JWT payload."""
        if not all(self.creds[k] for k in ("client_id", "client_secret", "username", "password")):
            raise FlumeError("Flume credentials are incomplete in [flume]")
        try:
            r = requests.post(f"{FLUME_API}/oauth/token", json=self.creds, timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise FlumeError(f"cannot reach Flume: {e}")
        if r.status_code != 200:
            raise FlumeError(f"Flume sign-in failed ({r.status_code}): {r.text[:300]}")
        try:
            d = r.json()["data"][0]
            self.token = d["access_token"]
            seg = self.token.split(".")[1]
            seg += "=" * (-len(seg) % 4)
            self.user_id = json.loads(base64.urlsafe_b64decode(seg))["user_id"]
        except (KeyError, IndexError, ValueError) as e:
            raise FlumeError(f"unexpected Flume sign-in response: {e}")
        self.expires = time.time() + int(d.get("expires_in", 3600)) - 300

    def request(self, method, path, **kw):
        """Call the API, signing in as needed (one re-login on 401). '{uid}' in path is replaced
        with the user id. Returns parsed JSON or raises FlumeError.
        """
        for attempt in (0, 1):
            if not self.token or time.time() >= self.expires:
                self.login()
            url = FLUME_API + path.format(uid=self.user_id)
            try:
                r = requests.request(method, url, timeout=HTTP_TIMEOUT,
                                     headers={"Authorization": f"Bearer {self.token}"}, **kw)
            except requests.RequestException as e:
                raise FlumeError(f"cannot reach Flume: {e}")
            if r.status_code == 401 and attempt == 0:
                self.token = None
                continue
            if r.status_code == 429:
                raise FlumeError("Flume API rate limit hit - increase poll_seconds")
            if r.status_code != 200:
                raise FlumeError(f"Flume {path} returned {r.status_code}: {r.text[:200]}")
            try:
                body = r.json()
            except ValueError:
                raise FlumeError(f"Flume {path} returned non-JSON")
            if isinstance(body, dict) and body.get("success") is False:
                raise FlumeError(f"Flume error: {body.get('message') or body}")
            return body
        raise FlumeError("Flume rejected the sign-in token twice")

    def devices(self):
        """List the account's meter sensors (device type 2)."""
        body = self.request("GET", "/users/{uid}/devices", params={"user": "false", "location": "false"})
        return [d for d in body.get("data") or [] if d.get("type") == 2]  # type 2 = meter sensor

    def minute_usage(self, dev, minutes):
        """Gallons per minute for the last `minutes` minutes, oldest first: [(datetime, gallons)]."""
        now = dt.datetime.now(self.tz) if self.tz else dt.datetime.now()
        now = now.replace(second=0, microsecond=0)
        since = now - dt.timedelta(minutes=minutes)
        q = {"queries": [{
            "request_id": "recent",
            "bucket": "MIN",
            "since_datetime": since.strftime("%Y-%m-%d %H:%M:%S"),
            "until_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "units": "GALLONS",
        }]}
        body = self.request("POST", f"/users/{{uid}}/devices/{dev}/query", json=q)
        try:
            rows = body["data"][0]["recent"]
        except (KeyError, IndexError, TypeError):
            raise FlumeError(f"unexpected Flume query response: {str(body)[:200]}")
        out = []
        for row in rows:
            try:
                t = dt.datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
                out.append((t, float(row.get("value") or 0)))
            except (KeyError, ValueError, TypeError):
                continue
        out.sort()
        return out

    def leak_active(self, dev):
        """True if Flume's own leak detection currently flags a leak on this device."""
        body = self.request("GET", f"/users/{{uid}}/devices/{dev}/leaks/active")
        return any(isinstance(i, dict) and i.get("active") for i in body.get("data") or [])


class FlumeMonitor:
    """Polls Flume and raises flow alerts: sustained high flow, nonstop flow, quiet-hours use,
    Flume's own leak flag, plus device offline/battery/API-down. Disabled at this site
    ([flume] enabled = false).
    """
    def __init__(self, cfg, alerter):
        """Read thresholds from [flume] (all have code defaults)."""
        self.api = FlumeClient(cfg)
        self.alerter = alerter
        self.device_ids = [d.strip() for d in cfg.get("flume", "device_id").split(",") if d.strip()]
        self.poll_seconds = max(60, cfg.getint("flume", "poll_seconds", 180))
        self.min_gpm = cfg.getfloat("flume", "running_gpm", 0.05)
        self.high_gpm = cfg.getfloat("flume", "high_flow_gpm", 5.0)
        self.high_min = cfg.getint("flume", "high_flow_minutes", 10)
        self.cont_min = cfg.getint("flume", "continuous_flow_minutes", 60)
        qs, qe = cfg.get("flume", "quiet_start"), cfg.get("flume", "quiet_end")
        self.quiet = (parse_hhmm(qs), parse_hhmm(qe)) if qs and qe else None
        self.quiet_label = f"{qs}-{qe}"
        self.quiet_min = cfg.getint("flume", "quiet_minutes", 10)
        self.quiet_gallons = cfg.getfloat("flume", "quiet_gallons", 1.0)
        self.delay_min = cfg.getint("flume", "data_delay_minutes", 2)
        self.api_fail_min = cfg.getint("flume", "api_failure_minutes", 30)
        self.devs = []
        self.first_failure = None
        self.next_poll = 0
        self.next_device_check = 0

    def label(self, dev):
        """Short name for a meter in messages."""
        return "water meter" if len(self.devs) <= 1 else f"water meter ...{dev[-4:]}"

    def poll(self):
        """Run one poll cycle if poll_seconds has elapsed. Alerts if the API keeps failing for
        api_failure_minutes; clears when it recovers.
        """
        now = time.time()
        if now < self.next_poll:
            return
        self.next_poll = now + self.poll_seconds
        try:
            if now >= self.next_device_check or not self.devs:
                self._check_devices()
                self.next_device_check = now + 3600
            for dev in self.devs:
                self._poll_device(dev)
        except FlumeError as e:
            log.error("Flume: %s", e)
            if self.first_failure is None:
                self.first_failure = now
            if now - self.first_failure >= self.api_fail_min * 60:
                self.alerter.fire(
                    "flume_api", "flume_api", "Flume API not responding",
                    f"Flume checks have failed since {fmt_time(self.first_failure)}.\nLast error: {e}\n"
                    "Flow and leak alerts from Flume are paused until this recovers.")
            return
        if self.first_failure is not None:
            self.first_failure = None
            self.alerter.clear("flume_api", "Flume API responding again")

    def _check_devices(self):
        """Refresh the device list (hourly) and alert on offline or low battery."""
        devs = self.api.devices()
        by_id = {d.get("id"): d for d in devs}
        missing = [x for x in self.device_ids if x not in by_id]
        if missing:
            log.warning("Flume device id(s) not found on the account: %s", ", ".join(missing))
        self.devs = self.device_ids or [d for d in by_id if d]
        if not self.devs:
            raise FlumeError("no Flume meter sensors found on this account")
        for dev in self.devs:
            d = by_id.get(dev, {})
            label = self.label(dev)
            if d.get("connected") is False:
                self.alerter.fire(f"flume_offline:{dev}", "flume_offline", f"Flume {label} is offline",
                                  f"Flume says the sensor is not connected (last seen {d.get('last_seen', 'unknown')}).\n"
                                  "Check the Flume bridge's power and Wi-Fi.")
            elif d.get("connected") is True:
                self.alerter.clear(f"flume_offline:{dev}", f"Flume {label} back online")
            batt = str(d.get("battery_level", "")).lower()
            if batt in ("low", "critical"):
                self.alerter.fire(f"flume_battery:{dev}", "flume_low_battery",
                                  f"Flume {label} battery is {batt}", "Replace the Flume sensor's batteries.")
            elif batt:
                self.alerter.clear(f"flume_battery:{dev}", f"Flume {label} battery OK")

    def _poll_device(self, dev):
        """Fetch recent per-minute usage and evaluate the four flow rules. The newest
        data_delay_minutes are ignored because Flume uploads late.
        """
        label = self.label(dev)
        lookback = max(self.high_min, self.cont_min, self.quiet_min if self.quiet else 0) + self.delay_min + 2
        series = self.api.minute_usage(dev, lookback)
        # Drop the newest minutes; Flume may not have uploaded them yet.
        if self.delay_min and series:
            cutoff = series[-1][0] - dt.timedelta(minutes=self.delay_min - 1)
            series = [s for s in series if s[0] < cutoff]
        values = [v for _, v in series]
        current = values[-1] if values else 0.0
        log.debug("Flume %s: last %d min, current %.2f gal/min", dev, len(values), current)

        def tail(n):
            """Last n per-minute values, or None if fewer than n are available."""
            return values[-n:] if n > 0 and len(values) >= n else None

        # 1) sustained high flow (burst pipe, open hose)
        key = f"flume_high_flow:{dev}"
        t = tail(self.high_min)
        if t and all(v >= self.high_gpm for v in t):
            self.alerter.fire(key, "flume_high_flow",
                              f"High water flow - {current:.1f} gal/min for {self.high_min}+ min",
                              f"The Flume {label} has seen at least {self.high_gpm} gal/min every minute for "
                              f"{self.high_min}+ minutes. This can mean a burst pipe or something left open.")
        elif t and t[-1] < self.high_gpm:
            self.alerter.clear(key, "Water flow back below the high-flow limit",
                               f"Current flow: {current:.2f} gal/min.")

        # 2) water running nonstop (running toilet, slow leak)
        key = f"flume_continuous:{dev}"
        t = tail(self.cont_min)
        if self.cont_min > 0 and t and all(v >= self.min_gpm for v in t):
            self.alerter.fire(key, "flume_continuous_flow",
                              f"Water running nonstop for {self.cont_min}+ min",
                              f"The Flume {label} has shown water flowing every minute for {self.cont_min}+ "
                              f"minutes (now {current:.2f} gal/min). Common causes: a running toilet, a dripping "
                              "fixture, or a leak.")
        elif t and t[-1] < self.min_gpm:
            self.alerter.clear(key, "Nonstop water flow has stopped")

        # 3) use during quiet hours
        if self.quiet:
            key = f"flume_quiet:{dev}"
            recent = series[-self.quiet_min:] if len(series) >= self.quiet_min else []
            inside = recent and all(in_window(ts.time(), *self.quiet) for ts, _ in recent)
            used = sum(v for _, v in recent)
            if inside and used >= self.quiet_gallons:
                self.alerter.fire(key, "flume_quiet_hours",
                                  f"Water used during quiet hours ({self.quiet_label})",
                                  f"{used:.1f} gallons in the last {self.quiet_min} minutes on the Flume {label}.")
            elif recent and (not inside or used < self.quiet_gallons):
                self.alerter.clear(key, "Quiet-hours water use has stopped")

        # 4) Flume's own leak detection
        key = f"flume_leak:{dev}"
        if self.api.leak_active(dev):
            self.alerter.fire(key, "flume_leak", "Flume reports a possible leak",
                              f"Flume's leak detection flagged the {label}. Open the Flume app for details.")
        else:
            self.alerter.clear(key, "Flume leak alert cleared")


# ----------------------------------------------------------------------------- commands

def site_name(cfg):
    """Name shown in texts and emails; falls back to the server's hostname."""
    return cfg.get("general", "site_name") or socket.gethostname()


def build(cfg, ephemeral=False):
    """Create the shared pieces: (state_dir, State, Mailer, ZoomSMS or None, Alerter).
    ephemeral=True uses in-memory state and creates no files, for CLI test commands.
    """
    state_dir = cfg.get("general", "state_dir", "/var/lib/water-alert")
    if not ephemeral:
        os.makedirs(state_dir, exist_ok=True)
    state = State(None if ephemeral else os.path.join(state_dir, "state.json"))
    mailer = Mailer(cfg)
    sms = ZoomSMS(cfg, state_dir) if cfg.getbool("zoom", "enabled", True) else None
    return state_dir, state, mailer, sms, Alerter(cfg, state, mailer, sms)


def cmd_run(cfg, args):
    """Main service loop (what systemd runs). Starts the enabled monitors, emails 'Monitor
    started', then every 15 s polls Flume, runs the Shelly watchdog and saves state. Every 12
    h it force-refreshes the Zoom token to keep the refresh chain alive, raising a 'zoom_auth'
    alert if that breaks. Stops cleanly on SIGTERM/SIGINT.
    """
    _, state, mailer, sms, alerter = build(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    shelly = flume = web = None
    watching = []
    if cfg.getbool("shelly", "enabled", True):
        shelly = ShellyMonitor(cfg, state, alerter)
        # Forget check-ins from names no longer in [shelly_sensors] (renamed or removed sensors).
        with state.lock:
            for prefix in [p for p in state.data["last_seen"] if p not in shelly.sensors]:
                del state.data["last_seen"][prefix]
        shelly.start()
        watching.append(f"Shelly Flood via MQTT ({len(shelly.sensors)} sensor(s) listed)")
        if cfg.getbool("webhook", "enabled", False):
            web = start_webhook(cfg, shelly)
    if cfg.getbool("flume", "enabled", True):
        flume = FlumeMonitor(cfg, alerter)
        watching.append(f"Flume every {flume.poll_seconds}s")
    if sms is None:
        log.warning("texting is disabled ([zoom] enabled = false)")
    elif not sms.recipients:
        log.warning("no [sms_recipients] configured; critical alerts will be email-only")
    log.info("water-alert running: %s", "; ".join(watching) or "nothing enabled!")

    if cfg.getbool("general", "notify_on_start", True):
        active = [a.get("subject", k) for k, a in state.data["alerts"].items() if a.get("active")]
        body = (f"Water alert monitor for {site_name(cfg)} started at {fmt_time()} (server {socket.gethostname()}).\n"
                f"Watching: {'; '.join(watching) or 'nothing'}\n"
                f"Text recipients: {len(sms.recipients) if sms else 0}\n"
                f"Alerts already active: {', '.join(active) or 'none'}")
        try:
            mailer.send("Monitor started", body)
        except Exception as e:
            log.error("startup email failed: %s", e)

    next_zoom_check = time.time() + 60
    while not stop.is_set():
        try:
            if flume:
                flume.poll()
            if shelly:
                shelly.check()
            if sms and time.time() >= next_zoom_check:
                # Refreshing twice a day keeps the Zoom sign-in alive and catches problems early.
                try:
                    sms.access_token(force_refresh=True)
                    sms.user_id()
                    alerter.clear("zoom_auth", "Zoom texting is working again")
                    next_zoom_check = time.time() + 12 * 3600
                except ZoomAuthError as e:
                    alerter.fire("zoom_auth", "zoom_auth", "Zoom texting is broken - leak texts will NOT go out",
                                 f"{e}\n\nFix: sudo water-alert zoom-auth   (no restart needed)\n"
                                 "Email alerts still work.")
                    next_zoom_check = time.time() + 1800
            state.save()
        except Exception:
            log.exception("error in main loop")
        stop.wait(15)

    log.info("stopping")
    if shelly:
        shelly.stop()
    if web:
        web.shutdown()
    state.save()


def cmd_zoom_auth(cfg, args):
    """Interactive one-time Zoom sign-in: print the approval link, take back the pasted redirect
    URL (or code), save tokens and show the signed-in user. Re-run after changing app scopes.
    """
    _, _, _, sms, _ = build(cfg, ephemeral=True)
    if sms is None:
        raise SystemExit("[zoom] enabled = false in the config")
    print("\n1) Open this link in a browser where you are signed in to Zoom as the sender:\n")
    print("   " + sms.authorize_url() + "\n")
    print("2) Approve the app. Your browser then goes to the redirect page. It's fine if that page")
    print("   shows an error. Copy the whole address from the address bar and paste it here.\n")
    pasted = input("Address (or just the code): ").strip()
    if "code=" in pasted:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
    else:
        code = pasted
    if not code:
        raise SystemExit("No code found.")
    sms.exchange_code(code)
    me, numbers = sms.whoami()
    print(f"\nSigned in as {me.get('email')} (user id {me['id']}). Tokens saved to {sms.token_path}")
    if numbers:
        print("Zoom Phone numbers on this user: " + ", ".join(numbers))
        if sms.sender_number and sms.sender_number not in numbers:
            print(f"WARNING: sender_number {sms.sender_number} is not one of these numbers.")
    else:
        print("(Could not list your Zoom Phone numbers; that only needs the optional phone:read scope.)")
    print("\nNext: sudo water-alert test-sms")


def cmd_test_email(cfg, args):
    """Send a test email."""
    _, _, mailer, _, _ = build(cfg, ephemeral=True)
    mailer.send("Test email", f"Test from {site_name(cfg)} water alerts at {fmt_time()}.\nEmail alerts work.")
    print(f"Sent to {', '.join(mailer.to)}")


def cmd_test_sms(cfg, args):
    """Send a test text to every recipient, or to --to NUMBER. Exits non-zero if any send failed."""
    _, _, _, sms, _ = build(cfg, ephemeral=True)
    if sms is None:
        raise SystemExit("[zoom] enabled = false in the config")
    targets = [("command line", normalize_number(args.to))] if args.to else sms.recipients
    if not targets:
        raise SystemExit("No [sms_recipients] configured (or use --to NUMBER)")
    n = dt.datetime.now()
    text = ascii_only(f"Test from {site_name(cfg)} water alerts: texting works. "
                      f"{n.month}/{n.day} {n.strftime('%I:%M%p').lstrip('0')}")
    results = sms.send(text, targets)
    for name, number, ok, detail in results:
        print(f"{'OK  ' if ok else 'FAIL'} {name:20} {number:15} {detail}")
    sys.exit(0 if all(r[2] for r in results) else 1)


def cmd_test_flume(cfg, args):
    """Sign in to Flume and print devices, the last 15 minutes of flow and leak status."""
    api = FlumeClient(cfg)
    api.login()
    print(f"Signed in to Flume (user id {api.user_id})")
    devs = api.devices()
    if not devs:
        print("No meter sensors found on this account.")
        return
    for d in devs:
        dev = d["id"]
        print(f"\nDevice {dev}: connected={d.get('connected')} battery={d.get('battery_level')} "
              f"last_seen={d.get('last_seen')}")
        series = api.minute_usage(dev, 15)
        print("  last 15 minutes (gallons/min):")
        for ts, v in series:
            print(f"    {ts:%H:%M}  {v:6.2f}")
        print(f"  Flume leak alert active: {api.leak_active(dev)}")
    print("\nIf more than one device is listed, put the one(s) you want in [flume] device_id.")


def cmd_simulate(cfg, args):
    """Push a fake leak through the real alert path (texts + email, marked TEST). Uses in-memory
    state, so it never affects live alerts.
    """
    _, _, _, _, alerter = build(cfg, ephemeral=True)
    sensors = cfg.section("shelly_sensors")
    name = args.sensor or (next(iter(sensors.values())) if sensors else "Test sensor")
    print(f"Routing for shelly_flood = {alerter.severity('shelly_flood')}")
    alerter.fire("simulate", "shelly_flood", f"TEST - Water detected - {name}",
                 "This is a test of the leak alert system. No water was actually detected.")
    print("Done. Check your phone and email.")


def cmd_status(cfg, args):
    """Print active alerts, last check-in per sensor and Zoom sign-in status (read-only)."""
    state_dir = cfg.get("general", "state_dir", "/var/lib/water-alert")
    data = read_json(os.path.join(state_dir, "state.json"), {})
    alerts = data.get("alerts", {})
    active = {k: a for k, a in alerts.items() if a.get("active")}
    print("Active alerts:" if active else "Active alerts: none")
    for k, a in active.items():
        print(f"  {a.get('subject', k)}  (since {fmt_time(a.get('since'))})")
    print("\nSensor check-ins:")
    sensors = cfg.section("shelly_sensors")
    seen = data.get("last_seen", {})
    for prefix in sorted(set(sensors) | set(seen)):
        ts = seen.get(prefix)
        label = sensors.get(prefix, "(not in [shelly_sensors])")
        print(f"  {prefix:30} {label:30} {fmt_time(ts) + f' ({fmt_duration(time.time() - ts)} ago)' if ts else 'never'}")
    tok = read_json(os.path.join(state_dir, "zoom_tokens.json"), {})
    print(f"\nZoom: {'signed in as ' + tok.get('user_email', '?') if tok.get('refresh_token') else 'NOT signed in'}")


def main():
    """Parse arguments, load config, set up logging and dispatch to a cmd_* function."""
    ap = argparse.ArgumentParser(description="Leak and flow alerts: Shelly Flood + Flume -> Zoom SMS + email")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("zoom-auth")
    sub.add_parser("test-email")
    p = sub.add_parser("test-sms")
    p.add_argument("--to", help="send only to this number")
    sub.add_parser("test-flume")
    p = sub.add_parser("simulate-leak")
    p.add_argument("--sensor", help="sensor name to show in the test alert")
    sub.add_parser("status")
    args = ap.parse_args()

    cfg = Config(args.config)
    level = "DEBUG" if args.verbose else cfg.get("general", "log_level", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(levelname)s %(message)s" if args.cmd == "run"
                        else "%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    handlers = {
        "run": cmd_run, "zoom-auth": cmd_zoom_auth, "test-email": cmd_test_email,
        "test-sms": cmd_test_sms, "test-flume": cmd_test_flume,
        "simulate-leak": cmd_simulate, "status": cmd_status,
    }
    try:
        handlers[args.cmd](cfg, args)
    except (ZoomAuthError, FlumeError, ValueError) as e:
        if args.cmd == "run":
            raise
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
