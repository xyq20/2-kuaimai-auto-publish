"""Keep the CLI exit receipt even when the web service is restarted."""
from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys
import time

from .database import utc_now
from .launcher import atomic_json, read_json


def main(directory: Path) -> int:
    cancelled = False
    interrupted_at = None

    def cancel(_signum, _frame):
        nonlocal cancelled, interrupted_at
        cancelled = True
        interrupted_at = interrupted_at or time.monotonic()

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)
    code = 1
    try:
        command = read_json(directory / "launch.json")["argv"]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
        while True:
            try:
                code = child.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if cancelled:
                    # SIGINT normally arrives at the entire process group. Repeat it
                    # for a child created just after the initial stop request.
                    elapsed = time.monotonic() - interrupted_at
                    if elapsed < 1:
                        child.send_signal(signal.SIGINT)
                    elif elapsed > 25:
                        child.kill()
                    elif elapsed > 20:
                        child.terminate()
    except Exception as error:
        print(f"任务进程启动失败：{type(error).__name__}", flush=True)
    finally:
        atomic_json(directory / "result.json", {"exit_code": code, "cancelled": cancelled, "finished_at": utc_now()})
    return code


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
