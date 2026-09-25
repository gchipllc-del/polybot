"""polybot lib package.

Bootstrap: the real `tradingcore` is a sibling repo present only on the original dev
machine, yet 34 modules here import it at module level. Its absence on a fresh clone was
the single most common cause of breakage in this project (pip aborting, dashboards
500ing, 44 test failures). If the real package is importable we use it untouched;
otherwise we put vendor/ on sys.path so the fallback shim answers instead.
"""
import sys as _sys
from pathlib import Path as _Path

try:  # real package wins wherever it is installed
    import tradingcore as _tc  # noqa: F401
except ImportError:
    _vendor = _Path(__file__).resolve().parent.parent / "vendor"
    if _vendor.is_dir() and str(_vendor) not in _sys.path:
        _sys.path.append(str(_vendor))
