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
    """Start the dashboard web server. Binds to localhost only."""
    print(f"Polybot Dashboard: http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
