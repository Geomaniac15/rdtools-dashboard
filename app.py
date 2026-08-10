"""RD Tools dashboard.

A minimal landing page that fronts the internal R&D applications. It is served
at the root of `rdtools.q-id` by nginx; each application lives behind its own
sub-path (e.g. `/atr-l-br/`) and is proxied to its own gunicorn service.

Run in production as `app:app` under gunicorn on 127.0.0.1:8001 (see
rdtools-dashboard.service). No database, no state — it renders the tile grid and
fans an emergency stop out to the two light-rig apps.
"""
import os

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

APPS = [
    {
        "name": "ATR-L-BR",
        "description": "Light testing for the ATR-L-BR rig (Godox fixture, DMX).",
        "href": "/atr-l-br/",
        "icon": "BR",
        "enabled": True,
    },
    {
        "name": "Control for Environmental Testing Chamber",
        "description": "Control and monitoring for the Q-ID environmental testing chamber.",
        "href": "/etc-control/",
        "icon": "ETC",
        "enabled": True,
    },
    {
        "name": "ATR-L-LL",
        "description": "Light testing for the ATR-L-LL rig (Philips Hue bulb).",
        "href": "/atr-l-ll/",
        "icon": "LL",
        "enabled": True,
    },
    {"name": "Materials Lab",
    "description": "Illuminant metamerism and the SPD library — drives both light rigs.",
    "href": "/materials/", 
    "icon": "ML", 
    "enabled": True},
]

# The two light-rig controllers, by loopback port (see tag-tester's
# deploy/tag-tester@.service). Each rig app's own E-stop reaches only its own
# rig, so this is the one button that darkens both — the guarantee the single
# combined app used to give. Addressed directly rather than through nginx so it
# doesn't depend on the sub-path routing.
STOPPABLE_APPS = [
    {"name": "ATR-L-BR", "url": os.environ.get("ATR_L_BR_URL", "http://127.0.0.1:8003")},
    {"name": "ATR-L-LL", "url": os.environ.get("ATR_L_LL_URL", "http://127.0.0.1:8004")},
    {"name": "Materials Lab", "url": os.environ.get("MATERIALS_URL", "http://127.0.0.1:8005")},
]
ESTOP_TIMEOUT = 10  # generous: a rig app may itself be waiting on a slow agent


@app.route("/")
def index():
    return render_template("index.html", apps=APPS)


@app.route("/emergency-stop-all", methods=["POST"])
def emergency_stop_all():
    """Stop every run and turn every light off, on both rig apps.

    Best effort by design: one app being down must not stop the other from being
    darkened, so failures are collected and reported rather than raised.
    """
    stopped, errors = [], []
    for rig_app in STOPPABLE_APPS:
        try:
            resp = requests.post(f"{rig_app['url']}/emergency-stop",
                                 timeout=ESTOP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            stopped += [f"{rig_app['name']}: {r}" for r in data.get("stopped", [])]
            errors += [f"{rig_app['name']}: {e}" for e in data.get("errors", [])]
        except Exception as exc:  # noqa: BLE001 — report it, never abort the loop
            errors.append(f"{rig_app['name']} did not respond ({exc}) — "
                          "check that rig manually")
    return jsonify(ok=not errors, stopped=stopped, errors=errors)
