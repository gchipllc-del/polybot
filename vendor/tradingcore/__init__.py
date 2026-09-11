"""tradingcore FALLBACK SHIM — activates ONLY when the real package is not installed.

The real tradingcore is a sibling repo (`pip install -e ../tradingcore`) that exists on
the original development machine. On any fresh clone it is absent, and because 34 modules
import it at module level, its absence cascaded into: pip aborting the whole dependency
install, the dashboard 500ing on every refresh, and 44 test failures. That single missing
package was the most common cause of breakage in this project.

This shim provides honest, minimal implementations of exactly the symbols in use so the
codebase degrades instead of dying. It is NOT a reimplementation: audit logging becomes a
local JSONL append, and the math functions are the standard textbook formulas. Anything
depending on the real package's richer behaviour should check IS_FALLBACK.

lib/__init__.py puts this on sys.path only after `import tradingcore` fails, so the real
package always wins where it exists.
"""
IS_FALLBACK = True
