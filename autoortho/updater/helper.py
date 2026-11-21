"""The update helper: runs as its own process after AutoOrtho quits.

Started with ``AO_RUN_MODE=apply_update`` and ``AO_UPDATE_PLAN=<plan.json>``
(see :mod:`.launch`). For an update it is the *new* version, started from
its staging folder; for a rollback it is the kept *previous* version. Either
way it never runs from the folder it replaces, so Windows file locks on the
install folder are gone once AutoOrtho and its workers have exited.

Swap (folder builds, the macOS app, Linux):

1. wait for AutoOrtho (and any process started from its folder) to exit
2. rename the install folder to the backup name
3. copy the staged build into the install folder's place
4. start it, and wait for it to report that its window is up
5. if it exits first, put the backup back and start that instead

Installer (Windows NSIS installs): run the installer silently with /UPDATE
(Windows shows the administrator prompt), then start the app.
"""

import logging
import logging.handlers
import os
import shutil
import sys
import time
from pathlib import Path

from . import launch
from .plan import (
    ACTION_INSTALLER,
    ACTION_SWAP,
    STATUS_BROKEN,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_ROLLED_BACK,
    clear_pending_start,
    has_started,
    read_plan,
    read_state,
    updates_dir,
    write_pending_start,
    write_result,
    write_state,
)
from .stage import remove_tree, tree_size

log = logging.getLogger("autoortho.updater.helper")

EXIT_WAIT_SECONDS = 600
FOLDER_PROCESS_WAIT_SECONDS = 60
RENAME_ATTEMPTS = 40
RENAME_DELAY = 0.5
FREE_SPACE_MARGIN = 200 * 1024 * 1024


class HelperError(Exception):
    pass


def _setup_logging():
    log_dir = Path(os.path.expanduser("~")) / ".autoortho-data" / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_dir / "updater.log",
            maxBytes=1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _is_build(folder, platform):
    # Imported here to keep the module importable without the stage helpers
    # being touched in tests that stub them.
    from .stage import _is_bundle_root

    return Path(folder).exists() and _is_bundle_root(Path(folder), platform)


class Helper:
    """Carries out one plan. The hooks exist so tests can fake processes."""

    def __init__(
        self,
        plan,
        updates_root=None,
        start_app=launch.start_app,
        run_elevated=launch.run_elevated_and_wait,
        sleep=time.sleep,
        clock=time.monotonic,
        wait_for_exit=None,
        wait_for_folder=None,
    ):
        self.plan = plan
        self.root = Path(updates_root or plan.updates_root or updates_dir())
        self.start_app = start_app
        self.run_elevated = run_elevated
        self.sleep = sleep
        self.clock = clock
        self.wait_for_exit = wait_for_exit or self._wait_for_exit
        self.wait_for_folder = wait_for_folder or self._wait_for_folder_processes

    # -- waiting ------------------------------------------------------------

    def _wait_for_exit(self, pid, created, timeout):
        """True once process ``pid`` (started at ``created``) has exited."""
        import psutil

        try:
            process = psutil.Process(pid)
            if created and abs(process.create_time() - created) > 1.0:
                return True  # the PID now belongs to another process
            process.wait(timeout)
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.TimeoutExpired:
            return False

    def _wait_for_folder_processes(self, folder, timeout):
        """Wait for processes started from ``folder`` (mount workers, the
        crash collector) to exit. True when none are left."""
        import psutil

        folder = os.path.normcase(os.path.abspath(str(folder)))
        own = os.getpid()
        deadline = self.clock() + timeout
        while True:
            left = []
            for process in psutil.process_iter(["pid", "exe"]):
                exe = process.info.get("exe")
                if not exe or process.info.get("pid") == own:
                    continue
                path = os.path.normcase(os.path.abspath(exe))
                if path == folder or path.startswith(folder + os.sep):
                    left.append(process.info["pid"])
            if not left:
                return True
            if self.clock() >= deadline:
                log.warning("Processes still running from %s: %s", folder, left)
                return False
            self.sleep(1.0)

    def _watch_start(self, process, version):
        """'started', 'exited' or 'timeout' for the app just started."""
        deadline = self.clock() + float(self.plan.health_timeout)
        while self.clock() < deadline:
            if has_started(version, self.root):
                return "started"
            if process is not None and process.poll() is not None:
                # It may have reported and exited between the two checks.
                if has_started(version, self.root):
                    return "started"
                return "exited"
            self.sleep(0.5)
        return "timeout"

    # -- file operations ----------------------------------------------------

    def _rename(self, source, destination):
        last = None
        for _attempt in range(RENAME_ATTEMPTS):
            try:
                os.rename(source, destination)
                return
            except OSError as exc:  # locked by antivirus, Explorer, ...
                last = exc
                self.sleep(RENAME_DELAY)
        raise HelperError(
            f"Could not rename {source} (a file in it is in use): {last}"
        )

    def _copy_build(self, source, target):
        shutil.copytree(source, target, symlinks=True)
        if not _is_build(target, self.plan.platform):
            raise HelperError("The copied build is incomplete.")

    def _restore(self, target, backup):
        """Put the backup back in place of a failed new build."""
        if Path(target).exists():
            failed = Path(f"{target}.failed")
            remove_tree(failed)
            try:
                self._rename(target, failed)
                remove_tree(failed)
            except HelperError:
                remove_tree(target)
        self._rename(backup, target)

    def _relaunch(self):
        try:
            return self.start_app(self.plan.relaunch)
        except OSError as exc:
            log.error("Could not start AutoOrtho: %s", exc)
            return None

    # -- running ------------------------------------------------------------

    def run(self):
        plan = self.plan
        log.info(
            "Update helper: %s %s -> %s at %s",
            plan.action,
            plan.from_version,
            plan.to_version,
            plan.target,
        )
        if not self.wait_for_exit(plan.wait_pid, plan.wait_pid_created, EXIT_WAIT_SECONDS):
            write_result(
                STATUS_FAILED,
                "AutoOrtho did not quit, so the update was not installed.",
                plan,
                self.root,
            )
            return 1
        self.wait_for_folder(plan.target, FOLDER_PROCESS_WAIT_SECONDS)
        if plan.action == ACTION_SWAP:
            return self.run_swap()
        if plan.action == ACTION_INSTALLER:
            return self.run_installer()
        raise HelperError(f"Unknown action {plan.action!r}")

    def run_swap(self):
        plan = self.plan
        target = Path(plan.target)
        source = Path(plan.source)
        backup = Path(plan.backup)
        what = "go back to" if plan.rollback else "install"

        problem = None
        if not _is_build(source, plan.platform):
            problem = "the new build is missing or incomplete"
        elif not _is_build(target, plan.platform):
            problem = f"{target} is not an AutoOrtho install"
        elif backup.exists():
            problem = f"{backup} already exists"
        else:
            free = shutil.disk_usage(target.parent).free
            if free < tree_size(source) + FREE_SPACE_MARGIN:
                problem = f"there is not enough free space on the drive of {target}"
        if problem:
            write_result(
                STATUS_FAILED,
                f"Could not {what} AutoOrtho {plan.to_version}: {problem}.",
                plan,
                self.root,
            )
            self._relaunch()
            return 1

        try:
            self._rename(target, backup)
        except HelperError as exc:
            write_result(
                STATUS_FAILED,
                f"Could not {what} AutoOrtho {plan.to_version}: {exc}",
                plan,
                self.root,
            )
            self._relaunch()
            return 1

        try:
            self._copy_build(source, target)
        except Exception as exc:
            log.exception("Copying the new build failed")
            try:
                self._restore(target, backup)
            except HelperError as restore_exc:
                write_result(
                    STATUS_BROKEN,
                    f"The update failed and the previous version could not be "
                    f"put back automatically. It is kept at {backup}; rename it "
                    f"to {target.name} to use it again. ({restore_exc})",
                    plan,
                    self.root,
                    backup=str(backup),
                )
                return 1
            write_result(
                STATUS_FAILED,
                f"Could not {what} AutoOrtho {plan.to_version}: {exc}",
                plan,
                self.root,
            )
            self._relaunch()
            return 1

        write_pending_start(
            plan.to_version,
            self.root,
            rollback=plan.rollback,
            from_version=plan.from_version,
        )
        process = self._relaunch()
        outcome = self._watch_start(process, plan.to_version) if process else "exited"
        log.info("New version start check: %s", outcome)

        if outcome == "exited":
            self.wait_for_folder(target, FOLDER_PROCESS_WAIT_SECONDS)
            clear_pending_start(self.root)
            try:
                self._restore(target, backup)
            except HelperError as exc:
                write_result(
                    STATUS_BROKEN,
                    f"AutoOrtho {plan.to_version} closed right after starting, "
                    f"and the previous version could not be put back "
                    f"automatically. It is kept at {backup}; rename it to "
                    f"{target.name} to use it again. ({exc})",
                    plan,
                    self.root,
                    backup=str(backup),
                )
                return 1
            write_result(
                STATUS_ROLLED_BACK,
                f"AutoOrtho {plan.to_version} closed right after starting, so "
                f"AutoOrtho {plan.from_version} was put back.",
                plan,
                self.root,
            )
            self._relaunch()
            return 1

        clear_pending_start(self.root)
        self._record_backup()
        return 0

    def _record_backup(self):
        """Keep this backup for rolling back; schedule the rest for removal."""
        plan = self.plan
        state = read_state(self.root)
        cleanup = list(state["cleanup"])
        old = state.get("backup") or {}
        old_path = old.get("path")
        if plan.rollback:
            # Going back: the newer version moved aside is not kept, and the
            # backup that was just restored from is no longer needed.
            discard = [plan.backup, plan.source]
            state["backup"] = None
        else:
            discard = [old_path] if old_path and old_path != plan.backup else []
            state["backup"] = {
                "path": plan.backup,
                "version": plan.from_version,
                "target": plan.target,
            }
        own = os.path.normcase(os.path.abspath(sys.executable))
        for path in discard:
            if not path:
                continue
            folder = os.path.normcase(os.path.abspath(path))
            # Never delete the folder this helper runs from (a rollback runs
            # from the kept backup); the next AutoOrtho start removes it.
            if not own.startswith(folder + os.sep):
                remove_tree(path)
            if Path(path).exists() and path not in cleanup:
                cleanup.append(path)
        state["cleanup"] = cleanup
        write_state(state, self.root)

    def run_installer(self):
        plan = self.plan
        installer = Path(plan.installer)
        arguments = " ".join(plan.installer_args)
        try:
            code = self.run_elevated(installer, arguments)
        except OSError as exc:
            write_result(
                STATUS_FAILED,
                f"Could not start the AutoOrtho {plan.to_version} installer: "
                f"{exc}",
                plan,
                self.root,
                installer=str(installer),
            )
            self._relaunch()
            return 1
        if code is None:
            write_result(
                STATUS_CANCELLED,
                f"The administrator prompt was declined, so AutoOrtho "
                f"{plan.to_version} was not installed.",
                plan,
                self.root,
            )
            self._relaunch()
            return 1
        if code != 0:
            log.error("Silent installer exited with %s; running it interactively", code)
            interactive = " ".join(
                argument for argument in plan.installer_args if argument != "/S"
            )
            try:
                code = self.run_elevated(installer, interactive)
            except OSError:
                code = -1
            if code != 0:
                write_result(
                    STATUS_BROKEN,
                    f"The AutoOrtho {plan.to_version} installer did not finish. "
                    f"Run {installer} to install it.",
                    plan,
                    self.root,
                    installer=str(installer),
                )
                if _is_build(plan.target, plan.platform):
                    self._relaunch()
                return 1

        write_pending_start(plan.to_version, self.root, from_version=plan.from_version)
        process = self._relaunch()
        outcome = self._watch_start(process, plan.to_version) if process else "exited"
        clear_pending_start(self.root)
        if outcome == "exited":
            write_result(
                STATUS_BROKEN,
                f"AutoOrtho {plan.to_version} was installed but closed right "
                f"after starting. Run {installer} again, or install the "
                f"previous version from the release page.",
                plan,
                self.root,
                installer=str(installer),
            )
            return 1
        return 0


def main():
    _setup_logging()
    plan_file = os.environ.get(launch.PLAN_ENV, "")
    try:
        plan = read_plan(plan_file)
    except (OSError, ValueError, TypeError) as exc:
        log.error("Cannot read update plan %r: %s", plan_file, exc)
        return 2
    helper = Helper(plan)
    try:
        return helper.run()
    except Exception as exc:
        log.exception("Update helper failed")
        try:
            write_result(
                STATUS_FAILED,
                f"The update failed: {exc}",
                plan,
                helper.root,
            )
        except OSError:
            pass
        return 1
    finally:
        try:
            Path(plan_file).unlink()
        except OSError:
            pass
