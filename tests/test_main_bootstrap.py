"""How the vendored tradingcore is bootstrapped, and kept contained under test.

1. main.py must load it on its own. vendor/tradingcore only goes on sys.path when the
   `lib` package is imported (lib/__init__.py). main.py used to import no lib module at top
   level, while six commands (scan, trade, harvest, calibrate, hermes, chaos) import
   tradingcore - directly or through agents.* - before any lib module. On every host
   without the real package (the Windows host included, see requirements.txt) they died
   with ModuleNotFoundError. In-process tests cannot see this: conftest.py and almost every
   test module import `lib` first, which bootstraps the fallback for the whole session. So
   these tests start a FRESH interpreter, the way the user runs it (`python main.py ...`).

2. The test session must keep the core's logs out of the repo's data/ (conftest.py). This
   module patches no log path of its own, so it sees exactly what conftest leaves in place.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _python(args, tmp_path):
    """A fresh interpreter in a scratch cwd. The core's logs go to tmp_path, and
    PYTHONPATH is dropped so only main.py itself can put vendor/ on the path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(TRADINGCORE_AUDIT_LOG=str(tmp_path / "audit_events.jsonl"),
               TRADINGCORE_FORECAST_LOG=str(tmp_path / "forecast_records.jsonl"),
               PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, *args], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)


def test_importing_main_bootstraps_the_core_for_every_command(tmp_path):
    """`import main`, then what each command imports first - with nothing else before."""
    code = "\n".join([
        f"import sys; sys.path.insert(0, {str(ROOT)!r})",
        "import main",
        "import tradingcore.audit         # scan, trade, harvest, chaos",
        "import tradingcore.calibration   # calibrate",
        "import tradingcore.kelly",
        "import agents.consensus          # trade (-> agents.compliance_agent)",
        "import agents.hermes_optimizer   # hermes",
        "print('bootstrapped')",
    ])
    r = _python(["-c", code], tmp_path)
    assert r.returncode == 0 and "bootstrapped" in r.stdout, r.stderr[-2000:]


def test_main_calibrate_runs_in_a_fresh_interpreter(tmp_path):
    """`python main.py calibrate` died at its first line with ModuleNotFoundError."""
    r = _python([str(ROOT / "main.py"), "calibrate"], tmp_path)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr[-2000:]
    assert r.returncode == 0, r.stderr[-2000:]


# ── 2. the test session keeps the core's logs out of the repo (conftest.py) ─────────────

_GUARDED = (ROOT / "data", ROOT / "logs")


def _under(path, dirs) -> bool:
    p = Path(path).resolve()
    return any(p == d.resolve() or d.resolve() in p.parents for d in dirs)


def _signatures(dirs) -> dict:
    """(size, mtime) of each file directly inside `dirs`: cheap, and subfolders are skipped."""
    sig = {}
    for d in dirs:
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file():
                    st = p.stat()
                    sig[p] = (st.st_size, st.st_mtime_ns)
    return sig


def test_session_keeps_core_logs_out_of_the_repo(_isolated_dirs):
    """conftest.py used to redirect lib.audit, a module that does not exist. The
    ImportError was swallowed, and every log_event a test reached appended to
    <repo>/data/audit_events.jsonl, which the dashboard lists as recent activity
    (circuit_breaker daily_loss_breached with pnl -80, forecaster market_id 'x', ...)."""
    import lib  # noqa: F401
    import tradingcore.audit as au
    import tradingcore.calibration as cal

    vendored = hasattr(au, "_LOG") and hasattr(cal, "_LOG")
    if vendored:                       # where it points, checked BEFORE anything is written
        for mod in (au, cal):
            assert not _under(mod._LOG, _GUARDED), f"{mod.__name__}._LOG = {mod._LOG}"
            assert _isolated_dirs["logs"] in Path(mod._LOG).parents

    # Any implementation: a probe written during a test must not reach the repo. The
    # unique marker keeps this race-free while the live system appends to data/ itself.
    marker = f"isolation-probe-{uuid.uuid4().hex}"
    before = _signatures(_GUARDED)
    au.log_event("conftest_isolation", marker, {"market_id": marker})
    cal.record_forecast(market_id=marker, platform="kalshi", question="probe?",
                        our_probability=0.5, market_probability=0.5, side="YES",
                        sources={}, outcome=None)     # unresolved: no test's Brier moves
    changed = [p for p, sig in _signatures(_GUARDED).items() if before.get(p) != sig]
    leaked = [str(p) for p in changed
              if marker in p.read_text(encoding="utf-8", errors="replace")]
    assert not leaked, f"test writes reached the repo: {leaked}"
    if vendored:                       # ...and they did land, in the session's tmp dir
        assert marker in Path(au._LOG).read_text(encoding="utf-8")
        assert marker in Path(cal._LOG).read_text(encoding="utf-8")
