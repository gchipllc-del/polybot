"""main.py must load the vendored tradingcore on its own.

vendor/tradingcore only goes on sys.path when the `lib` package is imported
(lib/__init__.py). main.py used to import no lib module at top level, while six commands
(scan, trade, harvest, calibrate, hermes, chaos) import tradingcore - directly or through
agents.* - before any lib module. On every host without the real package (the Windows host
included, see requirements.txt) they died with ModuleNotFoundError.

In-process tests cannot see this: conftest.py and almost every test module import `lib`
first, which bootstraps the fallback for the whole session. So these tests start a FRESH
interpreter, the way the user runs it (`python main.py <command>`).
"""
import os
import subprocess
import sys
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
