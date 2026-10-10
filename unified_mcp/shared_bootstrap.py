"""Private WMI bootstrap: restore environment before importing package state.

Invoked by absolute filename because the WMI provider does not inherit the
client's environment or Job Object. Never print bootstrap data or arguments.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
import traceback


def main():
    if len(sys.argv) != 2:
        raise RuntimeError("Shared daemon bootstrap requires one private configuration path")
    path = Path(sys.argv[1])
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise RuntimeError("Refusing redirected shared bootstrap")
    if path.stat().st_size > 512 * 1024:
        raise RuntimeError("Shared bootstrap is too large")
    with path.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    env = config["env"]
    if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
        raise RuntimeError("Invalid shared bootstrap environment")
    log_path = Path(config["log"])
    if log_path.parent.resolve() != path.parent.resolve():
        raise RuntimeError("Shared bootstrap log must remain in its private directory")
    # The sensitive handoff is single-use. It is no longer needed once loaded.
    path.unlink()
    os.environ.clear()
    os.environ.update(env)
    os.chdir(config["cwd"])
    sys.path.insert(0, config["package_root"])
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stderr = sys.stdout
    sys.argv = ["wx-qq-mcp-daemon", *config["daemon_args"]]
    try:
        runpy.run_module("unified_mcp.shared_service", run_name="__main__")
    except SystemExit as exc:
        if exc.code not in (None, 0):
            traceback.print_exc()
        raise
    except BaseException:
        # Exception messages contain no environment dump or bearer credentials.
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
