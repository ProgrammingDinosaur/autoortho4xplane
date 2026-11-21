"""Self-updater logic (no Qt): releases, downloads, staging and the helper."""

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO_ROOT = Path(__file__).resolve().parents[2]

from updater import ed25519, launch, plan as plan_module  # noqa: E402
from updater import signing as signing_module  # noqa: E402

SHIPPED_KEYS = signing_module.UPDATE_PUBLIC_KEYS  # before any test patches it
from updater.assets import classify_asset, safe_version  # noqa: E402
from updater.download import (  # noqa: E402
    DownloadCancelled,
    DownloadError,
    download,
)
from updater.helper import Helper  # noqa: E402
from updater.install_info import InstallInfo, detect  # noqa: E402
from updater.manifest import (  # noqa: E402
    CLIENT_API,
    MANIFEST_NAME,
    SIGNATURE_NAME,
    ManifestError,
    build_manifest,
    dump_manifest,
    parse_manifest,
)
from updater.plan import (  # noqa: E402
    ACTION_INSTALLER,
    ACTION_SWAP,
    UpdatePlan,
    confirm_started,
    read_plan,
    read_state,
    take_result,
    write_pending_start,
    write_plan,
    write_state,
)
from updater.releases import (  # noqa: E402
    GitHubReleases,
    Release,
    ReleaseAsset,
    check_for_update,
    normalize_channel,
    repository,
    select_asset,
)
from updater.service import UpdateManager  # noqa: E402
from updater.signing import (  # noqa: E402
    decode_key,
    sign_manifest,
    trusted_keys,
    verify_manifest,
)
from updater.stage import (  # noqa: E402
    StageError,
    extract,
    find_bundle_root,
    validate_bundle,
)
from updater.versions import parse_release_version, parse_version  # noqa: E402

RFC_SECRET = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
RFC_PUBLIC = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
)
RFC_SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555f"
    "b8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


@pytest.fixture(autouse=True)
def _no_signing_env(monkeypatch):
    # Most tests cover the checksum-only paths; the shipped key is tested
    # on its own below, and _require_signing turns signing back on.
    monkeypatch.delenv("AO_UPDATE_REPO", raising=False)
    monkeypatch.delenv("AO_UPDATE_PUBLIC_KEY", raising=False)
    monkeypatch.setattr(signing_module, "UPDATE_PUBLIC_KEYS", ())


def _require_signing(monkeypatch, secret=RFC_SECRET):
    monkeypatch.setenv("AO_UPDATE_REPO", "someone/fork")
    monkeypatch.setenv(
        "AO_UPDATE_PUBLIC_KEY",
        base64.b64encode(ed25519.public_key(secret)).decode(),
    )


# -- versions ------------------------------------------------------------------


def test_release_versions_are_strict():
    assert str(parse_release_version("3.4.0")) == "3.4.0"
    assert str(parse_release_version("v3.4.0")) == "3.4.0"
    assert str(parse_release_version("3.5.0-rc-2")) == "3.5.0rc2"
    for text in ("unstable", "tile-index", "2026.11.21.05.03.01", "3.4", "", None):
        assert parse_release_version(text) is None
    assert parse_release_version("3.5.0-rc-1") < parse_release_version("3.5.0")


def test_lenient_version_finds_embedded_text():
    assert str(parse_version("AutoOrtho v3.4.0")) == "3.4.0"
    assert str(parse_version("3.4.0-rc-1")) == "3.4.0rc1"
    assert parse_version("ref: refs/heads/main") is None


# -- assets --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,platform,kind,variant",
    [
        ("autoortho_win_3.4.0.zip", "windows", "zip", ""),
        ("AutoOrtho_win_3.4.0.exe", "windows", "nsis", ""),
        ("AutoOrtho_mac_3.4.0.zip", "macos", "app", ""),
        ("autoortho_linux_3.4.0_jammy.tar.gz", "linux", "tar", "jammy"),
        ("autoortho_linux_3.5.0-rc-1_resolute.tar.gz", "linux", "tar", "resolute"),
    ],
)
def test_release_files_are_classified(name, platform, kind, variant):
    spec = classify_asset(name)
    assert (spec.platform, spec.kind, spec.variant) == (platform, kind, variant)


def test_unknown_files_are_not_classified():
    assert classify_asset("update-manifest.json") is None
    assert classify_asset("overlay_index_xp11.json.gz") is None
    assert safe_version("3.5.0 rc/1") == "3.5.0-rc-1"


# -- manifest and signing --------------------------------------------------------


def _release_files(folder, tag="3.5.0"):
    folder.mkdir(parents=True, exist_ok=True)
    names = [
        f"autoortho_win_{tag}.zip",
        f"AutoOrtho_win_{tag}.exe",
        f"AutoOrtho_mac_{tag}.zip",
        f"autoortho_linux_{tag}_jammy.tar.gz",
        "notes.txt",
    ]
    paths = []
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(f"{name}-{index}".encode() * 10)
        paths.append(str(path))
    return paths


def test_manifest_round_trip(tmp_path):
    document = build_manifest("3.5.0", _release_files(tmp_path))
    manifest = parse_manifest(dump_manifest(document))
    assert manifest.tag == "3.5.0"
    assert manifest.min_client_api == 1
    assert len(manifest.entries) == 4  # notes.txt is skipped
    entry = manifest.entry("autoortho_win_3.5.0.zip")
    expected = hashlib.sha256(Path(tmp_path / entry.name).read_bytes()).hexdigest()
    assert entry.sha256 == expected


@pytest.mark.parametrize(
    "document",
    [
        {"schema": 2, "tag": "1.0.0", "assets": []},
        {"schema": 1, "tag": "", "assets": []},
        {"schema": 1, "tag": "1.0.0", "assets": [{"name": "a", "size": 0}]},
        {
            "schema": 1,
            "tag": "1.0.0",
            "assets": [
                {"name": "a", "platform": "linux", "kind": "tar", "size": 1, "sha256": "xyz"}
            ],
        },
    ],
)
def test_malformed_manifests_are_rejected(document):
    with pytest.raises(ManifestError):
        parse_manifest(json.dumps(document).encode())


def test_ed25519_matches_rfc8032():
    assert ed25519.public_key(RFC_SECRET) == RFC_PUBLIC
    assert ed25519.sign(RFC_SECRET, b"") == RFC_SIGNATURE
    assert ed25519.verify(RFC_PUBLIC, b"", RFC_SIGNATURE)
    assert not ed25519.verify(RFC_PUBLIC, b"x", RFC_SIGNATURE)
    bad = bytearray(RFC_SIGNATURE)
    bad[5] ^= 1
    assert not ed25519.verify(RFC_PUBLIC, b"", bytes(bad))


def test_manifest_signature_checks():
    data = b'{"schema": 1}'
    signature = sign_manifest(data, RFC_SECRET)
    assert verify_manifest(data, signature, keys=[RFC_PUBLIC])
    assert not verify_manifest(data + b" ", signature, keys=[RFC_PUBLIC])
    assert not verify_manifest(data, "not base64!", keys=[RFC_PUBLIC])
    assert not verify_manifest(data, signature, keys=[])


def test_shipped_public_key_is_valid_and_enforced(monkeypatch):
    assert SHIPPED_KEYS, "a release signing key must be listed"
    for text in SHIPPED_KEYS:
        assert len(decode_key(text)) == 32
    monkeypatch.setattr(signing_module, "UPDATE_PUBLIC_KEYS", SHIPPED_KEYS)
    assert signing_module.signing_required(environ={})


def test_keys_decode_and_test_override_needs_a_repo(monkeypatch):
    assert decode_key(RFC_PUBLIC.hex()) == RFC_PUBLIC
    assert decode_key(base64.b64encode(RFC_PUBLIC).decode()) == RFC_PUBLIC
    with pytest.raises(ValueError):
        decode_key("abc")
    monkeypatch.setenv("AO_UPDATE_PUBLIC_KEY", RFC_PUBLIC.hex())
    assert trusted_keys() == []  # ignored without AO_UPDATE_REPO
    monkeypatch.setenv("AO_UPDATE_REPO", "me/fork")
    assert trusted_keys() == [RFC_PUBLIC]
    assert repository() == "me/fork"


# -- install detection -------------------------------------------------------------


def _windows_build(folder, nsis=False, user=True):
    folder.mkdir(parents=True)
    (folder / "autoortho.exe").write_bytes(b"MZ")
    (folder / "ao_files").mkdir()
    if nsis:
        (folder / "uninstall.exe").write_bytes(b"MZ")
        if user:
            (folder / "_user_install_marker").write_bytes(b"")
    return folder / "autoortho.exe"


def _detect(exe, platform="win32", version="3.4.0", info=None, writable=True):
    return detect(
        executable=exe,
        frozen=True,
        sys_platform=platform,
        version_text=version,
        build_info=info or {},
        writable=lambda folder: writable,
    )


def test_source_runs_cannot_self_update():
    install = detect(frozen=False, version_text="3.4.0")
    assert install.kind == "source"
    assert not install.can_self_update


def test_windows_install_kinds(tmp_path):
    install = _detect(_windows_build(tmp_path / "zip"))
    assert (install.kind, install.can_self_update) == ("zip", True)
    assert install.root == (tmp_path / "zip").resolve()

    user = _detect(_windows_build(tmp_path / "user", nsis=True, user=True))
    assert (user.kind, user.all_users) == ("nsis", False)
    machine = _detect(_windows_build(tmp_path / "all", nsis=True, user=False))
    assert (machine.kind, machine.all_users) == ("nsis", True)


def test_install_reasons(tmp_path):
    exe = _windows_build(tmp_path / "zip")
    assert "not a release" in _detect(exe, version="unstable").unsupported_reason
    assert "cannot write" in _detect(exe, writable=False).unsupported_reason
    # The installer elevates by itself, so a read-only folder is fine.
    nsis = _windows_build(tmp_path / "nsis", nsis=True)
    assert _detect(nsis, writable=False).can_self_update

    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "autoortho.exe").write_bytes(b"MZ")
    assert "ao_files" in _detect(lonely / "autoortho.exe").unsupported_reason


def test_macos_and_linux_detection(tmp_path):
    app = tmp_path / "AutoOrtho.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "autoortho").write_bytes(b"")
    install = _detect(app / "autoortho", platform="darwin")
    assert install.kind == "app" and install.can_self_update
    assert install.app_executable == install.root / "Contents" / "MacOS" / "autoortho"

    moved = tmp_path / "AppTranslocation" / "X" / "AutoOrtho.app" / "Contents" / "MacOS"
    moved.mkdir(parents=True)
    (moved / "autoortho").write_bytes(b"")
    reason = _detect(moved / "autoortho", platform="darwin").unsupported_reason
    assert "temporary read-only copy" in reason

    linux = tmp_path / "autoortho_linux_3.4.0"
    linux.mkdir()
    (linux / "autoortho").write_bytes(b"")
    (linux / "ao_files").mkdir()
    assert "Linux release" in _detect(linux / "autoortho", platform="linux").unsupported_reason
    good = _detect(linux / "autoortho", platform="linux", info={"linux_variant": "jammy"})
    assert good.can_self_update and good.linux_variant == "jammy"


# -- release discovery and asset selection ---------------------------------------------


class FakeClient:
    def __init__(self, files=None, release=None):
        self.files = files or {}
        self.release = release
        self.fetched = []

    def fetch_small(self, url, limit=0):
        self.fetched.append(url)
        return self.files[url]

    def latest(self, channel):
        return self.release


def _install(kind="zip", platform="windows", variant="", version="3.4.0", reason=""):
    return InstallInfo(
        platform=platform,
        kind=kind,
        version_text=version,
        version=parse_release_version(version),
        root=Path("/install"),
        executable=Path("/install/autoortho.exe"),
        linux_variant=variant,
        unsupported_reason=reason,
    )


def _asset(name, data=b"data", digest=True):
    return ReleaseAsset(
        name=name,
        size=len(data),
        url=f"https://example.test/{name}",
        sha256=hashlib.sha256(data).hexdigest() if digest else "",
    )


def _release(tag="3.5.0", assets=()):
    return Release(
        tag=tag,
        version=parse_release_version(tag),
        title=tag,
        notes="Notes",
        html_url="https://example.test/release",
        prerelease=False,
        assets=tuple(assets),
    )


def test_selection_by_file_name_uses_github_digest():
    release = _release(
        assets=[
            _asset("autoortho_win_3.5.0.zip"),
            _asset("AutoOrtho_win_3.5.0.exe"),
            _asset("autoortho_linux_3.5.0_jammy.tar.gz", b"jammy"),
            _asset("autoortho_linux_3.5.0_resolute.tar.gz", b"resolute"),
        ]
    )
    asset, reason = select_asset(release, _install(), FakeClient())
    assert (asset.name, reason) == ("autoortho_win_3.5.0.zip", "")
    asset, _ = select_asset(release, _install("nsis"), FakeClient())
    assert asset.name == "AutoOrtho_win_3.5.0.exe"
    asset, _ = select_asset(
        release, _install("tar", "linux", "resolute"), FakeClient()
    )
    assert asset.sha256 == hashlib.sha256(b"resolute").hexdigest()
    asset, reason = select_asset(release, _install("tar", "linux", "focal"), FakeClient())
    assert asset is None and "focal" in reason


def test_selection_refuses_files_without_checksum():
    release = _release(assets=[_asset("autoortho_win_3.5.0.zip", digest=False)])
    asset, reason = select_asset(release, _install(), FakeClient())
    assert asset is None and "checksum" in reason


def _manifest_release(tmp_path, tag="3.5.0", signature=None, **overrides):
    paths = _release_files(tmp_path / "files", tag)
    document = build_manifest(tag, paths)
    document.update(overrides)
    data = dump_manifest(document)
    assets = [_asset(Path(p).name, Path(p).read_bytes(), digest=False) for p in paths]
    files = {f"https://example.test/{MANIFEST_NAME}": data}
    assets.append(ReleaseAsset(MANIFEST_NAME, len(data), f"https://example.test/{MANIFEST_NAME}"))
    if signature is not None:
        sig = signature(data).encode()
        files[f"https://example.test/{SIGNATURE_NAME}"] = sig
        assets.append(ReleaseAsset(SIGNATURE_NAME, len(sig), f"https://example.test/{SIGNATURE_NAME}"))
    return _release(tag, assets), FakeClient(files)


def test_selection_prefers_the_manifest(tmp_path):
    release, client = _manifest_release(tmp_path)
    asset, reason = select_asset(release, _install(), client)
    assert reason == ""
    expected = (tmp_path / "files" / "autoortho_win_3.5.0.zip").read_bytes()
    assert asset.sha256 == hashlib.sha256(expected).hexdigest()


def test_manifest_problems_block_the_update(tmp_path):
    release, client = _manifest_release(tmp_path / "a", min_client_api=CLIENT_API + 1)
    assert "manual install" in select_asset(release, _install(), client)[1]

    release, client = _manifest_release(tmp_path / "b", tag="3.5.0")
    other = _release("3.6.0", release.assets)
    assert "another release" in select_asset(other, _install(), client)[1]

    release, client = _manifest_release(tmp_path / "c")
    wrong = [
        ReleaseAsset(a.name, a.size + 1, a.url) if a.name.endswith(".zip") else a
        for a in release.assets
    ]
    assert "does not match" in select_asset(_release("3.5.0", wrong), _install(), client)[1]


def test_signed_manifests_are_required_once_keys_exist(tmp_path, monkeypatch):
    _require_signing(monkeypatch)
    release, client = _manifest_release(tmp_path / "unsigned")
    assert "not signed" in select_asset(release, _install(), client)[1]

    release, client = _manifest_release(
        tmp_path / "signed",
        signature=lambda data: sign_manifest(data, RFC_SECRET),
    )
    asset, reason = select_asset(release, _install(), client)
    assert asset is not None and reason == ""

    other_key = bytes(range(32))
    release, client = _manifest_release(
        tmp_path / "forged",
        signature=lambda data: sign_manifest(data, other_key),
    )
    assert "not valid" in select_asset(release, _install(), client)[1]

    plain = _release(assets=[_asset("autoortho_win_3.5.0.zip")])
    assert "no signed manifest" in select_asset(plain, _install(), FakeClient())[1]


def test_check_for_update_compares_versions():
    newer = _release("3.5.0", [_asset("autoortho_win_3.5.0.zip")])
    assert check_for_update(_install(version="3.5.0"), client=FakeClient(release=newer)) is None
    assert check_for_update(_install(version="3.6.0"), client=FakeClient(release=newer)) is None
    offer = check_for_update(_install(), client=FakeClient(release=newer))
    assert offer.can_install and offer.version_text == "3.5.0"
    # Not self-updatable: offered for the release page, no file lookup.
    blocked = check_for_update(
        _install(reason="source"),
        client=FakeClient(release=newer),
        current_version=parse_version("3.4.0"),
    )
    assert blocked.asset is None and blocked.reason == "source"
    assert normalize_channel("Release candidates") == "rc"
    assert normalize_channel("anything") == "stable"


class FakeResponse:
    def __init__(self, payload=None, status=200, chunks=(), headers=None):
        self.payload = payload
        self.status_code = status
        self.chunks = list(chunks)
        self.headers = headers or {}

    def json(self):
        return self.payload

    def iter_content(self, size):
        yield from self.chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _api_release(tag, prerelease=False, draft=False):
    return {
        "tag_name": tag,
        "name": tag,
        "body": "",
        "html_url": f"https://example.test/{tag}",
        "prerelease": prerelease,
        "draft": draft,
        "assets": [
            {
                "name": f"autoortho_win_{tag}.zip",
                "size": 4,
                "browser_download_url": f"https://example.test/{tag}.zip",
                "digest": "sha256:" + "a" * 64,
            }
        ],
    }


def test_github_channels():
    stable = GitHubReleases(
        session=FakeSession([FakeResponse(_api_release("3.4.0"))]), repo="o/r"
    )
    release = stable.latest("stable")
    assert release.tag == "3.4.0" and release.assets[0].sha256 == "a" * 64
    assert stable.session.calls[0][0].endswith("/repos/o/r/releases/latest")

    listing = [
        _api_release("tile-index", prerelease=True),
        _api_release("3.6.0-rc-1", prerelease=True, draft=True),
        _api_release("3.5.0-rc-2", prerelease=True),
        _api_release("3.4.0"),
    ]
    rc = GitHubReleases(session=FakeSession([FakeResponse(listing)]), repo="o/r")
    assert rc.latest("rc").tag == "3.5.0-rc-2"


# -- download --------------------------------------------------------------------


def test_download_verifies_and_resumes(tmp_path):
    data = b"0123456789" * 50
    sha = hashlib.sha256(data).hexdigest()
    target = tmp_path / "file.zip"

    session = FakeSession([FakeResponse(chunks=[data[:200], data[200:]])])
    seen = []
    assert download("u", target, len(data), sha, session=session,
                    progress=lambda d, t: seen.append(d)) == target
    assert target.read_bytes() == data and seen[-1] == len(data)

    # Already there and intact: no request at all.
    assert download("u", target, len(data), sha, session=FakeSession([])) == target

    target.unlink()
    part = tmp_path / "file.zip.part"
    part.write_bytes(data[:100])
    session = FakeSession([
        FakeResponse(status=206, chunks=[data[100:]], headers={"Content-Range": f"bytes 100-{len(data) - 1}/{len(data)}"})
    ])
    download("u", target, len(data), sha, session=session)
    assert session.calls[0][1]["headers"]["Range"] == "bytes=100-"
    assert target.read_bytes() == data

    # A server that ignores the range sends everything again.
    target.unlink()
    part.write_bytes(data[:100])
    download("u", target, len(data), sha, session=FakeSession([FakeResponse(chunks=[data])]))
    assert target.read_bytes() == data


def test_download_rejects_bad_data(tmp_path):
    data = b"x" * 64
    target = tmp_path / "f.zip"
    with pytest.raises(DownloadError):
        download("u", target, len(data), "0" * 64, session=FakeSession([FakeResponse(chunks=[data])]))
    assert not target.exists() and not (tmp_path / "f.zip.part").exists()

    with pytest.raises(DownloadError):
        download("u", target, 10, "0" * 64, session=FakeSession([FakeResponse(chunks=[data])]))

    with pytest.raises(DownloadCancelled):
        download(
            "u", target, len(data), hashlib.sha256(data).hexdigest(),
            session=FakeSession([FakeResponse(chunks=[data[:32], data[32:]])]),
            cancelled=lambda: True,
        )


# -- staging ---------------------------------------------------------------------------


def _zip_build(path, version="3.5.0", top="autoortho_release", extra=None):
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"{top}/autoortho.exe", b"MZ")
        bundle.writestr(f"{top}/ao_files/autoortho/.version", version)
        bundle.writestr(f"{top}/ao_files/lib.dll", b"lib")
        for name, data in (extra or {}).items():
            bundle.writestr(name, data)
    return path


def _tar_build(path, version="3.5.0", variant="jammy", top="autoortho_linux_3.5.0"):
    with tarfile.open(path, "w:gz") as bundle:
        def add(name, data, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            bundle.addfile(info, io.BytesIO(data))

        add(f"{top}/autoortho", b"#!", 0o755)
        add(f"{top}/ao_files/autoortho/.version", version.encode())
        add(
            f"{top}/ao_files/autoortho/.build-info.json",
            json.dumps({"platform": "linux", "linux_variant": variant}).encode(),
        )
    return path


def test_zip_stage_and_validate(tmp_path):
    archive = _zip_build(tmp_path / "w.zip")
    staged = tmp_path / "staged"
    extract(archive, staged, "zip", "windows")
    root = find_bundle_root(staged, "windows")
    assert root == staged / "autoortho_release"
    validate_bundle(root, "windows", parse_release_version("3.5.0"))
    with pytest.raises(StageError):
        validate_bundle(root, "windows", parse_release_version("3.6.0"))


def test_zip_slip_is_refused(tmp_path):
    archive = _zip_build(tmp_path / "evil.zip", extra={"../escape.txt": b"x"})
    with pytest.raises(StageError):
        extract(archive, tmp_path / "staged", "zip", "windows")
    assert not (tmp_path / "escape.txt").exists()


def test_tar_stage_checks_linux_variant(tmp_path):
    archive = _tar_build(tmp_path / "l.tar.gz", variant="resolute")
    extract(archive, tmp_path / "staged", "tar", "linux")
    root = find_bundle_root(tmp_path / "staged", "linux")
    validate_bundle(root, "linux", parse_release_version("3.5.0"), linux_variant="resolute")
    with pytest.raises(StageError):
        validate_bundle(root, "linux", parse_release_version("3.5.0"), linux_variant="jammy")


def test_two_builds_in_one_archive_are_refused(tmp_path):
    archive = _zip_build(
        tmp_path / "two.zip",
        extra={"second/autoortho.exe": b"MZ", "second/ao_files/x": b""},
    )
    extract(archive, tmp_path / "staged", "zip", "windows")
    with pytest.raises(StageError):
        find_bundle_root(tmp_path / "staged", "windows")


# -- plan files ----------------------------------------------------------------------------


def test_plan_round_trip_ignores_unknown_fields(tmp_path):
    plan = UpdatePlan(
        action=ACTION_SWAP, wait_pid=1, target="t", platform="linux",
        from_version="3.4.0", to_version="3.5.0",
    )
    path = write_plan(plan, tmp_path / "plan.json")
    data = json.loads(path.read_text())
    data["future_field"] = 1
    path.write_text(json.dumps(data))
    assert read_plan(path) == plan
    data["action"] = "explode"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_plan(path)


def test_start_confirmation_only_for_the_expected_version(tmp_path):
    write_pending_start("3.5.0", tmp_path, from_version="3.4.0")
    assert confirm_started("3.4.0", tmp_path) is None
    pending = confirm_started("3.5.0", tmp_path)
    assert pending["from_version"] == "3.4.0" and not pending["rollback"]
    assert plan_module.has_started("3.5.0", tmp_path)


# -- helper ------------------------------------------------------------------------------


def _linux_build(folder, version):
    (folder / "ao_files" / "autoortho").mkdir(parents=True)
    (folder / "autoortho").write_bytes(b"#!")
    (folder / "ao_files" / "autoortho" / ".version").write_text(version)
    return folder


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def _swap_plan(tmp_path, rollback=False):
    target = _linux_build(tmp_path / "install" / "autoortho_linux_3.4.0", "3.4.0")
    source = _linux_build(tmp_path / "updates" / "3.5.0" / "staged" / "b", "3.5.0")
    return UpdatePlan(
        action=ACTION_SWAP,
        wait_pid=123,
        target=str(target),
        platform="linux",
        from_version="3.4.0",
        to_version="3.5.0",
        source=str(source),
        backup=str(target) + ".previous-3.4.0",
        relaunch=[str(target / "autoortho")],
        rollback=rollback,
        health_timeout=30,
        updates_root=str(tmp_path / "updates"),
    )


def _helper(plan, start_app, run_elevated=None):
    return Helper(
        plan,
        start_app=start_app,
        run_elevated=run_elevated or (lambda *a: 0),
        sleep=lambda seconds: None,
        clock=Clock(),
        wait_for_exit=lambda *a: True,
        wait_for_folder=lambda *a: True,
    )


def _version_at(folder):
    return (Path(folder) / "ao_files" / "autoortho" / ".version").read_text()


def test_swap_installs_and_keeps_a_backup(tmp_path):
    plan = _swap_plan(tmp_path)
    root = Path(plan.updates_root)
    started = []

    def start_app(command):
        started.append(_version_at(plan.target))
        confirm_started(plan.to_version, root)
        return FakeProcess()

    assert _helper(plan, start_app).run() == 0
    assert started == ["3.5.0"]
    assert _version_at(plan.target) == "3.5.0"
    assert _version_at(plan.backup) == "3.4.0"
    state = read_state(root)
    assert state["backup"]["path"] == plan.backup
    assert state["backup"]["version"] == "3.4.0"
    assert take_result(root) is None  # success leaves no report
    assert plan_module.pending_start(root) is None


def test_swap_rolls_back_when_new_version_exits(tmp_path):
    plan = _swap_plan(tmp_path)
    root = Path(plan.updates_root)
    versions = []

    def start_app(command):
        versions.append(_version_at(plan.target))
        return FakeProcess(code=1)  # exits before reporting

    assert _helper(plan, start_app).run() == 1
    assert versions == ["3.5.0", "3.4.0"]  # new one tried, old one restarted
    assert _version_at(plan.target) == "3.4.0"
    assert not Path(plan.backup).exists()
    result = take_result(root)
    assert result["status"] == "rolled_back"


def test_swap_refuses_an_existing_backup(tmp_path):
    plan = _swap_plan(tmp_path)
    Path(plan.backup).mkdir()
    relaunched = []
    assert _helper(plan, lambda c: relaunched.append(c) or FakeProcess()).run() == 1
    assert _version_at(plan.target) == "3.4.0"
    assert relaunched  # the old version is started again
    assert take_result(Path(plan.updates_root))["status"] == "failed"


def test_swap_restores_when_copy_fails(tmp_path, monkeypatch):
    plan = _swap_plan(tmp_path)

    def broken_copy(self, source, target):
        Path(target).mkdir()
        raise OSError("disk full")

    monkeypatch.setattr(Helper, "_copy_build", broken_copy)
    assert _helper(plan, lambda c: FakeProcess()).run() == 1
    assert _version_at(plan.target) == "3.4.0"
    assert take_result(Path(plan.updates_root))["status"] == "failed"


def test_rollback_drops_the_newer_version(tmp_path):
    plan = _swap_plan(tmp_path, rollback=True)
    root = Path(plan.updates_root)
    write_state({"backup": {"path": plan.source, "version": "3.5.0"}, "cleanup": []}, root)

    def start_app(command):
        confirm_started(plan.to_version, root)
        return FakeProcess()

    assert _helper(plan, start_app).run() == 0
    state = read_state(root)
    assert state["backup"] is None
    assert not Path(plan.backup).exists()


def _installer_plan(tmp_path):
    target = _windows_build(tmp_path / "AutoOrtho", nsis=True).parent
    return UpdatePlan(
        action=ACTION_INSTALLER,
        wait_pid=1,
        target=str(target),
        platform="windows",
        from_version="3.4.0",
        to_version="3.5.0",
        installer=str(tmp_path / "AutoOrtho_win_3.5.0.exe"),
        installer_args=["/S", "/UPDATE", "/CurrentUser", f'/INSTDIR="{target}"'],
        relaunch=[str(target / "autoortho.exe")],
        health_timeout=30,
        updates_root=str(tmp_path / "updates"),
    )


def test_installer_paths(tmp_path):
    plan = _installer_plan(tmp_path)
    root = Path(plan.updates_root)
    calls = []

    def elevated(exe, args):
        calls.append(args)
        return 0

    def start_app(command):
        confirm_started(plan.to_version, root)
        return FakeProcess()

    assert _helper(plan, start_app, elevated).run() == 0
    assert calls == [" ".join(plan.installer_args)]

    starts = []
    assert _helper(plan, lambda c: starts.append(c) or FakeProcess(), lambda *a: None).run() == 1
    assert take_result(root)["status"] == "cancelled" and starts

    codes = iter([2, 1])
    seen = []

    def failing(exe, args):
        seen.append(args)
        return next(codes)

    assert _helper(plan, lambda c: FakeProcess(), failing).run() == 1
    assert "/S" not in seen[1].split()  # retried with the installer's pages
    assert take_result(root)["status"] == "broken"


# -- manager -----------------------------------------------------------------------------


def _manager_install(tmp_path, kind="tar", version="3.4.0"):
    root = _linux_build(tmp_path / "install" / "autoortho_linux_3.4.0", version)
    return InstallInfo(
        platform="linux",
        kind=kind,
        version_text=version,
        version=parse_release_version(version),
        root=root,
        executable=root / "autoortho",
        linux_variant="jammy",
    )


def test_prepare_and_plan_end_to_end(tmp_path, monkeypatch):
    install = _manager_install(tmp_path)
    archive = _tar_build(tmp_path / "rel.tar.gz", variant="jammy")
    data = archive.read_bytes()
    release = _release(
        "3.5.0",
        [ReleaseAsset("autoortho_linux_3.5.0_jammy.tar.gz", len(data), "https://example.test/l", hashlib.sha256(data).hexdigest())],
    )
    manager = UpdateManager(install=install, root=tmp_path / "updates", client=FakeClient(release=release))
    offer = manager.check()
    assert offer.can_install

    import updater.download as download_module

    monkeypatch.setattr(download_module.requests, "Session", lambda: FakeSession([FakeResponse(chunks=[data])]))
    prepared = manager.prepare(offer)
    assert prepared.action == ACTION_SWAP
    assert _version_at(prepared.source) == "3.5.0"
    # A second prepare reuses the staged build without downloading.
    monkeypatch.setattr(download_module.requests, "Session", lambda: FakeSession([]))
    assert manager.prepare(offer).source == prepared.source

    plan = manager.make_plan(prepared, pid=42, created=1.0)
    assert plan.backup.endswith("autoortho_linux_3.4.0.previous-3.4.0")
    assert plan.relaunch == [str(install.root / "autoortho")]
    Path(plan.backup).mkdir()
    assert manager.make_plan(prepared, pid=42).backup.endswith("-2")


def test_installer_plan_arguments(tmp_path):
    root = tmp_path / "Program Files" / "AutoOrtho"
    install = InstallInfo(
        platform="windows", kind="nsis", version_text="3.4.0",
        version=parse_release_version("3.4.0"), root=root,
        executable=root / "autoortho.exe", all_users=True,
    )
    manager = UpdateManager(install=install, root=tmp_path / "u")
    from updater.service import PreparedUpdate

    plan = manager.make_plan(
        PreparedUpdate("3.5.0", ACTION_INSTALLER, root / "x.exe", installer=tmp_path / "i.exe"),
        pid=1,
        created=0,
    )
    assert plan.installer_args == ["/S", "/UPDATE", "/AllUsers", f'/INSTDIR="{root}"']


def test_startup_cleanup_and_rollback_target(tmp_path):
    install = _manager_install(tmp_path)
    root = tmp_path / "updates"
    manager = UpdateManager(install=install, root=root)
    for name in ("3.3.0", "3.4.0", "3.5.0", "junk"):
        (root / name).mkdir(parents=True)
    leftover = tmp_path / "old-backup"
    leftover.mkdir()
    backup = _linux_build(tmp_path / "install" / "autoortho_linux_3.4.0.previous-3.3.0", "3.3.0")
    write_state(
        {
            "backup": {"path": str(backup), "version": "3.3.0", "target": str(install.root)},
            "cleanup": [str(leftover)],
        },
        root,
    )
    report = manager.startup("3.4.0")
    assert report.result is None and report.started is None
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["3.5.0"]
    assert not leftover.exists()
    assert manager.rollback_target() == (backup, "3.3.0")

    prepared = manager.prepare_rollback()
    assert prepared.rollback and prepared.source == backup

    # While a fresh update waits for its start report, nothing is removed.
    (root / "3.4.0").mkdir()
    write_pending_start("3.4.0", root)
    report = manager.startup("3.4.0")
    assert report.started["version"] == "3.4.0"
    assert (root / "3.4.0").exists()


def test_clean_env_for_independent_processes():
    env = launch.clean_env(
        {
            "AO_RUN_MODE": "mount_worker",
            "AO_UPDATE_PLAN": "x",
            "_PYI_APPLICATION_HOME_DIR": "y",
            "LD_LIBRARY_PATH": "/bundle",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib",
            "HOME": "/home/me",
        }
    )
    assert env == {
        "LD_LIBRARY_PATH": "/usr/lib",
        "HOME": "/home/me",
        "PYINSTALLER_RESET_ENVIRONMENT": "1",
    }
    assert launch.relaunch_command("macos", Path("/A.app"), None)[:3] == ["/usr/bin/open", "-n", "-W"]


# -- release scripts -------------------------------------------------------------------------


def test_manifest_script_writes_and_signs(tmp_path, monkeypatch):
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import make_update_manifest
    finally:
        sys.path.pop(0)
    monkeypatch.setenv("AO_UPDATE_SIGNING_KEY", base64.b64encode(RFC_SECRET).decode())
    files = _release_files(tmp_path / "files")
    assert make_update_manifest.main(["--tag", "3.5.0", "--out", str(tmp_path / "out"), *files]) == 0
    data = (tmp_path / "out" / MANIFEST_NAME).read_bytes()
    signature = (tmp_path / "out" / SIGNATURE_NAME).read_text()
    assert verify_manifest(data, signature, keys=[RFC_PUBLIC])
    assert parse_manifest(data).tag == "3.5.0"
