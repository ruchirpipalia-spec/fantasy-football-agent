"""
Robust launcher for the fantasy-football MCP server.

Why this exists: `.mcp.json` used to point straight at server.py, which
assumes its dependencies (fastmcp, nfl_data_py, pandas, requests) are
already installed. That's true if someone installs this plugin by
cloning the repo and running `pip install -r requirements.txt` per the
README — but installing the packaged `.plugin` file directly (e.g.
opening/saving it in Cowork) skips that step entirely, since there's no
natural place in that flow for a manual pip install to happen. The
result is the MCP server process crashing immediately on import, which
just looks like "server not connected" with no obvious cause.

This launcher checks for the required packages first and installs them
itself if missing, so the plugin is self-contained regardless of which
install path was used, then runs the real server.
"""

import importlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REQUIREMENTS = ROOT / "requirements.txt"
REQUIRED_MODULES = ["fastmcp", "nfl_data_py", "pandas", "requests"]


def _missing_modules() -> list[str]:
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    return missing


def _install_requirements() -> None:
    print(
        f"[fantasy-football-agent] Missing dependencies detected — installing "
        f"from {REQUIREMENTS}...",
        file=sys.stderr,
    )
    base_cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)]
    result = subprocess.run(base_cmd, capture_output=True, text=True)
    if result.returncode != 0 and "externally-managed-environment" in (result.stderr or ""):
        # Common on newer Debian/Ubuntu/macOS Python installs (PEP 668).
        result = subprocess.run(base_cmd + ["--break-system-packages"], capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"[fantasy-football-agent] Automatic dependency install failed:\n"
            f"{result.stderr}\n"
            f"Try running manually: {sys.executable} -m pip install -r {REQUIREMENTS}",
            file=sys.stderr,
        )
        sys.exit(1)
    print("[fantasy-football-agent] Dependencies installed successfully.", file=sys.stderr)


def main() -> None:
    missing = _missing_modules()
    if missing:
        _install_requirements()
        still_missing = _missing_modules()
        if still_missing:
            print(
                f"[fantasy-football-agent] Still missing after install attempt: "
                f"{still_missing}. Try running manually: "
                f"{sys.executable} -m pip install -r {REQUIREMENTS}",
                file=sys.stderr,
            )
            sys.exit(1)

    # server.py locates its own directory via __file__, so this is safe
    # regardless of where launch.py itself was invoked from.
    sys.path.insert(0, str(HERE))
    import runpy

    runpy.run_path(str(HERE / "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
