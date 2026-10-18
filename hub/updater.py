"""Self-update from GitHub.

If the hub runs from a git clone, it checks the remote every few minutes. When there is
new code it fast-forwards, reinstalls dependencies if pyproject.toml changed, and asks the
process to exit; the start script (start-hub.bat / systemd / Docker restart policy) starts
it again on the new code. Turn off with HOVERCAM_AUTO_UPDATE=0.

Security: whoever can push to the hub's GitHub repo can run code on the hub. Keep that
repo's write access to yourself.
"""

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger("hovercam.updater")

REPO = Path(__file__).resolve().parent.parent
CHECK_EVERY_S = int(os.environ.get("HOVERCAM_UPDATE_MINUTES", "10")) * 60
BRANCH = os.environ.get("HOVERCAM_UPDATE_BRANCH", "main")
WINDOWS_GIT = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd" / "git.exe"


class RestartRequested(Exception):
    """Raised out of the agent's main loop so the process exits and is restarted."""


def git_executable() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    return str(WINDOWS_GIT) if WINDOWS_GIT.exists() else None


def enabled() -> bool:
    return (os.environ.get("HOVERCAM_AUTO_UPDATE", "1") != "0"
            and (REPO / ".git").exists() and git_executable() is not None)


async def _git(*args: str, timeout: float = 90) -> tuple[int, str]:
    # safe.directory: the hub may run as another account (Windows SYSTEM task) than the
    # one that owns the folder; git refuses such repos unless told it's fine.
    proc = await asyncio.create_subprocess_exec(
        git_executable() or "git", "-c", "safe.directory=*", "-C", str(REPO), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "git timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def current_commit() -> str | None:
    if not (REPO / ".git").exists() or git_executable() is None:
        return None
    code, out = await _git("rev-parse", "--short", "HEAD", timeout=10)
    return out if code == 0 else None


async def check_once() -> bool:
    """Fetch; if the remote moved, update the working copy. True means restart now."""
    code, out = await _git("fetch", "--quiet", "origin", BRANCH)
    if code != 0:
        log.warning("update check failed: %s", out[-300:])
        return False
    _, head = await _git("rev-parse", "HEAD", timeout=10)
    _, remote = await _git("rev-parse", f"origin/{BRANCH}", timeout=10)
    if not remote or head == remote:
        return False
    _, changed = await _git("diff", "--name-only", head, remote, timeout=10)
    # This copy only runs code (nobody edits it), so take the remote exactly; ignored
    # files (state, .venv, go2rtc.exe, logs) are untouched.
    code, out = await _git("reset", "--hard", remote, timeout=60)
    if code != 0:
        log.warning("update failed: %s", out[-300:])
        return False
    log.info("updated %s -> %s (%s)", head[:7], remote[:7],
             ", ".join(changed.split()[:8]) or "no file list")
    if "pyproject.toml" in changed.split():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--quiet", "-e", str(REPO),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    return True


async def watch(busy) -> None:
    """Background task. `busy()` returning True postpones the restart (e.g. someone is
    watching a recording); it is retried every minute, at most for an hour."""
    if not enabled():
        log.info("auto-update off (not a git clone, git missing, or disabled)")
        return
    log.info("auto-update on: checking GitHub every %d min", CHECK_EVERY_S // 60)
    await asyncio.sleep(60)
    while True:
        try:
            if await check_once():
                for _ in range(60):
                    if not busy():
                        break
                    await asyncio.sleep(60)
                log.info("restarting on the new version")
                raise RestartRequested()
        except RestartRequested:
            raise
        except Exception as exc:  # never let the updater take the hub down
            log.warning("update check error: %s", exc)
        await asyncio.sleep(CHECK_EVERY_S)
