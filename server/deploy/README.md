# Hosting: MongoDB Atlas + Vultr

The map server runs on a Vultr VM behind Caddy (HTTPS), and stores data in
MongoDB Atlas. The boxes then upload over the internet, so they don't need to
be on the same network as anything.

Secrets (the Atlas connection string and the upload key) only ever go in
`server/.env` on the VM and `night-noise-box/.env` on your laptop. Both are
ignored by git.

## 1. Atlas

1. Create a cluster. The free tier is enough. Pick a region close to the Vultr
   region you'll use.
2. **Database Access:** add a database user with read/write access.
3. **Network Access:** add the Vultr VM's IP once you have it (step 2). While
   testing from a laptop you can add your own IP too.
4. **Connect → Drivers → Python:** copy the `mongodb+srv://...` string and put
   the user's password in it.

The server creates the collections itself on first start: `readings` (a
time-series collection), `stations` (with a geospatial index) and `events`.

## 2. Vultr VM

Create a Cloud Compute instance with Ubuntu 24.04. The smallest size is fine.
Then, as root over SSH:

```bash
apt update && apt install -y git python3-venv caddy
useradd --system --home /opt/noise-map --shell /usr/sbin/nologin noisemap
git clone https://github.com/AkseliHyv/junction2026-vendetta.git /opt/noise-map
python3 -m venv /opt/noise-map/venv
/opt/noise-map/venv/bin/pip install -r /opt/noise-map/server/requirements.txt
```

If `apt` can't find `caddy`, install it from Caddy's own repository
(caddyserver.com/docs/install). If the GitHub repository is private, copy it
over with `scp -r` or `rsync` instead of `git clone`.

Create the secrets file:

```bash
cp /opt/noise-map/server/.env.example /opt/noise-map/server/.env
nano /opt/noise-map/server/.env      # paste MONGODB_URI, set NOISE_MAP_KEY
chown -R noisemap: /opt/noise-map && chmod 600 /opt/noise-map/server/.env
```

Generate the key with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

Start the server and HTTPS:

```bash
cp /opt/noise-map/server/deploy/noise-map.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now noise-map
journalctl -u noise-map -f      # should say "storing data in MongoDB"

nano /opt/noise-map/server/deploy/Caddyfile     # your domain, or <VM IP>.sslip.io
cp /opt/noise-map/server/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

If the VM has a firewall enabled (`ufw status`), allow `22`, `80` and `443`.
Port 8080 stays closed; only Caddy talks to the server.

## 3. Boxes

In `night-noise-box/python/config.py`:

```python
READINGS_URL = "https://<your domain or IP.sslip.io>/api/readings"
```

In `night-noise-box/.env` (copy `.env.example`), the same key as the server:

```
NOISE_MAP_KEY=...
```

Then `night-noise-box/deploy.sh <board>` and press Run in App Lab. `deploy.sh`
copies the `.env` file to the board along with the app.

## Demo fleet

`mock-fleet.service` runs the mock boxes listed in `mock-devices/demo-boxes.txt`
nonstop, so the map always has data, with one upload per box every 5 minutes.

```bash
cp /opt/noise-map/server/deploy/mock-fleet.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now mock-fleet
journalctl -u mock-fleet -f
```

To change which boxes run, edit `demo-boxes.txt` and `systemctl restart mock-fleet`.

### Staying inside the Atlas free tier

The free cluster has 512 MB of storage and a limit on operations per second.
With ~140 boxes uploading every 5 minutes the database levels off at about
200 MB, because `noise-map.service` keeps raw payloads for 1 hour
(`--raw-hours 1`) and individual readings for 2 days (`--readings-days 2`).
The 5-minute and hourly summaries are kept 8 days (`AGG_KEEP_DAYS` in
`server/summaries.py`) and take the most space as the fleet grows: each box
adds roughly 1 MB of summaries per 8 days and 0.4 MB of readings per 2 days.
Watch storage and operations under the cluster's **Metrics** tab in Atlas.

## Updating

```bash
cd /opt/noise-map && sudo -u noisemap git pull && sudo -u noisemap git -C /opt/noise-hunt pull
cp server/deploy/noise-map.service server/deploy/mock-fleet.service /etc/systemd/system/
systemctl daemon-reload && systemctl restart noise-map mock-fleet
```

Copying the service files every time is what keeps them in step with the code
(for example when a file they point to is renamed).

## Checking it

```bash
curl https://<host>/api/readings | head -c 300          # public, read by the map
curl -X POST https://<host>/api/readings -d '{}'        # should say "missing or wrong key"
```
