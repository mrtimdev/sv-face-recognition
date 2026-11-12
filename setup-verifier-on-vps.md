Before deciding the setup, checking what's reachable: the UAT HRM at both addresses, and whether a verifier already answers there.

Both addresses reach the same UAT HRM server, and nothing answers on its port 8091, so the verifier has to run next to HRM on that server rather than on your Mac.

No preference needed, so both server setups will be covered. I'm moving the local key and the "skip missing photos" setting out of the shared application.properties into a git-ignored local.properties, since UAT on port 8082 would otherwise inherit them.

The only 64-character value in application.properties is the desktop API key, which was already there. Next I'll check that the config precedence resolves correctly across local file, missing file, and environment variable, using a throwaway Spring test that I'll delete afterward.

No output: Maven's test runner hides `System.out`. Rerunning and writing the results to a file instead.

Maven failed before the probe ran, so no result. Re-creating the probe to see the actual error; `local.properties` is back in place.

Name clash: the test method and the helper are both called `probe`. Renaming the helper and running all three cases, plus a "no local file" case.

The `uat` profile can't start because Spring Boot rejects `spring.profiles.active` inside a profile file, so the UAT server has to run the default profile instead. Rerunning the probe without the uat case next.

The probe shows the UAT server no longer picks up your local key or skip setting, since only your Mac has the local.properties values. A command-line key is overridden by local.properties on your Mac, though a real OS environment variable still wins; next I'll move on to the server steps and desktop settings.

The verification already happens in one place: svhrm checks the face on every POST/PUT of an angle or profile photo, whether it comes from the desktop app, the web profile or the phone. To make that work on UAT, run the verifier on the UAT server next to svhrm:

```
Desktop app (any network) ──HTTPS──▶ UAT HRM  uathrms.svtrucking.biz / 154.26.134.117:8082
                                        │  each angle or photo POST/PUT
                                        ▼
                                     verifier on the same server, 127.0.0.1:8091 (never public)
```

The desktop never talks to the verifier, and the UAT server can't reach the verifier on your Mac (`192.168.0.25` is a private address).

## What I changed (svhrm, uncommitted)

Your UAT server runs the **default** Spring profile. `application-uat.properties` contains `spring.profiles.active=uat`, which Spring Boot refuses inside a profile file, so that profile can't start, and UAT answers on 8082, the port in `application.properties`. That meant UAT would have inherited the local verifier key and the "skip missing photos" setting I put there last time.

- **`application.properties`:** the key now comes only from `FACE_VERIFICATION_KEY`, skipping missing photos is off, and it loads an optional `./local.properties`.
- **`local.properties`** (new, in the svhrm folder, git-ignored): holds your Mac's verifier key and turns skipping on. It exists only on your Mac.
- I checked how the settings resolve:
  - **Your Mac:** your key, skipping on.
  - **A server without that file:** empty key and skipping off, until `FACE_VERIFICATION_KEY` is set. Uploads then fail safely with "unavailable" instead of saving unchecked.

The broken `spring.profiles.active=uat` line is a separate problem. I left it alone; deleting that line would make the uat profile usable.

## Steps on the UAT server (154.26.134.117)

**1. Find out how svhrm runs there:**
```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}'; systemctl list-units --type=service | grep -i -E 'svhrm|java'; ps -eo pid,args | grep -i '[j]ava'
```

**2. Build and start the verifier on that server, on localhost only:**
```bash
git clone https://github.com/mrtimdev/sv-face-recognition.git ~/sv-face-recognition && cd ~/sv-face-recognition
```
```bash
docker build -f verifier/Dockerfile -t sv-face-verifier .
```
```bash
umask 077; openssl rand -hex 32 > ~/.sv-face-verifier-key
```
```bash
docker run -d --name sv-face-verifier --restart unless-stopped -e SV_FACE_VERIFIER_KEY="$(cat ~/.sv-face-verifier-key)" -p 127.0.0.1:8091:8091 sv-face-verifier
```
```bash
curl http://127.0.0.1:8091/healthz
```

The `clone` and `build` steps need the `verifier/` folder, which is only on your Mac until you commit and push it.

**3. Give svhrm the same key**, depending on what step 1 showed:
- **Java or systemd service:** add `Environment=FACE_VERIFICATION_KEY=<contents of ~/.sv-face-verifier-key>` to the service (`sudo systemctl edit <service>`), then restart it. The URL already defaults to `http://127.0.0.1:8091`.
- **Docker container:** inside a container, `127.0.0.1` is the container itself, so skip step 2's `docker run`. Use svhrm's `docker-compose.face-verification.yml` instead, with `SV_FACE_SOURCE_DIR=$HOME/sv-face-recognition` and `SV_FACE_VERIFIER_KEY` set in the `.env` next to the compose files. It runs the verifier in the app's own network and passes the key to both.

**4. Deploy the updated svhrm to UAT.** The verification code, the Remove button, the web DELETE route and these settings are all still uncommitted on your Mac. Until they're deployed, UAT runs its old code and doesn't call the verifier at all.

## Desktop app

The settings already fit: mode **Online** → `https://uathrms.svtrucking.biz`. Keep Online. The office address `http://154.26.134.117:8082` is a public internet address over plain HTTP, so selecting it would send the app's HRM key unencrypted over the internet. Angle changes made anywhere reach the desktop's Live Monitor through HRM's live updates, as before.

Send me the output of step 1 and I'll give you the exact commands for that setup.