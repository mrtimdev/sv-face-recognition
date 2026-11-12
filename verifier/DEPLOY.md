# Deploy the face-angle verifier

This deploys `sv_face_verifier.server` as a private Docker service on
a Linux VPS and makes it available to SV HRM over HTTPS. The VPS needs a public
DNS name and inbound ports 80 and 443 available for Caddy to obtain and renew a
TLS certificate. The verifier itself is not published on the VPS's port 8091.

## 1. Prepare the VPS

Use an Ubuntu VPS with Docker Engine and the Docker Compose plugin installed.
Allow inbound TCP ports 80 and 443 in both the VPS provider's firewall/security
group and the OS firewall. For example, with UFW:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Do not open port 8091. If another web server already owns ports 80 or 443,
configure that server to proxy HTTPS traffic to Caddy instead, or choose a
different deployment plan before starting this stack.

Create a DNS `A` record for a name such as `verifier.example.com` pointing to
the VPS's public IPv4 address. If you publish an `AAAA` record, it must point to
a working IPv6 address on this VPS as well. Wait for DNS to resolve before
starting Caddy.

## 2. Get the project and configure the service

Clone or copy this repository to the VPS, then work in its `verifier`
directory:

```bash
cd /path/to/sv-face-recognition/verifier
```

Set the verifier hostname:

```bash
printf 'VERIFIER_DOMAIN=verifier.example.com\n' > .env
chmod 600 .env
```

Generate a strong shared key directly into the ignored environment file:

```bash
umask 077
printf 'SV_FACE_VERIFIER_KEY=' > verifier.env
openssl rand -hex 32 >> verifier.env
chmod 600 verifier.env
```

Keep the key private. Do not commit `verifier.env`; the included `.gitignore`
excludes it. Configure this exact key as `FACE_VERIFICATION_KEY` for SV HRM.
Generate a different key for each deployment.

## 3. Build and start

From `verifier`:

```bash
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs --tail=100 verifier caddy
```

Compose builds the image from the repository root, taking only the verifier
and the desktop app's face code and models. Caddy listens on ports 80/443, obtains a certificate for
`VERIFIER_DOMAIN`, and forwards HTTPS requests to the verifier over a private
Docker network. The verifier only listens inside that network.

Check health from the VPS or a client machine:

```bash
curl --fail https://verifier.example.com/healthz
```

Expected response:

```json
{"status":"ok","model":"opencv-sface-2021dec-v1"}
```

The health endpoint is public and contains no secret information. Encoding
requests require the shared key. Inspect `docker compose ... logs` if startup or
certificate issuance fails; common causes are incorrect DNS, closed ports 80/443,
or another service already using those ports.

To update after changing the application, run from `verifier`:

```bash
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d
```

To stop the service without deleting its TLS certificate data:

```bash
docker compose -f compose.yaml down
```

## 4. Configure SV HRM on Windows

On the Windows device running SV HRM, configure these environment variables for
the Spring Boot process or Windows service:

```text
FACE_VERIFICATION_URL=https://verifier.example.com
FACE_VERIFICATION_KEY=<the exact value of SV_FACE_VERIFIER_KEY>
```

If running the Spring Boot JAR interactively from PowerShell, set them in that
PowerShell window before starting the application:

```powershell
$env:FACE_VERIFICATION_URL = "https://verifier.example.com"
$env:FACE_VERIFICATION_KEY = Read-Host "Paste the verifier shared key"
java -jar .\svhrm.jar
```

Replace the JAR path/name with the actual one. The key is prompted rather than
included in the command text. For an installed Windows service, set the same
variables in the service's deployment/environment configuration and restart the
service; variables set in an interactive PowerShell window do not reach an
already-running Windows service.

The Windows machine only needs outbound HTTPS access to the VPS on port 443.
Do not configure the Windows client to use `localhost` or expose port 8091:
`localhost` would refer to the Windows machine, not the VPS.

## 5. Verify the connection

From Windows PowerShell, check that the VPS is reachable:

```powershell
Invoke-RestMethod "https://verifier.example.com/healthz"
```

Then test an actual face-angle/profile upload through SV HRM. The verifier's
authenticated API is `POST /v1/faces/encode` and expects the
`x-face-verifier-key` header; HRM supplies this when configured with the
matching key. A health check alone does not verify the shared key or the HRM
integration. If uploads fail, check the Spring Boot logs and VPS logs:

```bash
cd /path/to/sv-face-recognition/verifier
docker compose -f compose.yaml logs --tail=100 verifier caddy
```

## Security notes

- Keep the verifier behind HTTPS and do not publish port 8091 directly.
- Store the shared key as a secret on both hosts; do not put it in source control,
  screenshots, or logs.
- Restrict VPS inbound traffic to the ports and sources you actually need.
- Use a trusted DNS name and keep the VPS clock and Docker packages current.
- Treat uploaded face images and generated face vectors as sensitive data.
