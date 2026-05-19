# FKMTime Instance Manager
Manages FKMTime instances running on **OpenWRT** routers (or any Linux host with Docker).

## Running natively

```bash
python3 manager.py
```

Serves on `http://0.0.0.0:8181`. Default credentials: `root` / `root`.

### macOS

On macOS it is recommended to run the manager **natively** (not in Docker). 
Running natively gives Docker containers access to the host's Bluetooth and mDNS stack 
via [mdns-docker-adapter](https://github.com/filipton/docker-adapter/releases/latest).

Once the manager is running, open the **Adapter** tab in the web UI to download and enable `docker-adapter` with one click.

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
