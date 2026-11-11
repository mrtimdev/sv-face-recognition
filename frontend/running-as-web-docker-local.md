I'll check the guide and compose file so the commands match what's actually in the repo.

You can run it with Docker, using the commands below. Docker is already running on your Mac, port 6080 is free, and the only thing missing is the `.env` file, which step 2 creates.

**1. Go to the folder:**

```bash
cd "/Users/timdev/Desktop/works/Python Projects/sv-face-recognition/frontend/deploy/stream"
```

**2. Create your settings file:**

```bash
cp .env.example .env && chmod 600 .env
```

**3. Set the password:** this opens the file in TextEdit. Set `VNC_PASSWORD=` to at least 8 characters and save.

```bash
open -e .env
```

**4. Build and start it:** the first build can take several minutes, and the image is about 2.2 GB.

```bash
docker compose up -d --build
```

**5. Open it in your browser:** go to http://127.0.0.1:6080 and enter the password from step 3.

**6. Set up the app:**
- Sign in with your SV HRM account and leave **Remember me** ticked.
- In Settings, enter the camera's `rtsp://` address and the HRM API key.

## Checking and stopping it
**Check attendance health:** expect `healthy` after signing in and starting the camera. A stopped, paused or signed-out engine is `unhealthy`, even when the browser is reachable.

```bash
docker compose ps
```

**See the logs if something's wrong:**

```bash
docker compose logs --tail 100
```

**Stop it:** your data is kept. Avoid adding `-v`, because `docker compose down -v` deletes all of it, including faces and any unsent attendance.

```bash
docker compose down
```

## What's different on a Mac
- **Real attendance:** if you use your real HRM account and camera, check-ins go to the real HRM. Close the Desktop App while testing, because running both on one camera could record people twice.
- **Separate data:** the container doesn't use your Desktop App's settings, sign-in or local data, so it starts empty.
- **Camera:** use a network (RTSP) camera. Docker on a Mac can't see the built-in webcam or a USB camera.
- **Staying on:** it stops while the Mac sleeps or Docker is closed, so this is good for testing but not for 24/7 use. If you don't run `docker compose down`, it starts again whenever Docker starts.
- **Speed:** Docker Desktop limits the CPU and memory containers can use. If it's slow, raise them in Docker Desktop under Settings > Resources.