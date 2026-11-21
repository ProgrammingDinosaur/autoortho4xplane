# Self-updater

AutoOrtho can download and install its own new versions. The code lives in
`autoortho/updater/` and has no Qt dependency; the UI parts are in
`autoortho/ui/main_window.py` (the "software updates" section), the header
banner in `autoortho/ui/shell.py`, and the workers in `autoortho/ui/workers.py`.

## How an update runs

1. **Check.** `releases.GitHubReleases` reads `releases/latest` (stable
   channel) or the release list (release-candidate channel). Only tags of the
   form `x.y.z` or `x.y.z-rc-N` count, so `tile-index` and `unstable` are
   ignored.
2. **Pick the file.** `install_info.detect()` works out how this copy was
   installed: Windows folder build (`zip`), Windows installer (`nsis`, found
   by the `uninstall.exe` next to the program), macOS `app`, or Linux `tar`
   (with the Ubuntu codename from the bundled `.build-info.json`).
   `releases.select_asset()` then finds the matching file through the
   release's `update-manifest.json`, or falls back to the file name and
   GitHub's own per-file SHA-256 digest.
3. **Download and unpack.** `download.download()` resumes interrupted
   downloads and checks the size and SHA-256. `stage.py` unpacks into
   `~/.autoortho-data/updates/<tag>/staged/` (refusing unsafe paths), finds
   the build inside, and checks its `.version` matches the release.
   macOS uses `ditto` so the app keeps its symlinks and executable bits.
4. **Hand over.** On **Restart Now** the app quits through its normal close
   path (stopping streaming and unmounting first). Just before the window
   closes it writes `updates/plan.json` and starts the helper.
5. **Helper.** The helper is AutoOrtho itself started with
   `AO_RUN_MODE=apply_update` (`__main__.py` dispatches to
   `updater/helper.py` before loading config, Qt or native libraries). For an
   update it is the *new* build, run from its staging folder, so it never
   holds files in the install folder. It:
   * waits for the old process and anything started from the install folder
     (mount workers, the crash collector) to exit;
   * renames the install folder to `<name>.previous-<old version>` and copies
     the staged build into its place;
   * starts the new version and waits up to two minutes for it to write
     `updates/started.json` (done in `_run_update_startup` once the window is
     up);
   * if the new version exits before that, puts the previous folder back,
     starts it, and leaves `updates/result.json` explaining what happened.

   For installer installs it instead runs the downloaded
   `AutoOrtho_win_<v>.exe /S /UPDATE /CurrentUser|/AllUsers /INSTDIR="..."`
   through `ShellExecuteEx` (Windows shows the administrator prompt), from a
   copy of the current build so the installer can replace every file. If the
   silent install fails it reruns the installer with its pages.
6. **Next start.** The app reads `result.json` (and shows any failure),
   confirms the start, and deletes staging folders for versions that are
   not newer than itself, plus backups that are no longer kept.

Only one previous version is kept. **Go Back to *version*** in Settings runs
the same swap with the kept folder as the source, using that version's own
helper.

Child processes started for the helper or the relaunched app get
`PYINSTALLER_RESET_ENVIRONMENT=1` and have AutoOrtho's worker variables and
PyInstaller's `_PYI_*` variables removed (`launch.clean_env`), so they start as
independent programs.

## Release side

* `autoortho.spec` bundles `autoortho/.build-info.json`
  (`{version, platform, arch, linux_variant}`). The Linux codename comes from
  the build container's `/etc/os-release`; set `AO_BUILD_VARIANT` to
  override it.
* The `update-manifest` job in `.github/workflows/build_release.yml` runs
  after all three builds, writes `update-manifest.json` with
  `scripts/make_update_manifest.py`, signs it when the `AO_UPDATE_SIGNING_KEY`
  secret exists, and attaches both to the release.
* File names must keep the patterns in `updater/assets.py`; the manifest
  script and the client both classify files there.
* `min_client_api` in the manifest (default 1) tells updaters with a lower
  `manifest.CLIENT_API` to send users to the release page. Raise it, and
  `CLIENT_API`, when a packaging change needs a newer updater.

## Manifest signatures

Signing is on. Every release must carry `update-manifest.json.sig`, signed
with the Ed25519 key whose public half is listed in `UPDATE_PUBLIC_KEYS`
(`autoortho/updater/signing.py`); the updater refuses unsigned or wrongly
signed releases and offers the release page instead. That protects users even
if a GitHub release or the account is tampered with, since the private key
never leaves the repository secret.

* Public key: `VBEec0JPmen38MahUopSOb195wXkxO3y5MdYj7L2jkY=` (created
  2026-11-21).
* Private key: the `AO_UPDATE_SIGNING_KEY` repository secret, which the
  `update-manifest` job uses. The maintainer keeps an offline copy; GitHub
  secrets cannot be read back.

Check the first release candidate built with this code: its release must list
`update-manifest.json` and `update-manifest.json.sig`, and

```
python scripts/update_signing.py verify update-manifest.json update-manifest.json.sig VBEec0JPmen38MahUopSOb195wXkxO3y5MdYj7L2jkY=
```

must print `valid`. If the job ever fails to sign a release, updaters skip
that release (users can still install it by hand); fix the secret and cut
the next one.

**Rotating the key:** create a new pair with
`python scripts/update_signing.py keygen`, add the new public key next to the
old one in `UPDATE_PUBLIC_KEYS`, release that, then switch the secret to the
new private key and drop the old public key a release or two later.

**If the private key is lost or leaked:** releases signed with a new key are
refused by installed versions, so users must install one release by hand.
Generate a new pair, replace the secret and `UPDATE_PUBLIC_KEYS`, and say so
in the release notes. (A leaked key also lets someone sign a malicious
manifest, but they would still need to publish it on this repository.)

## Testing against a fork

Set `AO_UPDATE_REPO=owner/name` to update from another repository's releases,
and `AO_UPDATE_PUBLIC_KEY` (only honoured together with `AO_UPDATE_REPO`) to
use a test signing key; the fork's release workflow then needs that key's
private half as its own `AO_UPDATE_SIGNING_KEY` secret. Publish two releases there built by the release
workflow, install the older one, and update from it.

Checklist per platform: zip, installer for the current user, installer for
all users, macOS app, both Linux tarballs; update while streaming; update with
a file in the install folder held open; decline the administrator prompt;
make the new version exit at startup (rollback); Go Back.

## Code signing

The builds are not code-signed. Signing would make Windows SmartScreen and
Defender less suspicious of an app that downloads and starts programs, and
on macOS a Developer ID signature plus notarization would stop macOS asking
for permissions again after updates (the ad-hoc signature changes with every
build). Both need certificates (for example SignPath's free open-source
programme for Windows, and an Apple Developer account for macOS); the commented
`!finalize` lines in `installer.nsi` show where Windows signing goes.
