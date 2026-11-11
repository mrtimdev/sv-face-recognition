# Run the Desktop App in a browser, 24/7

This folder runs the same Desktop App in a container, on a virtual screen you open in a browser.
It adds nothing to the installers: the only app change is `SV_FACE_UNATTENDED`, and only this container sets it.

## Choose where it runs

This decides what happens when your network drops. Nothing in software can change it: **a computer that
can't reach the camera can't see faces.**

| | **A. Office computer (recommended)** | **B. VPS only** |
|---|---|---|
| Where the app runs | A small PC in the office, beside the camera | Your VPS |
| Office internet drops | Attendance keeps working | **Attendance stops** until it returns |
| Camera video | Stays in the office | Crosses the internet all day |
| Internet use | Low | High, constant upload from the office |
| Open it from anywhere | Through the VPS tunnel below | Directly |
| Face data and attendance stored | Office computer | VPS |

For unstable Wi-Fi, choose **A**, and connect **both the camera and that PC by cable** to the same switch or
router. Then the internet can drop freely. If the camera itself stays on Wi-Fi, a Wi-Fi drop still stops
attendance in either setup, because no video arrives.

### What happens during an outage

| What fails | What the app does |
|---|---|
| Link to the camera | Shows `RECONNECTING` / `OFFLINE`, pauses attendance, and returns to `LIVE` by itself. Nobody can be checked in during the gap. |
| Internet or HRM | Keeps recognizing from its saved faces. Check-ins are saved on this computer and sent when HRM returns. Changes made in HRM arrive after it returns. |
| App crash, Quit or Relaunch | Started again within seconds, signed in and recording. |
| Computer reboot | Docker starts it again, signed in and recording, even with no internet. |

## Install

Needs Docker with the Compose plugin, on Linux x86-64 or ARM64.

```bash
git clone https://github.com/mrtimdev/sv-face-recognition.git
```

```bash
cd sv-face-recognition/frontend/deploy/stream
```

```bash
cp .env.example .env && chmod 600 .env
```

Edit `.env`: set `VNC_PASSWORD` to at least 8 characters, and check `TZ`, because attendance times use it.

```bash
docker compose up -d --build
```

The first build takes several minutes and the image is about 2.2 GB. Then open the app:

- **On that computer:** `http://127.0.0.1:6080`
- **From elsewhere:** set up HTTPS below first. To look before that, use an SSH tunnel:
  `ssh -L 6080:127.0.0.1:6080 you@server`, then open the same address.

The first time, sign in with your SV HRM account and **leave "Remember me" ticked**. Without it, the app waits at
sign-in after every restart. Then enter the camera's `rtsp://` address and the HRM API key in Settings, as on the Desktop App.

### Camera settings

- Set the I-frame interval to **2 seconds or less**. The app gives up on a connection that shows no picture
  within 2.5 seconds, so a longer interval can leave it stuck on `RECONNECTING` even with a working camera.
- Give the camera a fixed IP address, and test the stream with VLC from the same network first.

## Secure access from the internet

Anyone who gets through this reaches the app **already signed in**, including employee records and faces.
VNC only checks 8 characters of its password, so it must never be exposed directly. Keep `BIND_ADDRESS=127.0.0.1`.

On the VPS, add a password file and a site to nginx (use a real certificate, for example from certbot):

```bash
sudo htpasswd -c /etc/nginx/sv-face.htpasswd yourname
```

```nginx
server {
    listen 443 ssl;
    server_name face.example.com;
    ssl_certificate     /etc/letsencrypt/live/face.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/face.example.com/privkey.pem;

    auth_basic "SV Face ID";
    auth_basic_user_file /etc/nginx/sv-face.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:6080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;
        proxy_send_timeout 1d;
    }
}
```

Restricting it further to your office IP or a VPN is better still. Sign in with an account that has only the
permissions the screen needs, which must include starting the camera.

### Setup A: reach the office computer through the VPS

The office usually has no public address, so the office computer connects out to the VPS.
[WireGuard](https://www.wireguard.com/) or [Tailscale](https://tailscale.com/) is the sturdiest way. With either
one, point `proxy_pass` above at the office computer's VPN address, and set `BIND_ADDRESS` to that same address
in the office computer's `.env` so only the VPN can reach it.

Remote viewing stops while the office internet is down. Attendance doesn't.

### Setup B: reach the camera from the VPS

Never expose the camera's RTSP port to the internet. Connect the VPS to the office with the same VPN, and use
the camera's VPN-reachable address in the app.

## Day to day

```bash
docker compose ps
```

`healthy` means the processes are running and the attendance engine reports fresh camera frames, available storage, live workers and queue capacity. A signed-out, stopped or paused engine is `unhealthy`; sign in and start monitoring before expecting a healthy state. Check the logs for failures:

```bash
docker compose logs --tail 100
```

To update the app after pulling new code (settings, sign-in and attendance stay):

```bash
git pull && docker compose up -d --build
```

To back up everything, including faces and attendance. **Keep this private: it holds biometric data and the sign-in:**

```bash
docker run --rm -v sv-face-stream_data:/data:ro -v "$PWD":/backup python:3.12-slim-trixie tar czf /backup/sv-face-backup.tgz -C /data .
```

`docker compose down -v` **deletes all of it**, including attendance not yet sent. Plain `down` is safe.

## Limits

- **One shared screen.** Everyone who opens it sees and controls the same session.
- **No sound.** Alert sounds don't reach the browser.
- **Monitoring and record keeping only.** Enrolling with a webcam on the viewer's own computer doesn't work,
  because the app only sees cameras the container can reach. Phone enrollment needs its port exposed separately.
- **License plate reading isn't installed.** The screen opens, but reading plates needs its separate environment.
- **A frozen app isn't detected.** A crash is restarted automatically, but a hang is not. Use Relaunch, or
  `docker compose restart`.
- **Remembered sign-in.** It's renewed daily while running, and a restart after HRM has ended the session returns
  to the sign-in screen. If HRM ends the session during an outage, the app signs out once HRM answers again.

## Files

| File | Purpose |
|---|---|
| `Dockerfile`, `Dockerfile.dockerignore` | Builds the image. The allow-list keeps settings, sign-ins, attendance and faces out. |
| `compose.yaml`, `.env.example` | How it runs: restart policy, private port, data volume. |
| `supervisord.conf`, `run-app.sh`, `run-vnc.sh` | Keep the screen, browser access and app running. |
| `entrypoint.sh` | Runs at each start. Refuses short passwords and places the program in the data volume. |
| `openbox-rc.xml` | Windows fill the screen, with no desktop menu. |
