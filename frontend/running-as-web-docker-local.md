# Run the browser dashboard with Docker

From `frontend/`:

```bash
mkdir -p deploy/web/data
```

Create `deploy/web/data/settings.json` with your SV HRM URL, integration key,
and camera address. The file and directory must be writable by the container
user (UID 1000 on Linux). Keep the settings file private.

```json
{
  "hrm_api_url": "https://hrm.example.com",
  "hrm_api_key": "YOUR_INTEGRATION_KEY",
  "source": "rtsp://camera.example.com:554/stream1"
}
```

```bash
docker compose -f deploy/web/compose.yaml up -d --build
```

Open [Face ID at localhost:8080](http://127.0.0.1:8080), sign in with SV HRM,
and use **Live monitor → Start engine**. On Docker Desktop, use a network
camera reachable from the container. To record on every container start, add
`command: ["--start"]` under the Compose `app` service.

The bind-mounted `deploy/web/data/` directory keeps settings, attendance,
unsent events, face templates and captures after a rebuild or shutdown. When
moving an existing installation, stop its engine before copying data and update
any configured absolute storage paths to point inside `/data`.

```bash
docker compose -f deploy/web/compose.yaml logs --tail 100
docker compose -f deploy/web/compose.yaml down
```

Docker health checks the HTTP service. Live Monitor shows camera, storage and
HRM delivery status.
