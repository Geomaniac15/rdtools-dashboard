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
        "name": "Q-ID Tag Testing",
        "description": "Q-ID light-rig tester - recipes, runs, calibration for the Auto-TTL Rigs.",
        "href": "/light-testing/",
        "icon": "◉",
        "enabled": True,
    },
    {
        "name": "Control for Environmental Testing Chamber",
        "description": "Control and monitoring for the Q-ID environmental testing chamber.",
        "href": "/etc-control/",
        "icon": "ETC",
        "enabled": True,
    },
]


@app.route("/")
def index():
    return render_template("index.html", apps=APPS)


# Environmental Testing Chamber control — the real app isn't deployed yet, so
# serve a maintenance page. nginx routes everything except /light-testing/ here,
# so no nginx change is needed. Swap this for a proxy to the real service later.
@app.route("/etc-control/")
def etc_control():
    return render_template(
        "maintenance.html", name="Environmental Testing Chamber Control"
    ), 503
