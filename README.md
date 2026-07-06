# RD Tools dashboard

The landing page for **`rdtools.q-id`** — a Quantum Base R&D tools hub. It fronts
several internal applications, each living behind its own sub-path on the same
host. Currently:

| App | Path | Backend |
|-----|------|---------|
| Light Testing | `/light-testing/` | `tag-tester` gunicorn on `127.0.0.1:8000` |
| _(placeholder)_ | — | future |

This service is only the dashboard shell (the tile grid at `/`). Each app is a
separate service; nginx routes each sub-path to the right backend.

## Architecture

```
rdtools.q-id/               → nginx :80 → this dashboard (gunicorn 127.0.0.1:8001)
rdtools.q-id/light-testing/ → nginx :80 → tag-tester    (gunicorn 127.0.0.1:8000)
```

nginx strips the `/light-testing` prefix (trailing-slash `proxy_pass`); the
tag-tester app emits prefixed client URLs via its `URL_PREFIX=/light-testing`
env var so `fetch`/`href` calls resolve under the sub-path.

`rdtools` is the master Pi's Tailscale MagicDNS name (formerly `qb-light-testing`,
IP `100.86.6.122`); `q-id` is the tailnet DNS suffix. Plain HTTP over the tailnet,
no public exposure.

## Deploy

```bash
cd /home/qb/rdtools-dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt
sudo cp rdtools-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rdtools-dashboard
```

Adding an app: add an entry to `APPS` in `app.py` (set `enabled: True` and its
`href`), then add a matching `location` block in nginx pointing at that app's
gunicorn port. Logs: `journalctl -u rdtools-dashboard -f`.
