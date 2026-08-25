# RD Tools dashboard

The landing page for **`rdtools.q-id`** — a Quantum Base R&D tools hub. It fronts
several internal applications, each living behind its own sub-path on the same
host. Currently:

| App | Path | Backend |
|-----|------|---------|
| ATR-L-BR (light testing, DMX/Godox rig) | `/atr-l-br/` | `tag-tester` gunicorn on `127.0.0.1:8003` (`RIG=dmx`) |
| ATR-L-SPD (light testing, Philips Hue rig) | `/atr-l-spd/` | `tag-tester` gunicorn on `127.0.0.1:8004` (`RIG=hue`) |
| Environmental Testing Chamber | `/etc-control/` | `etc-control` gunicorn on `127.0.0.1:8002` |

This service is only the dashboard shell (the tile grid at `/`, plus the
all-rigs emergency stop). Each app is a separate service; nginx routes each
sub-path to the right backend.

The two light-rig apps are **the same `tag-tester` codebase, deployed twice** —
one instance per rig, differing only in environment (`RIG`, `URL_PREFIX`, `PORT`,
`TAG_TESTER_DB`). See `deploy/tag-tester@.service` in that repo. They replace the
old combined `/light-testing/` app, which drove both rigs; `/light-testing` now
redirects here.

## Architecture

```
rdtools.q-id/             → nginx :80 → this dashboard (gunicorn 127.0.0.1:8001)
rdtools.q-id/atr-l-br/    → nginx :80 → tag-tester     (gunicorn 127.0.0.1:8003, RIG=dmx)
rdtools.q-id/atr-l-spd/   → nginx :80 → tag-tester     (gunicorn 127.0.0.1:8004, RIG=hue)
rdtools.q-id/atr-l-ll/    → 308 → /atr-l-spd/            (the rig's former name)
rdtools.q-id/etc-control/ → nginx :80 → etc-control    (gunicorn 127.0.0.1:8002)
```

nginx strips each sub-path prefix (trailing-slash `proxy_pass`); each app emits
prefixed client URLs via its own `URL_PREFIX` env var so `fetch`/`href` calls
resolve under the sub-path.

`rdtools` is the controller Pi's Tailscale MagicDNS name (formerly `qb-light-testing`,
IP `100.86.6.122`); `q-id` is the tailnet DNS suffix. Plain HTTP over the tailnet,
no public exposure.

## Emergency stop

Each light-rig app's own E-stop covers **only its own rig** — that is the price of
splitting them. The **STOP ALL LIGHTS** button in this dashboard's header is the
one control that darkens both: `POST /emergency-stop-all` fans out to each rig
app's `/emergency-stop` over loopback and reports what it managed to stop.

It is best effort by design — one app being down never stops the other from being
darkened; failures are reported, not raised. Override the backends with the
`ATR_L_BR_URL` / `ATR_L_SPD_URL` env vars if the ports ever move. (`ATR_L_LL_URL`,
the pre-rename name, is still honoured as a fallback.)

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
