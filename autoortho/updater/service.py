"""The app's side of an update: check, download, stage, hand over, clean up.

Everything here runs in AutoOrtho itself (from worker threads where it is
slow); :mod:`.helper` takes over once AutoOrtho quits.
"""

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import launch
from .assets import KIND_APP, KIND_NSIS, KIND_TAR, KIND_ZIP, safe_version
from .download import download
from .install_info import detect, executable_name
from .plan import (
    ACTION_INSTALLER,
    ACTION_SWAP,
    UpdatePlan,
    clear_pending_start,
    confirm_started,
    pending_start,
    plan_path,
    read_state,
    take_result,
    updates_dir,
    write_plan,
    write_state,
)
from .releases import CHANNEL_STABLE, GitHubReleases, check_for_update
from .stage import (
    extract,
    find_bundle_root,
    remove_tree,
    tree_size,
    validate_bundle,
)
from .versions import parse_release_version

log = logging.getLogger(__name__)

SWAP_KINDS = (KIND_ZIP, KIND_APP, KIND_TAR)
SPACE_MARGIN = 200 * 1024 * 1024
# Unpacked builds are roughly 2-3x their archive; keep room for both.
ARCHIVE_SPACE_FACTOR = 4
PENDING_START_STALE_SECONDS = 3600
STAGED_MARKER = "staged-complete.txt"


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class PreparedUpdate:
    """A verified update ready to hand to the helper."""

    version_text: str
    action: str
    helper_executable: Path
    source: Path | None = None  # swap: staged build (or the kept backup)
    installer: Path | None = None
    rollback: bool = False


@dataclass(frozen=True)
class StartupReport:
    result: dict | None  # what the helper reported (failures)
    started: dict | None  # set when this start completes an update


def _bundle_executable(root, platform):
    root = Path(root)
    if platform == "macos":
        return root / "Contents" / "MacOS" / "autoortho"
    return root / executable_name(platform)


def _free_bytes(folder):
    folder = Path(folder)
    while not folder.exists() and folder.parent != folder:
        folder = folder.parent
    return shutil.disk_usage(folder).free


def _megabytes(value):
    return f"{value / (1024 * 1024):,.0f} MB"


class UpdateManager:
    def __init__(self, install=None, root=None, client=None):
        self.install = install if install is not None else detect()
        self.root = Path(root) if root else updates_dir()
        self.client = client

    def _client(self):
        if self.client is None:
            self.client = GitHubReleases()
        return self.client

    # -- check ----------------------------------------------------------------

    def check(self, channel=CHANNEL_STABLE, current_version=None):
        """An UpdateOffer for a newer release, or None when up to date."""
        return check_for_update(
            self.install,
            channel=channel,
            current_version=current_version,
            client=self._client(),
        )

    # -- prepare --------------------------------------------------------------

    def prepare(self, offer, progress=None, cancelled=None, stage_progress=None):
        """Download and verify ``offer`` (and unpack it); a PreparedUpdate."""
        install = self.install
        if not offer.can_install:
            raise UpdateError(offer.reason or "This update cannot be installed automatically.")
        asset = offer.asset
        folder = self.root / safe_version(offer.release.tag)
        reused = self._reuse_staged(folder, offer)
        if reused is not None:
            return reused
        needed = asset.size * (1 if asset.kind == KIND_NSIS else ARCHIVE_SPACE_FACTOR)
        free = _free_bytes(self.root)
        if free < needed + SPACE_MARGIN:
            raise UpdateError(
                f"Not enough free space for the update in {self.root}: it "
                f"needs about {_megabytes(needed + SPACE_MARGIN)}, "
                f"{_megabytes(free)} is free."
            )
        archive = download(
            asset.url,
            folder / asset.name,
            asset.size,
            asset.sha256,
            progress=progress,
            cancelled=cancelled,
        )

        if asset.kind == KIND_NSIS:
            if stage_progress:
                stage_progress("Preparing the installer")
            helper_root = folder / "helper"
            remove_tree(helper_root)
            # The helper must not run from the folder the installer
            # replaces, so it runs from a copy of this version.
            shutil.copytree(
                install.root,
                helper_root,
                ignore=shutil.ignore_patterns("uninstall.exe"),
            )
            return PreparedUpdate(
                version_text=offer.release.tag,
                action=ACTION_INSTALLER,
                helper_executable=helper_root / executable_name(install.platform),
                installer=archive,
            )

        if stage_progress:
            stage_progress("Unpacking")
        staged = folder / "staged"
        (folder / STAGED_MARKER).unlink(missing_ok=True)
        extract(archive, staged, asset.kind, install.platform)
        build = find_bundle_root(staged, install.platform)
        validate_bundle(
            build,
            install.platform,
            offer.release.version,
            linux_variant=install.linux_variant,
        )
        size = tree_size(build)
        free = _free_bytes(install.root.parent)
        if free < size + SPACE_MARGIN:
            raise UpdateError(
                f"Not enough free space next to {install.root}: the new "
                f"version needs about {_megabytes(size + SPACE_MARGIN)}, "
                f"{_megabytes(free)} is free."
            )
        try:
            archive.unlink()  # the staged copy is all that is needed now
        except OSError:
            pass
        (folder / STAGED_MARKER).write_text(str(build), encoding="utf-8")
        return PreparedUpdate(
            version_text=offer.release.tag,
            action=ACTION_SWAP,
            helper_executable=_bundle_executable(build, install.platform),
            source=build,
        )

    def _reuse_staged(self, folder, offer):
        """A build staged earlier (for example before a restart), if intact."""
        if offer.asset.kind == KIND_NSIS:
            return None
        marker = folder / STAGED_MARKER
        try:
            build = Path(marker.read_text(encoding="utf-8").strip())
        except OSError:
            return None
        try:
            validate_bundle(
                build,
                self.install.platform,
                offer.release.version,
                linux_variant=self.install.linux_variant,
            )
        except Exception:
            return None
        if folder not in build.parents:
            return None
        return PreparedUpdate(
            version_text=offer.release.tag,
            action=ACTION_SWAP,
            helper_executable=_bundle_executable(build, self.install.platform),
            source=build,
        )

    # -- rollback -------------------------------------------------------------

    def rollback_target(self):
        """(path, version) of the kept previous version, or None."""
        install = self.install
        if install.kind not in SWAP_KINDS or not install.can_self_update:
            return None
        backup = read_state(self.root).get("backup") or {}
        path = backup.get("path")
        version = str(backup.get("version") or "")
        if not path or not version or parse_release_version(version) is None:
            return None
        if os.path.normcase(str(backup.get("target") or "")) != os.path.normcase(
            str(install.root)
        ):
            return None
        if not _bundle_executable(path, install.platform).is_file():
            return None
        return Path(path), version

    def prepare_rollback(self):
        target = self.rollback_target()
        if target is None:
            raise UpdateError("There is no previous version to go back to.")
        path, version = target
        return PreparedUpdate(
            version_text=version,
            action=ACTION_SWAP,
            helper_executable=_bundle_executable(path, self.install.platform),
            source=path,
            rollback=True,
        )

    # -- hand over ------------------------------------------------------------

    def _backup_path(self):
        root = self.install.root
        base = f"{root.name}.previous-{safe_version(self.install.version_text)}"
        candidate = root.with_name(base)
        number = 2
        while candidate.exists():
            candidate = root.with_name(f"{base}-{number}")
            number += 1
        return candidate

    def make_plan(self, prepared, pid=None, created=None):
        install = self.install
        if pid is None:
            pid = os.getpid()
            try:
                import psutil

                created = psutil.Process(pid).create_time()
            except Exception:
                created = 0.0
        relaunch = launch.relaunch_command(
            install.platform, install.root, install.app_executable
        )
        plan = UpdatePlan(
            action=prepared.action,
            wait_pid=int(pid),
            wait_pid_created=float(created or 0.0),
            target=str(install.root),
            platform=install.platform,
            from_version=install.version_text,
            to_version=prepared.version_text,
            relaunch=relaunch,
            rollback=prepared.rollback,
            updates_root=str(self.root),
        )
        if prepared.action == ACTION_SWAP:
            plan.source = str(prepared.source)
            plan.backup = str(self._backup_path())
        else:
            plan.installer = str(prepared.installer)
            plan.installer_args = [
                "/S",
                "/UPDATE",
                "/AllUsers" if install.all_users else "/CurrentUser",
                f'/INSTDIR="{install.root}"',
            ]
        return plan

    def hand_over(self, prepared):
        """Write the plan and start the helper; AutoOrtho must quit next."""
        if not Path(prepared.helper_executable).is_file():
            raise UpdateError("The prepared update is missing; download it again.")
        plan = self.make_plan(prepared)
        path = write_plan(plan, plan_path(self.root))
        launch.spawn_helper(prepared.helper_executable, path)
        return plan

    # -- startup --------------------------------------------------------------

    def startup(self, version_text):
        """Run once at startup: read the helper's report and tidy up."""
        result = take_result(self.root)
        started = confirm_started(version_text, self.root)
        pending = pending_start(self.root)
        if pending and not started:
            age = time.time() - float(pending.get("time") or 0)
            if age > PENDING_START_STALE_SECONDS:
                clear_pending_start(self.root)
                pending = None
        if not pending:
            self.cleanup()
        return StartupReport(result=result, started=started)

    def cleanup(self):
        """Delete staging folders and backups that are no longer kept."""
        state = read_state(self.root)
        remaining = []
        for path in state["cleanup"]:
            remove_tree(path)
            if Path(path).exists():
                remaining.append(path)
        if remaining != state["cleanup"]:
            state["cleanup"] = remaining
            write_state(state, self.root)
        # Keep downloads of versions newer than this one (one may be waiting
        # for a restart); everything else in the updates folder is done with.
        current = self.install.version
        if self.root.is_dir():
            for child in self.root.iterdir():
                if not child.is_dir():
                    continue
                version = parse_release_version(child.name)
                if version is not None and current is not None and version > current:
                    continue
                remove_tree(child)
