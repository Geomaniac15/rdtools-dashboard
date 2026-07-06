"""RD Tools dashboard.

A minimal landing page that fronts the internal R&D applications. It is served
at the root of `rdtools.q-id` by nginx; each application lives behind its own
sub-path (e.g. `/light-testing/`) and is proxied to its own gunicorn service.

Run in production as `app:app` under gunicorn on 127.0.0.1:8001 (see
rdtools-dashboard.service). No database, no state — just renders the tile grid.
"""

from flask import Flask, render_template

app = Flask(__name__)

APPS = [
    {
        "name": "Light Testing",
        "description": "Q-ID light-rig tester - recipes, runs, calibration for "
        "the Auto-TTL Rigs.",
        "href": "/light-testing/",
        "icon": "◉",
        "enabled": True,
    },
    {
        "name": "Coming soon",
        "description": "Another R&D tool will live here.",
        "href": "#",
        "icon": "…",
        "enabled": False,
    },
]


@app.route("/")
def index():
    return render_template("index.html", apps=APPS)
