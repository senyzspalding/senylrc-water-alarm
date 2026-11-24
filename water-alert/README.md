# water-alert

Leak and water-flow alerts on an Ubuntu server.

- **Shelly Flood Gen4** sensors report to a local MQTT broker (Mosquitto), so leak alerts don't depend on the Shelly cloud.
- **Flume** (Flume 2 or F2X) is checked through the Flume cloud API every 3 minutes.
- **Critical alerts** are texted from your Zoom Phone number to each recipient separately, and emailed to noc@senylrc.org.
- **Everything else** (offline sensors, low batteries, API problems) is email only.

## What triggers what (defaults)

| Alert | Trigger | Goes to |
|---|---|---|
| Water detected | A Shelly Flood sensor reports water | **Text + email**, re-sent every 30 min until dry, then "CLEARED" |
| High water flow | Flume shows 5+ gal/min every minute for 10 min | **Text + email** |
| Water running nonstop | Water flowing every minute for 60 min (running toilet, slow leak) | Email |
| Flume leak | Flume's own leak detection flags a leak | Email |
| Quiet-hours use | 1+ gallon used within 10 min between 23:00 and 05:00 | Email |
| Sensor silent | A listed Shelly hasn't checked in for 25 h | Email |
| Low battery / Flume offline / MQTT down / Flume API down | — | Email |
| Zoom texting broken | Zoom sign-in can't refresh (checked twice a day) | Email |

You can change any of these in `[routing]` (`critical` = text + email, `warning` = email, `off`) and the thresholds in `[flume]`.

## 1. Install

Copy this folder to the server and run:

```bash
sudo ./install.sh
```

This installs Mosquitto and a Python virtual environment, creates a `wateralert` service user, adds the `water-alert` command, and generates MQTT logins. To see the logins:

```bash
sudo cat /etc/water-alert/mqtt-credentials.txt
```

The config file is `/etc/water-alert/water-alert.ini`. Re-running `install.sh` later updates the program and keeps your config.

## 2. Email

The defaults send from noreply@senylrc.org to noc@senylrc.org through the local postfix relay on port 25.

```bash
sudo water-alert test-email
```

## 3. Zoom texting

**Check in the Zoom admin portal first**
- SMS is enabled for your Zoom Phone user.
- Your number is registered for US business texting (10DLC). Carriers block unregistered texts to outside numbers.
- A quick manual check: text one of the outside numbers from the Zoom app.

**Create the app** (one time) at marketplace.zoom.us → Develop → Build App → **General App**:
1. Choose **User-managed**.
2. Set the **OAuth Redirect URL** to a page you control, for example `https://www.senylrc.org/zoom-oauth`, and add the same URL to the allow list. The page doesn't need to exist.
3. Under **Scopes**, add:
   - `phone:read:sms_message` ("View a SMS message"). Despite the name, Zoom uses this scope for *sending* texts; there is no separate write scope.
   - `user:read:user` (lets the script look up your own user id)
   - optionally, a phone read scope such as `phone:read:user`, so zoom-auth can confirm your number
4. Copy the **Client ID** and **Client Secret** into `[zoom]`.

**Fill in the rest of `[zoom]` and `[sms_recipients]`**
- Set `sender_number` to your Zoom Phone number.
- List each recipient on its own line as `name = number`: your Zoom numbers on the account and the three outside phones. Every recipient gets a separate text, so nobody sees the others' numbers and replies come back only to you.

**Sign in once**

```bash
sudo water-alert zoom-auth
```

This prints a link. Open it in a browser signed in to Zoom as yourself, then approve. The browser lands on the redirect page, and it's fine if that page shows an error. Copy the full address from the address bar and paste it back.

The service refreshes the sign-in on its own after that. If a refresh ever fails, noc@ gets an email telling you to run `zoom-auth` again.

```bash
sudo water-alert test-sms                   # every recipient
sudo water-alert test-sms --to 8455551234   # one number
```

## 4. Flume

1. At portal.flumewater.com → Settings → **API Access**, copy the client ID and secret.
2. Put them in `[flume]` along with your Flume login.
3. Run:

```bash
sudo water-alert test-flume
```

This lists your sensor, the last 15 minutes of flow, and the leak status. If more than one sensor shows up, put the one you want in `device_id`.

After about a week, look at your normal usage in the Flume app and tune `high_flow_gpm`, `continuous_flow_minutes`, and the quiet hours. Irrigation or a long hose run can trip the high-flow alert.

## 5. Shelly Flood Gen4

In each sensor's web page (or the Shelly app) → **Settings → MQTT**:

| Setting | Value |
|---|---|
| Enable MQTT | on |
| Server | `<server IP>:1883`, printed by install.sh |
| Username / password | the `shelly` login from mqtt-credentials.txt |
| Client ID / topic prefix | a name like `shellyflood-basement` |
| RPC status notifications over MQTT | on |
| Generic status update over MQTT | on |

The Flood runs on batteries and sleeps. After saving, press its button to wake it so it picks up the change.

Add each sensor to the config, using its prefix and a friendly name:

```ini
[shelly_sensors]
shellyflood-basement = Basement water heater
```

If `ufw` is on, allow only the sensor subnet to reach the MQTT port (install.sh prints the command, or pass `MQTT_ALLOW_FROM=<subnet>`).

### Securing the sensor connection

By default the broker listens on port 1883 with a password but **no encryption**, so anyone who can see that traffic can read the `shelly` password and the sensor messages. Pick one or both:

1. **Isolate the sensors.** Put them on their own VLAN/Wi-Fi and firewall them so they can reach only this server, on only the MQTT port (TCP 1883, or 8883 with TLS). Block every other path to and from the sensor network.
2. **Encrypt with TLS.** Re-run the installer with TLS on:

   ```bash
   sudo MQTT_TLS=1 ./install.sh
   ```

   This keeps 1883 working, adds a TLS listener on **8883**, and creates a private CA in `/etc/water-alert/tls/` (the CA key stays there, root only). Then, for each sensor, while it is awake:

   - Upload `/etc/mosquitto/certs/ca.crt` with the RPC `Shelly.PutUserCA` (or the device web page's certificate upload).
   - `MQTT.SetConfig` with server `<server IP>:8883` and `ssl_ca` = `user_ca.pem`, then `Shelly.Reboot`.
   - Check `sudo water-alert status` and test with a wet towel. Confirm against Shelly's Gen4 documentation for your firmware, since the exact field names and TLS options can differ between versions.

   Once **all** sensors work over 8883, close the plaintext port to the network:

   ```bash
   sudo MQTT_TLS=1 MQTT_PLAIN_LAN=0 ./install.sh
   ```

   The water-alert service talks to the broker over `localhost:1883`, which stays available on the server itself. The server certificate is valid for 10 years and names the server's IP, so re-issue it (delete `/etc/water-alert/tls/server.crt`, re-run the installer, restart mosquitto) if the server's IP changes. Sensors keep trusting the same CA.

Rotate the MQTT passwords if they were ever shared in a chat or email. Edit them with `mosquitto_passwd`, then update the `shelly` login on each sensor and `[mqtt] password` in the config.

Test by touching the sensor cable with a wet paper towel.

**Optional backup path:** set `[webhook] enabled = true` with a random `token`. In the Shelly **Actions/Webhooks**, point the flood-alarm event at
`http://<server>:8765/shelly?sensor=shellyflood-basement&state=alarm&token=<token>`
and the alarm-off event at the same URL with `state=clear`.

## 6. Start it

```bash
sudo systemctl enable --now water-alert
journalctl -u water-alert -f          # live log
sudo water-alert simulate-leak        # fake leak: full text + email path, marked TEST
sudo water-alert status               # active alerts, last check-in per sensor, Zoom sign-in
```

After a day, run `status` to see how often each Flood checks in on its own. Set `silence_hours` a little above that.

## Files

| Path | What |
|---|---|
| `/opt/water-alert/` | program + Python venv |
| `/etc/water-alert/water-alert.ini` | config (readable by root and the service only) |
| `/var/lib/water-alert/` | alert state and Zoom tokens |
| `/etc/mosquitto/conf.d/water-alert.conf` | broker listeners with password login (plus TLS on 8883 if enabled) |
| `/etc/water-alert/tls/` | private CA and server certificate (only if TLS enabled, root only) |
| `/etc/mosquitto/certs/` | CA cert, server cert and key the broker reads (only if TLS enabled) |
