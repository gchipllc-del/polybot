"""
Web Dashboard Server — Flask API + HTML dashboard at localhost:5050.

Usage:
    python main.py dashboard
    python main.py dashboard --port 8080

Security:
    - Binds to 127.0.0.1 only (not exposed to network)
    - No secrets in any API response
    - Read-only endpoints (no mutations via web)
"""

from pathlib import Path

from flask import Flask, jsonify, render_template

from lib.dashboard_data import (
    get_calibration_data,
    get_circuit_breaker_status,
    get_events,
    get_full_dashboard_state,
    get_portfolio_summary,
    get_positions_table,
    get_trade_history,
)
from lib.resolution_tracker import get_performance_summary

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/state")
def api_state():
    return jsonify(get_full_dashboard_state())


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(get_portfolio_summary())


@app.route("/api/positions")
def api_positions():
    return jsonify(get_positions_table())


@app.route("/api/calibration")
def api_calibration():
    return jsonify(get_calibration_data())


@app.route("/api/events")
def api_events():
    return jsonify(get_events(30))


@app.route("/api/history")
def api_history():
    return jsonify(get_trade_history())


@app.route("/api/performance")
def api_performance():
    return jsonify(get_performance_summary())


@app.route("/api/breakers")
def api_breakers():
    return jsonify(get_circuit_breaker_status())


def run_dashboard(port: int = 5050):
    """Start the dashboard web server. Binds to localhost only.

    Fails fast with a helpful message if the port is already bound, so two
    dashboards on the same port can't silently conflict. See also the sibling
    traderbot project, which defaults to 5051 to avoid collision with polybot.
    """
    import errno
    import socket

    # Pre-flight check: confirm the port is free before Flask starts, so we
    # can emit an actionable error instead of a Werkzeug stack trace.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f"ERROR: Port {port} is already in use on 127.0.0.1.")
            print(f"       Another dashboard may already be running.")
            print(f"       Check with:  lsof -i :{port}")
            print(f"       Or pick a different port:  python main.py dashboard --port <N>")
            raise SystemExit(2)
        raise
    finally:
        probe.close()

    print(f"Polybot Dashboard: http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
