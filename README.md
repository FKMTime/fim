# FKMTime Instance Manager
Manages FKMTime instances running on **OpenWRT** routers (or any Linux host with Docker).

## Running natively

```bash
python3 manager.py
```

Serves on `http://0.0.0.0:8181`. On OpenWrt, sign in with any LuCI username/password (same as the web UI, via rpcd). Elsewhere the default is `root` / `root` in `auth.json`.

### macOS

On macOS it is recommended to run the manager **natively** (not in Docker). 
Running natively gives Docker containers access to the host's Bluetooth and mDNS stack 
via [mdns-docker-adapter](https://github.com/filipton/docker-adapter/releases/latest).

Once the manager is running, open the **Adapter** tab in the web UI to download and enable `docker-adapter` with one click.

### Wi-Fi stats on OpenWrt

On OpenWrt, while an instance is running, the manager records the hotspot's per-station Wi-Fi stats (`iw station dump`, the in-use channel's survey, DHCP leases, load) once a minute. They are written every 10 minutes, and on stop, to `<instance>/logs/wifi-YYYY-MM-DD_HH.log.gz` (UTC hour); read them with `zcat`. Files older than 30 days are deleted, and the oldest ones while they total more than 200 MB.

### Backup uplink on OpenWrt

The WiFi tab takes an optional **backup uplink** network, such as a phone hotspot to fall back on when the venue's Wi-Fi fails. The uplink (`default_radio0`, STA mode) counts as working only while it has an IPv4 address **and** a TCP connection to port 443 on 1.1.1.1, 8.8.8.8 or 9.9.9.9 succeeds through it. That catches a network that connects but has no internet, or only a login page. When the uplink has not worked for a minute, the manager points `default_radio0` at the other network and runs `wifi reload`; only the changed radio restarts, so the hotspot and the instance stay up. While on the backup it looks for the primary every 5 minutes and switches back once the primary is in range. A return that fails doubles the wait before the next try, up to an hour. If neither network works, it keeps alternating between them every minute, so a hotspot switched on later is picked up within about two minutes.

The form has no security-type field for the backup. The manager connects with `sae-mixed`, which joins WPA2, WPA3 and mixed networks, when both `wpa_supplicant -vsae` and the radio's driver (`iw phy`) support WPA3 (SAE), and with `psk2` (WPA2) otherwise. The Raspberry Pi's built-in Wi-Fi (`brcmfmac`) does not support SAE, so it uses `psk2`; a WPA3-only network cannot be the backup there. The primary keeps the security type set in LuCI.

The backup, and a copy of the primary while the backup is in use, are kept in `<data dir>/wifi_uplink.json` (mode `0600`, it holds the passwords). Switches are logged to the manager's stdout (`logread | grep 'Uplink failover'` on OpenWrt).

### Offline web UI

The portal loads only local assets from `fim/web/static/` (`app.css`, `app.js`, `login.css`) — no CDN or external CSS/JS packages. Syntax highlighting is built into `app.js`.

## Running in Docker

The manager needs the Docker socket so it can run `docker compose` commands on the host.

### Important: same-path bind mount

The FKMTime instance templates use relative bind mounts (e.g. `./db`, `./logs`). 
Docker Compose resolves these to absolute paths and passes them to the **host** Docker 
daemon, so the data directory must be mounted at the **same absolute path** inside and outside the container.

### Quick start

```bash
export FIM_DATA_DIR=/opt/fim
mkdir -p $FIM_DATA_DIR
docker compose up -d
```

Override `FIM_DATA_DIR` in a `.env` file beside `docker-compose.yml` if you prefer a different path.

### Manual `docker run`

```bash
docker run -d \
  -p 8181:8181 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /opt/fim:/opt/fim \
  -e FIM_DATA_DIR=/opt/fim \
  fim
```
