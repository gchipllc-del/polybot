"""
Terminal Dashboard — Rich-based colored status display for prediction markets.

Usage:
    python main.py status           # Fast: portfolio + positions + breakers
    python main.py status --full    # Includes calibration breakdown
"""

from lib.dashboard_data import (
    get_calibration_data,
    get_circuit_breaker_status,
    get_portfolio_summary,
    get_positions_table,
    get_trade_history,
)


def render_terminal_dashboard(include_calibration: bool = False):
    """Render the full terminal dashboard using Rich."""
    try:
        from rich import box
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text
    except ImportError:
        # Fallback to plain text if Rich not installed
        _render_plain()
        return

    console = Console()

    # ── Portfolio Header ──────────────────────────────────────────
    portfolio = get_portfolio_summary()

    if "error" in portfolio:
        console.print(f"[bold red]Error:[/] {portfolio['error']}")
        return

    mode = portfolio.get("mode", "manifold")
    mode_badge = "[bold yellow]PAPER[/]" if mode == "manifold" else "[bold red]LIVE[/]"
    phase = portfolio.get("phase", 1)
    phase_label = portfolio.get("phase_label", "")

    pl = portfolio.get("daily_pl", 0)
    pl_color = "green" if pl >= 0 else "red"

    cal_grade = portfolio.get("calibration_grade", "N/A")
    cal_color = {"Excellent": "green", "Good": "cyan", "Fair": "yellow", "Poor": "red"}.get(cal_grade, "dim")

    header = Text.from_markup(
        f"  Bankroll: [bold]${portfolio.get('total_bankroll', 0):,.2f}[/]  "
        f"Cash: ${portfolio.get('cash_balance', 0):,.2f}  "
        f"Positions: ${portfolio.get('position_value', 0):,.2f}\n"
        f"  Mode: {mode_badge}  Phase {phase}: {phase_label}  "
        f"Kelly: {portfolio.get('kelly_multiplier', 0.25):.0%}  "
        f"Min Edge: {portfolio.get('min_edge', 0.08):.0%}\n"
        f"  Daily P/L: [{pl_color}]${pl:+,.2f}[/]  "
        f"Calibration: [{cal_color}]{cal_grade}[/]"
        + (f" (Brier: {portfolio['brier_score']:.4f})" if portfolio.get("brier_score") else "")
    )
    console.print(Panel(header, title="[bold gold1]POLYBOT — Prediction Market Trader[/]", border_style="gold1"))

    # ── Platform Balances ─────────────────────────────────────────
    balances = portfolio.get("platform_balances", {})
    if balances:
        bal_parts = [f"  {name}: ${val}" if isinstance(val, (int, float)) else f"  {name}: {val}"
                     for name, val in balances.items()]
        console.print("  ".join(bal_parts))

    # ── Positions Table ───────────────────────────────────────────
    positions = get_positions_table()
    open_pos = [p for p in positions if p.get("status") == "open"]

    if open_pos:
        table = Table(title="Open Positions", box=box.ROUNDED, border_style="blue")
        table.add_column("Side", style="bold")
        table.add_column("Question")
        table.add_column("Platform", style="dim")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("P/L $", justify="right")
        table.add_column("P/L %", justify="right")
        table.add_column("Score", justify="center")

        for p in open_pos:
            pnl = p.get("pnl", 0)
            pnl_pct = p.get("pnl_pct", 0)
            pnl_color = "green" if pnl >= 0 else "red"

            table.add_row(
                p.get("side", "?"),
                p.get("question", "?"),
                p.get("platform", ""),
                f"${p.get('entry_price', 0):.2f}",
                f"${p.get('current_price', 0):.2f}" if p.get("current_price") else "-",
                f"[{pnl_color}]${pnl:+.2f}[/]",
                f"[{pnl_color}]{pnl_pct:+.1%}[/]",
                f"{p.get('composite_score', 0)}/9",
            )
        console.print(table)
    else:
        console.print("[dim]No open positions[/]")

    # ── Trade History Summary ─────────────────────────────────────
    history = get_trade_history()
    if history.get("total_trades", 0) > 0:
        wr = history["win_rate"]
        wr_color = "green" if wr >= 0.55 else "yellow" if wr >= 0.45 else "red"
        pnl = history["total_pnl"]
        pnl_color = "green" if pnl >= 0 else "red"

        console.print(
            f"\n  Trade History: {history['total_trades']} trades  "
            f"Win Rate: [{wr_color}]{wr:.0%}[/]  "
            f"Total P/L: [{pnl_color}]${pnl:+,.2f}[/]"
        )

    # ── Circuit Breakers ──────────────────────────────────────────
    cb = get_circuit_breaker_status()
    if "error" not in cb:
        breakers = cb.get("breakers", {})
        lines = []
        for name, b in breakers.items():
            pct = b.get("pct_used", 0)
            if b.get("tripped"):
                bullet = "[bold red]TRIPPED[/]"
            elif pct > 0.6:
                bullet = "[yellow]WARNING[/]"
            else:
                bullet = "[green]OK[/]"
            label = name.replace("_", " ").title()
            lines.append(f"  {bullet:20s} {label}: {b.get('current', 0)} / {b.get('limit', 0)}")

        paper = "[green]Yes[/]" if cb.get("paper_mode") else "[bold red]NO[/]"
        lines.append(f"\n  Paper Mode: {paper}")

        console.print(Panel("\n".join(lines), title="Circuit Breakers", border_style="yellow"))

    # ── Calibration Detail (optional) ─────────────────────────────
    if include_calibration:
        cal = get_calibration_data()
        if cal.get("brier_score") is not None:
            console.print(f"\n[bold]Calibration Detail:[/]")
            console.print(f"  Brier Score: {cal['brier_score']:.4f}")
            console.print(f"  Log Loss:    {cal['log_loss']:.4f}")

            curve = cal.get("calibration_curve", {})
            if curve:
                ct = Table(title="Calibration Curve", box=box.SIMPLE, border_style="magenta")
                ct.add_column("Bucket")
                ct.add_column("Predicted", justify="right")
                ct.add_column("Actual", justify="right")
                ct.add_column("Gap", justify="right")
                ct.add_column("N", justify="right")

                for bucket, data in curve.items():
                    gap = data.get("gap", 0)
                    gap_color = "green" if gap < 0.05 else "yellow" if gap < 0.10 else "red"
                    ct.add_row(
                        bucket,
                        f"{data['predicted_mean']:.2f}",
                        f"{data['actual_rate']:.2f}",
                        f"[{gap_color}]{gap:.3f}[/]",
                        str(data["count"]),
                    )
                console.print(ct)

            sa = cal.get("source_accuracy", {})
            if sa:
                console.print(f"\n  Source Accuracy (lower Brier = better):")
                for src, data in sa.items():
                    console.print(f"    {src:20s} Brier={data['brier']:.4f} (n={data['count']})")
        else:
            console.print("\n[dim]No resolved forecasts yet — calibration data pending[/]")
    else:
        console.print("\n[dim]Run with --full for calibration breakdown[/]")


def _render_plain():
    """Plain text fallback when Rich is not installed."""
    portfolio = get_portfolio_summary()
    if "error" in portfolio:
        print(f"Error: {portfolio['error']}")
        return

    print("=" * 60)
    print("  POLYBOT — Prediction Market Trader")
    print("=" * 60)
    print(f"  Bankroll:    ${portfolio.get('total_bankroll', 0):,.2f}")
    print(f"  Cash:        ${portfolio.get('cash_balance', 0):,.2f}")
    print(f"  Mode:        {portfolio.get('mode', 'manifold')}")
    print(f"  Phase:       {portfolio.get('phase', 1)}")
    print(f"  Daily P/L:   ${portfolio.get('daily_pl', 0):+,.2f}")
    print(f"  Calibration: {portfolio.get('calibration_grade', 'N/A')}")

    positions = get_positions_table()
    open_pos = [p for p in positions if p.get("status") == "open"]
    print(f"\n  Open Positions: {len(open_pos)}")
    for p in open_pos:
        pnl = p.get("pnl", 0)
        print(f"    {p.get('side', '?')} @ {p.get('entry_price', 0):.2f} -> "
              f"{p.get('current_price', 0):.2f} ({pnl:+.2f}) | {p.get('question', '?')}")

    print("=" * 60)
