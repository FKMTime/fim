# FKMTime Instance Manager
Manages FKMTime instances running on **OpenWRT** routers (or any Linux host with Docker).

## Running natively

```bash
python3 manager.py
```

Serves on `http://0.0.0.0:8181`. Default credentials: `root` / `root`.

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
