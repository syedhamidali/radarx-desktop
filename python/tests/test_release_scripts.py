"""Tests of the release scripts in ``scripts/release`` (the download channel, docs/RELEASING.md).

The scripts live outside ``python/`` and use only the standard library; they are imported by path.
Buckets are local directories, so nothing here touches a network, R2 or any account. The synthetic
``latest.json`` has the shape tauri-action writes (asset urls on a GitHub release).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "release"


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


common = _load("release_common")
rewrite_latest = _load("rewrite_latest")
build_index = _load("build_index")
upload_version = _load("upload_version")
promote_mod = _load("promote")

BASE = "https://downloads.labshope.com"
GH = "https://github.com/example-org/example-repo/releases/download"


def _sig(label: str) -> str:
    """A minisign-shaped signature: base64 of a two-comment text, different for every file."""
    text = (
        "untrusted comment: signature from tauri secret key\n"
        + base64.b64encode(hashlib.sha256(label.encode()).digest() * 2).decode()
        + "\ntrusted comment: timestamp:1760000000\tfile:"
        + label
        + "\n"
        + base64.b64encode(hashlib.sha256(b"g" + label.encode()).digest() * 2).decode()
        + "\n"
    )
    return base64.b64encode(text.encode()).decode()


def installer_names(v: str) -> dict[str, str]:
    return {
        "mac_arm_tgz": "radarx-desktop_aarch64.app.tar.gz",
        "mac_intel_tgz": "radarx-desktop_x64.app.tar.gz",
        "mac_arm_dmg": f"radarx-desktop_{v}_aarch64.dmg",
        "mac_intel_dmg": f"radarx-desktop_{v}_x64.dmg",
        "win": f"radarx-desktop_{v}_x64-setup.exe",
        "appimage": f"radarx-desktop_{v}_amd64.AppImage",
        "deb": f"radarx-desktop_{v}_amd64.deb",
        "rpm": f"radarx-desktop-{v}-1.x86_64.rpm",
    }


def github_manifest(v: str = "0.3.0", notes: str = "What is new\n- one\n- two") -> dict:
    n = installer_names(v)

    def entry(key: str) -> dict:
        return {"signature": _sig(n[key]), "url": f"{GH}/v{v}/{n[key]}"}

    return {
        "version": v,
        "notes": notes,
        "pub_date": "2026-10-20T10:00:00.000Z",
        "platforms": {
            "darwin-aarch64": entry("mac_arm_tgz"),
            "darwin-aarch64-app": entry("mac_arm_tgz"),
            "darwin-x86_64": entry("mac_intel_tgz"),
            "darwin-x86_64-app": entry("mac_intel_tgz"),
            "linux-x86_64": entry("appimage"),
            "windows-x86_64": entry("win"),
            "windows-x86_64-nsis": entry("win"),
        },
    }


def make_download_dir(root: Path, v: str = "0.3.0", manifest: dict | None = None) -> Path:
    """What `gh release download` leaves: installers, .sig files and tauri-action's latest.json."""
    root.mkdir(parents=True, exist_ok=True)
    for key, name in installer_names(v).items():
        (root / name).write_bytes(f"binary {name} {'x' * 64}".encode() * 50)
        if key not in ("mac_arm_dmg", "mac_intel_dmg", "deb", "rpm"):
            (root / (name + ".sig")).write_text(_sig(name))
    (root / "latest.json").write_text(json.dumps(manifest or github_manifest(v)))
    return root


def upload(bucket, folder: Path, v: str) -> None:
    """What the workflow's upload step does: the folder goes to radarx/v<version>/."""
    for p in sorted(folder.iterdir()):
        bucket.put(
            f"radarx/v{v}/{p.name}",
            p.read_bytes(),
            cache_control=common.CACHE_IMMUTABLE,
            content_type=common.content_type_of(p.name),
        )


def prepared_version(tmp_path: Path, bucket, v: str, **kw) -> Path:
    folder = make_download_dir(tmp_path / f"dl-{v}", v, **kw)
    rewrite_latest.prepare_folder(folder, f"v{v}")
    upload(bucket, folder, v)
    return folder


def snapshot(bucket) -> dict[str, bytes]:
    return {k: bucket.get(k) for k in bucket.list_objects("")}


@pytest.fixture
def bucket(tmp_path: Path):
    return common.LocalBucket(tmp_path / "bucket")


# --- versions, names, checksums -----------------------------------------------------------------


def test_version_parsing_and_ordering() -> None:
    assert common.version_of("v0.3.0") == "0.3.0"
    assert common.version_of("0.3.0-rc.1") == "0.3.0-rc.1"
    for bad in ("", "v", "latest", "1.2", "1.2.3.4", "v01.2.3", "1.2.3+build", "../1.2.3"):
        with pytest.raises(common.ReleaseError):
            common.version_of(bad)
    order = ["0.2.1", "0.3.0-rc.2", "0.3.0-rc.10", "0.3.0", "0.9.0", "0.10.0", "1.0.0"]
    assert sorted((order[i] for i in (4, 0, 6, 2, 5, 1, 3)), key=common.version_key) == order
    assert common.version_key("1.0.0-alpha") < common.version_key("1.0.0-alpha.1")
    assert common.version_key("1.0.0-alpha.beta") < common.version_key("1.0.0-beta")


def test_sums_round_trip_and_refuse_bad_lines() -> None:
    sums = {"b.dmg": "a" * 64, "a.exe": "b" * 64}
    text = common.format_sums(sums)
    assert text.splitlines()[0].endswith("  a.exe")  # sorted, sha256sum layout
    assert common.parse_sums(text) == sums
    assert common.parse_sums(f"{'c' * 64} *star.bin\n") == {"star.bin": "c" * 64}
    for bad in ("nothex  file\n", f"{'a' * 64} ../evil\n", f"{'a' * 64}  a\n{'b' * 64}  a\n"):
        with pytest.raises(common.ReleaseError):
            common.parse_sums(bad)


def test_cache_control_rules() -> None:
    assert common.cache_control_of("radarx/latest.json") == "no-cache"
    assert common.cache_control_of("radarx/releases.json") == "no-cache"
    immutable = "public, max-age=31536000, immutable"
    assert common.cache_control_of("radarx/v0.3.0/latest.json") == immutable
    assert common.cache_control_of("radarx/v0.3.0/x.dmg") == immutable


# --- rewriting ----------------------------------------------------------------------------------


def test_rewrite_points_every_url_at_the_download_host_and_keeps_signatures_byte_for_byte() -> None:
    src = github_manifest()
    before = json.dumps(src, sort_keys=True)
    out = common.rewrite_manifest(src, "v0.3.0")
    assert json.dumps(src, sort_keys=True) == before  # the input is not modified
    assert set(out["platforms"]) == set(src["platforms"])
    for key, entry in out["platforms"].items():
        name = src["platforms"][key]["url"].rsplit("/", 1)[1]
        assert entry["url"] == f"{BASE}/radarx/v0.3.0/{name}"
        assert entry["signature"] == src["platforms"][key]["signature"]  # identical string
        assert set(entry) == {"url", "signature"}
    for field in ("version", "notes", "pub_date"):
        assert out[field] == src[field]


def test_prepare_folder_writes_candidate_and_checksums(tmp_path: Path) -> None:
    folder = make_download_dir(tmp_path / "dl")
    original = json.loads((folder / "latest.json").read_text())
    sums = rewrite_latest.prepare_folder(folder, "v0.3.0")
    cand = json.loads((folder / "latest.json").read_text())
    for key, entry in cand["platforms"].items():
        assert entry["url"].startswith(f"{BASE}/radarx/v0.3.0/")
        assert "github" not in entry["url"]
        assert entry["signature"] == original["platforms"][key]["signature"]
        # and equals the .sig file the build wrote
        name = entry["url"].rsplit("/", 1)[1]
        assert (folder / (name + ".sig")).read_text().strip() == entry["signature"]
    on_disk = common.parse_sums((folder / "SHA256SUMS").read_text())
    assert on_disk == sums
    assert set(sums) == {p.name for p in folder.iterdir()} - {"SHA256SUMS"}
    assert "latest.json" in sums
    for name, digest in sums.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest


def test_prepare_folder_is_idempotent(tmp_path: Path) -> None:
    folder = make_download_dir(tmp_path / "dl")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    first = {p.name: p.read_bytes() for p in folder.iterdir()}
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == first


def _break(manifest: dict, how: str) -> None:
    plats = manifest["platforms"]
    if how == "no-signature":
        del plats["windows-x86_64"]["signature"]
    elif how == "empty-signature":
        plats["darwin-aarch64"]["signature"] = "  "
    elif how == "signature-not-base64":
        plats["darwin-aarch64"]["signature"] = "not base64 !!"
    elif how == "http-url":
        plats["darwin-x86_64"]["url"] = plats["darwin-x86_64"]["url"].replace("https:", "http:")
    elif how == "no-url":
        del plats["darwin-x86_64"]["url"]
    elif how == "wrong-version":
        manifest["version"] = "0.2.9"
    elif how == "no-version":
        del manifest["version"]
    elif how == "no-notes":
        del manifest["notes"]
    elif how == "bad-date":
        manifest["pub_date"] = "last tuesday"
    elif how == "no-platforms":
        manifest["platforms"] = {}
    elif how == "missing-windows":
        for k in ("windows-x86_64", "windows-x86_64-nsis"):
            del plats[k]
    elif how == "file-name-with-path":
        plats["darwin-x86_64"]["url"] = f"{GH}/v0.3.0/..%2Fevil.tar.gz"
    else:  # pragma: no cover
        raise AssertionError(how)


@pytest.mark.parametrize(
    "how",
    [
        "no-signature",
        "empty-signature",
        "signature-not-base64",
        "http-url",
        "no-url",
        "wrong-version",
        "no-version",
        "no-notes",
        "bad-date",
        "no-platforms",
        "missing-windows",
        "file-name-with-path",
    ],
)
def test_a_bad_manifest_is_refused_and_nothing_is_written(tmp_path: Path, how: str) -> None:
    manifest = github_manifest()
    _break(manifest, how)
    folder = make_download_dir(tmp_path / "dl", manifest=manifest)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(common.ReleaseError):
        rewrite_latest.prepare_folder(folder, "v0.3.0")
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_a_version_that_does_not_match_the_tag_is_refused(tmp_path: Path) -> None:
    folder = make_download_dir(tmp_path / "dl")
    with pytest.raises(common.ReleaseError, match="does not match"):
        rewrite_latest.prepare_folder(folder, "v0.3.1")


def test_missing_file_missing_sig_and_sig_mismatch_are_refused(tmp_path: Path) -> None:
    name = installer_names("0.3.0")["win"]
    for what, expect in (("file", "not in"), ("sig", "not in"), ("tamper-sig", "differs")):
        folder = make_download_dir(tmp_path / what)
        if what == "file":
            (folder / name).unlink()
        elif what == "sig":
            (folder / (name + ".sig")).unlink()
        else:
            (folder / (name + ".sig")).write_text(_sig("another file"))
        with pytest.raises(common.ReleaseError, match=expect):
            rewrite_latest.prepare_folder(folder, "v0.3.0")
        assert "SHA256SUMS" not in {p.name for p in folder.iterdir()}


def test_not_json_and_missing_manifest_are_refused(tmp_path: Path) -> None:
    folder = make_download_dir(tmp_path / "dl")
    (folder / "latest.json").write_text("<html>not json</html>")
    with pytest.raises(common.ReleaseError, match="not valid JSON"):
        rewrite_latest.prepare_folder(folder, "v0.3.0")
    (folder / "latest.json").unlink()
    with pytest.raises(common.ReleaseError, match="no manifest"):
        rewrite_latest.prepare_folder(folder, "v0.3.0")


def test_rewrite_cli_exit_codes(tmp_path: Path, capsys) -> None:
    folder = make_download_dir(tmp_path / "dl")
    assert rewrite_latest.main(["--tag", "v0.3.0", "--dir", str(folder)]) == 0
    assert "checksummed" in capsys.readouterr().out
    assert rewrite_latest.main(["--tag", "v9.9.9", "--dir", str(folder)]) == 1
    assert "REFUSED" in capsys.readouterr().err
    # a build that lacks a platform can be accepted explicitly
    manifest = github_manifest()
    _break(manifest, "missing-windows")
    partial = make_download_dir(tmp_path / "partial", manifest=manifest)
    assert rewrite_latest.main(["--tag", "v0.3.0", "--dir", str(partial)]) == 1
    args = ["--tag", "v0.3.0", "--dir", str(partial), "--require", "darwin-aarch64"]
    assert rewrite_latest.main(args) == 0


# --- classifying installers and the index -------------------------------------------------------


def test_classify_asset() -> None:
    c = build_index.classify_asset
    assert c("radarx-desktop_0.3.0_aarch64.dmg") == {
        "os": "macos",
        "arch": "aarch64",
        "kind": "dmg",
    }
    assert c("radarx-desktop_0.3.0_x64.dmg") == {"os": "macos", "arch": "x86_64", "kind": "dmg"}
    assert c("radarx-desktop_0.3.0_x64-setup.exe") == {
        "os": "windows",
        "arch": "x86_64",
        "kind": "exe",
    }
    assert c("radarx-desktop_0.3.0_arm64_en-US.msi")["arch"] == "aarch64"
    assert c("radarx-desktop_0.3.0_amd64.deb") == {"os": "linux", "arch": "x86_64", "kind": "deb"}
    assert c("radarx-desktop-0.3.0-1.x86_64.rpm")["arch"] == "x86_64"
    assert c("radarx-desktop_0.3.0_aarch64.AppImage") == {
        "os": "linux",
        "arch": "aarch64",
        "kind": "appimage",
    }
    assert c("radarx-windows-x64.zip") == {"os": "windows", "arch": "x86_64", "kind": "zip"}
    for not_an_installer in (
        "radarx-desktop_aarch64.app.tar.gz",
        "radarx-desktop_0.3.0_x64-setup.exe.sig",
        "latest.json",
        "SHA256SUMS",
        "notes.txt",
    ):
        assert c(not_an_installer) is None
    with pytest.raises(common.ReleaseError):
        c("radarx-desktop_0.3.0.dmg")  # architecture unknown
    with pytest.raises(common.ReleaseError):
        c("radarx-desktop_0.3.0_x64.zip")  # system unknown


def test_the_index_lists_only_promoted_versions_newest_first(tmp_path: Path, bucket) -> None:
    for v in ("0.2.2", "0.3.0", "0.3.1"):
        prepared_version(tmp_path, bucket, v)
    assert build_index.build_index(bucket) == {
        "schema": 1,
        "product": "radarx",
        "latest": None,
        "releases": [],
    }
    promote_mod.promote(bucket, "v0.2.2")
    promote_mod.promote(bucket, "v0.3.0")  # 0.3.1 stays a candidate
    index = json.loads(bucket.get("radarx/releases.json"))
    assert [r["version"] for r in index["releases"]] == ["0.3.0", "0.2.2"]
    assert index["latest"] == "0.3.0"
    assert index["schema"] == 1 and index["product"] == "radarx"


def test_index_entries(tmp_path: Path, bucket) -> None:
    folder = prepared_version(tmp_path, bucket, "0.3.0")
    promote_mod.promote(bucket, "v0.3.0")
    rel = json.loads(bucket.get("radarx/releases.json"))["releases"][0]
    assert set(rel) == {"version", "date", "notes", "assets"}
    assert rel["date"] == "2026-10-20T10:00:00.000Z"
    assert rel["notes"] == "What is new\n- one\n- two"
    names = [a["name"] for a in rel["assets"]]
    assert names == [
        "radarx-desktop_0.3.0_aarch64.dmg",
        "radarx-desktop_0.3.0_x64.dmg",
        "radarx-desktop_0.3.0_x64-setup.exe",
        "radarx-desktop_0.3.0_amd64.AppImage",
        "radarx-desktop_0.3.0_amd64.deb",
        "radarx-desktop-0.3.0-1.x86_64.rpm",
    ]
    for a in rel["assets"]:
        assert set(a) == {"os", "arch", "kind", "name", "size", "sha256", "url"}
        assert a["os"] in ("macos", "windows", "linux") and a["arch"] in ("aarch64", "x86_64")
        assert a["url"] == f"{BASE}/radarx/v0.3.0/{a['name']}"
        data = (folder / a["name"]).read_bytes()
        assert a["size"] == len(data)
        assert a["sha256"] == hashlib.sha256(data).hexdigest()
    dmg = rel["assets"][0]
    assert (dmg["os"], dmg["arch"], dmg["kind"]) == ("macos", "aarch64", "dmg")


def test_the_index_is_deterministic_and_has_no_clock(tmp_path: Path, bucket) -> None:
    for v in ("0.3.0", "0.3.1"):
        prepared_version(tmp_path, bucket, v)
        promote_mod.promote(bucket, f"v{v}")
    first = bucket.get("radarx/releases.json")
    again = common.dump_json(build_index.build_index(bucket))
    assert again == first
    assert first.endswith(b"\n")
    assert b"generated" not in first
    out = tmp_path / "out.json"
    assert build_index.main(["--bucket-dir", str(bucket.root), "--out", str(out)]) == 0
    assert out.read_bytes() == first


# --- upload ------------------------------------------------------------------------------------


def test_upload_puts_the_folder_under_the_version_and_never_the_live_names(
    tmp_path: Path, bucket
) -> None:
    folder = make_download_dir(tmp_path / "dl")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    keys = upload_version.upload_version(bucket, folder, "v0.3.0")
    assert keys[-2:] == ["radarx/v0.3.0/latest.json", "radarx/v0.3.0/SHA256SUMS"]  # completed last
    stored = set(bucket.list_objects(""))
    assert stored == {f"radarx/v0.3.0/{p.name}" for p in folder.iterdir()}
    assert "radarx/latest.json" not in stored and "radarx/releases.json" not in stored
    assert "radarx/v0.3.0/PROMOTION.json" not in stored  # an upload is a candidate
    for k in stored:
        assert bucket.meta[k]["cache_control"] == "public, max-age=31536000, immutable"
        assert bucket.get(k) == (folder / k.rsplit("/", 1)[1]).read_bytes()
    assert bucket.meta["radarx/v0.3.0/latest.json"]["content_type"] == "application/json"
    # the candidate can be re-uploaded (a re-run of the build job) until it is promoted
    upload_version.upload_version(bucket, folder, "v0.3.0")
    promote_mod.promote(bucket, "v0.3.0")
    with pytest.raises(common.ReleaseError, match="immutable"):
        upload_version.upload_version(bucket, folder, "v0.3.0")


def test_upload_refuses_a_folder_that_was_not_prepared_or_was_changed(
    tmp_path: Path, bucket
) -> None:
    folder = make_download_dir(tmp_path / "dl")
    with pytest.raises(common.ReleaseError, match="rewrite_latest"):
        upload_version.upload_version(bucket, folder, "v0.3.0")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    (folder / "surprise.bin").write_bytes(b"x")
    with pytest.raises(common.ReleaseError, match="not in"):
        upload_version.upload_version(bucket, folder, "v0.3.0")
    (folder / "surprise.bin").unlink()
    deb = folder / installer_names("0.3.0")["deb"]
    deb.write_bytes(b"changed after the checksums were made")
    with pytest.raises(common.ReleaseError, match="does not match"):
        upload_version.upload_version(bucket, folder, "v0.3.0")
    with pytest.raises(common.ReleaseError, match="does not match"):
        upload_version.upload_version(bucket, folder, "v0.3.1")  # manifest is for 0.3.0
    assert bucket.list_objects("") == {}


def test_upload_and_verify_cli(tmp_path: Path, bucket, capsys) -> None:
    folder = make_download_dir(tmp_path / "dl")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    d = str(bucket.root)
    assert promote_mod.main(["verify", "--tag", "v0.3.0", "--bucket-dir", d]) == 1
    assert upload_version.main(["--tag", "v0.3.0", "--dir", str(folder), "--bucket-dir", d]) == 0
    assert "candidate, not live" in capsys.readouterr().out
    assert promote_mod.main(["verify", "--tag", "v0.3.0", "--bucket-dir", d]) == 0
    assert bucket.list_objects("radarx/latest.json") == {}  # verify and upload never go live
    (bucket.root / "radarx/v0.3.0" / installer_names("0.3.0")["win"]).write_bytes(b"corrupt")
    assert promote_mod.main(["verify", "--tag", "v0.3.0", "--bucket-dir", d]) == 1
    assert upload_version.main(["--tag", "v0.3.0", "--dir", str(tmp_path), "--bucket-dir", d]) == 1


# --- promote ------------------------------------------------------------------------------------


def test_promote_publishes_the_candidate_and_the_index(tmp_path: Path, bucket) -> None:
    folder = prepared_version(tmp_path, bucket, "0.3.0")
    candidate = bucket.get("radarx/v0.3.0/latest.json")
    assert bucket.list_objects("radarx/latest.json") == {}  # nothing is live before promote
    res = promote_mod.promote(bucket, "v0.3.0")
    assert res.version == "0.3.0" and res.live_before is None and res.withdrawn == []
    assert bucket.get("radarx/latest.json") == candidate
    live = json.loads(bucket.get("radarx/latest.json"))
    original = github_manifest()
    for key, entry in live["platforms"].items():
        assert entry["signature"] == original["platforms"][key]["signature"]
        assert entry["url"].startswith(f"{BASE}/radarx/v0.3.0/")
    assert bucket.meta["radarx/latest.json"]["cache_control"] == "no-cache"
    assert bucket.meta["radarx/releases.json"]["cache_control"] == "no-cache"
    assert json.loads(bucket.get("radarx/v0.3.0/PROMOTION.json")) == {
        "status": "promoted",
        "version": "0.3.0",
    }
    # the installers were not touched
    for p in folder.iterdir():
        assert bucket.get(f"radarx/v0.3.0/{p.name}") == p.read_bytes()


def test_promote_is_idempotent(tmp_path: Path, bucket) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    promote_mod.promote(bucket, "v0.3.0")
    first = snapshot(bucket)
    promote_mod.promote(bucket, "v0.3.0")
    assert snapshot(bucket) == first


def test_promote_refuses_when_files_are_missing(tmp_path: Path, bucket) -> None:
    with pytest.raises(common.ReleaseError, match="not ready"):
        promote_mod.promote(bucket, "v0.3.0")  # no folder at all
    folder = make_download_dir(tmp_path / "dl")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    for skip, expect in (("latest.json", "not ready"), ("SHA256SUMS", "not ready")):
        b = common.LocalBucket(tmp_path / f"b-{skip}")
        upload(b, folder, "0.3.0")
        (b.root / "radarx/v0.3.0" / skip).unlink()
        with pytest.raises(common.ReleaseError, match=expect):
            promote_mod.promote(b, "v0.3.0")
        assert b.list_objects("radarx/latest.json") == {}
    # an installer listed in SHA256SUMS that was never uploaded
    b = common.LocalBucket(tmp_path / "b-installer")
    upload(b, folder, "0.3.0")
    (b.root / "radarx/v0.3.0" / installer_names("0.3.0")["deb"]).unlink()
    with pytest.raises(common.ReleaseError, match="missing"):
        promote_mod.promote(b, "v0.3.0")
    assert b.list_objects("radarx/latest.json") == {}


def test_a_tampered_or_extra_file_is_refused_and_nothing_changes(tmp_path: Path, bucket) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    promote_mod.promote(bucket, "v0.3.0")
    prepared_version(tmp_path, bucket, "0.3.1")
    before = snapshot(bucket)

    target = bucket.root / "radarx/v0.3.1" / installer_names("0.3.1")["win"]
    good = target.read_bytes()
    target.write_bytes(good[:-1] + bytes([good[-1] ^ 1]))  # one flipped bit
    with pytest.raises(common.ReleaseError, match="SHA-256"):
        promote_mod.promote(bucket, "v0.3.1")
    target.write_bytes(good)

    extra = bucket.root / "radarx/v0.3.1" / "surprise.exe"
    extra.write_bytes(b"not in the checksums")
    with pytest.raises(common.ReleaseError, match="not listed"):
        promote_mod.promote(bucket, "v0.3.1")
    extra.unlink()

    # a candidate manifest that was edited after SHA256SUMS was made
    cand = bucket.root / "radarx/v0.3.1/latest.json"
    manifest = json.loads(cand.read_text())
    manifest["notes"] = "edited"
    cand.write_bytes(common.dump_json(manifest))
    with pytest.raises(common.ReleaseError, match="SHA-256"):
        promote_mod.promote(bucket, "v0.3.1")

    # the live state is exactly what it was (the tampered upload itself is still there)
    after = snapshot(bucket)
    assert {k: v for k, v in after.items() if not k.startswith("radarx/v0.3.1/")} == {
        k: v for k, v in before.items() if not k.startswith("radarx/v0.3.1/")
    }
    assert json.loads(bucket.get("radarx/latest.json"))["version"] == "0.3.0"


def test_a_sig_file_that_differs_from_the_manifest_is_refused(tmp_path: Path, bucket) -> None:
    folder = make_download_dir(tmp_path / "dl")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    sig = folder / (installer_names("0.3.0")["win"] + ".sig")
    sig.write_text(_sig("forged"))
    # keep the checksums consistent with the forged .sig so only the cross-check can catch it
    sums = common.parse_sums((folder / "SHA256SUMS").read_text())
    sums[sig.name] = hashlib.sha256(sig.read_bytes()).hexdigest()
    (folder / "SHA256SUMS").write_text(common.format_sums(sums))
    upload(bucket, folder, "0.3.0")
    with pytest.raises(common.ReleaseError, match="differs from the manifest"):
        promote_mod.promote(bucket, "v0.3.0")


def test_a_candidate_for_another_version_or_host_is_refused(tmp_path: Path, bucket) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    with pytest.raises(common.ReleaseError, match="not under"):
        promote_mod.promote(bucket, "v0.3.0", base_url="https://elsewhere.example")
    # folder v0.3.2 holding the manifest of 0.3.0
    folder = make_download_dir(tmp_path / "dl2", "0.3.0")
    rewrite_latest.prepare_folder(folder, "v0.3.0")
    upload(bucket, folder, "0.3.2")
    with pytest.raises(common.ReleaseError, match="does not match"):
        promote_mod.promote(bucket, "v0.3.2")


def test_older_than_live_needs_rollback_and_rollback_withdraws_newer(
    tmp_path: Path, bucket
) -> None:
    for v in ("0.3.0", "0.3.1", "0.3.2"):
        prepared_version(tmp_path, bucket, v)
        promote_mod.promote(bucket, f"v{v}")
    keys_before = set(snapshot(bucket))
    assert json.loads(bucket.get("radarx/latest.json"))["version"] == "0.3.2"

    with pytest.raises(common.ReleaseError, match="older than the live"):
        promote_mod.promote(bucket, "v0.3.0")
    with pytest.raises(common.ReleaseError, match="older"):
        promote_mod.promote(bucket, "v0.3.2", rollback=True)  # not older
    with pytest.raises(common.ReleaseError, match="older"):
        promote_mod.promote(bucket, "v0.4.0", rollback=True)

    res = promote_mod.promote(bucket, "v0.3.0", rollback=True)
    assert res.withdrawn == ["0.3.1", "0.3.2"]
    live = json.loads(bucket.get("radarx/latest.json"))
    assert live["version"] == "0.3.0"
    assert bucket.get("radarx/latest.json") == bucket.get("radarx/v0.3.0/latest.json")
    index = json.loads(bucket.get("radarx/releases.json"))
    assert index["latest"] == "0.3.0"
    assert [r["version"] for r in index["releases"]] == ["0.3.0"]
    # rollback never deletes anything
    assert keys_before <= set(snapshot(bucket))
    assert json.loads(bucket.get("radarx/v0.3.2/PROMOTION.json"))["status"] == "withdrawn"
    assert bucket.list_objects("radarx/v0.3.2/")  # the installers are still there

    # going forward again is a normal promote
    promote_mod.promote(bucket, "v0.3.1")
    assert json.loads(bucket.get("radarx/latest.json"))["version"] == "0.3.1"
    index = json.loads(bucket.get("radarx/releases.json"))
    assert [r["version"] for r in index["releases"]] == ["0.3.1", "0.3.0"]


def test_rollback_with_nothing_live_is_refused(tmp_path: Path, bucket) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    with pytest.raises(common.ReleaseError, match="none is live"):
        promote_mod.promote(bucket, "v0.3.0", rollback=True)


def test_dry_run_checks_but_writes_nothing(tmp_path: Path, bucket) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    before = snapshot(bucket)
    res = promote_mod.promote(bucket, "v0.3.0", dry_run=True)
    assert res.dry_run and "radarx/latest.json" in res.wrote
    assert snapshot(bucket) == before
    (bucket.root / "radarx/v0.3.0/SHA256SUMS").unlink()
    with pytest.raises(common.ReleaseError):
        promote_mod.promote(bucket, "v0.3.0", dry_run=True)


def test_promote_cli(tmp_path: Path, bucket, capsys) -> None:
    prepared_version(tmp_path, bucket, "0.3.0")
    d = str(bucket.root)
    assert promote_mod.main(["is-promoted", "--tag", "v0.3.0", "--bucket-dir", d]) == 1
    assert promote_mod.main(["promote", "--tag", "v0.3.0", "--bucket-dir", d, "--dry-run"]) == 0
    assert "would promote" in capsys.readouterr().out
    assert promote_mod.main(["is-promoted", "--tag", "v0.3.0", "--bucket-dir", d]) == 1
    assert promote_mod.main(["promote", "--tag", "v0.3.0", "--bucket-dir", d]) == 0
    assert "promoted v0.3.0" in capsys.readouterr().out
    assert promote_mod.main(["is-promoted", "--tag", "v0.3.0", "--bucket-dir", d]) == 0
    assert promote_mod.main(["promote", "--tag", "v0.4.0", "--bucket-dir", d]) == 1
    assert "REFUSED" in capsys.readouterr().err
    assert promote_mod.main(["promote", "--tag", "nonsense", "--bucket-dir", d]) == 1


def test_scripts_run_as_programs(tmp_path: Path) -> None:
    folder = make_download_dir(tmp_path / "dl")
    run = [sys.executable, "-I"]
    r = subprocess.run(
        [*run, str(SCRIPTS / "rewrite_latest.py"), "--tag", "v0.3.0", "--dir", str(folder)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [*run, str(SCRIPTS / "build_index.py"), "--bucket-dir", str(tmp_path / "empty")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["releases"] == []


# --- the S3 / R2 implementation (the aws command line is faked) ---------------------------------


class FakeAws:
    """Answers the few `aws` calls the bucket makes, from a dict of objects."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.calls: list[list[str]] = []
        self.inputs: list[bytes | None] = []

    def run(self, cmd, **kw):
        input = kw.get("input")  # noqa: A001
        self.calls.append(cmd)
        self.inputs.append(input)
        if cmd[1] == "s3api":
            prefix = cmd[cmd.index("--prefix") + 1]
            contents = [
                {"Key": k, "Size": len(v)} for k, v in sorted(self.objects.items())
                if k.startswith(prefix)
            ]  # fmt: skip
            body = json.dumps({"Contents": contents} if contents else {}).encode()
            return subprocess.CompletedProcess(cmd, 0, body, b"")
        if (
            cmd[1:3] == ["s3", "cp"] and cmd[3] != "-" and cmd[4].startswith("s3://")
        ):  # upload a local file
            self.objects[cmd[4].split("/", 3)[3]] = Path(cmd[3]).read_bytes()
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        if cmd[1:3] == ["s3", "cp"] and cmd[3] == "-":  # upload from stdin
            key = cmd[4].split("/", 3)[3]
            self.objects[key] = input
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        key = cmd[3].split("/", 3)[3]
        return subprocess.CompletedProcess(cmd, 0, self.objects[key], b"")

    def popen(self, cmd, stdout=None, stderr=None):
        self.calls.append(cmd)
        data = self.objects[cmd[3].split("/", 3)[3]]

        class P:
            def __init__(self):
                import io

                self.stdout = io.BytesIO(data)

            def wait(self):
                return 0

        return P()


def test_s3_bucket_builds_endpoint_commands_without_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value")
    fake = FakeAws({"radarx/v0.3.0/a.bin": b"abc", "radarx/other": b"x"})
    b = common.S3Bucket(
        "my-bucket", "https://ACCT.r2.cloudflarestorage.com", runner=fake.run, popen=fake.popen
    )
    assert b.list_objects("radarx/v0.3.0/") == {"radarx/v0.3.0/a.bin": 3}
    assert b.get("radarx/v0.3.0/a.bin") == b"abc"
    assert b.sha256("radarx/v0.3.0/a.bin") == hashlib.sha256(b"abc").hexdigest()
    b.put("radarx/latest.json", b"{}", cache_control="no-cache", content_type="application/json")
    assert fake.objects["radarx/latest.json"] == b"{}"
    f = tmp_path / "x.bin"
    f.write_bytes(b"file body")
    b.put_file("radarx/v0.3.0/x.bin", f, cache_control="immutable", content_type="x/y")
    assert fake.objects["radarx/v0.3.0/x.bin"] == b"file body"
    up = next(c for c in fake.calls if c[1:3] == ["s3", "cp"] and c[3] == "-")
    assert up[up.index("--cache-control") + 1] == "no-cache"
    assert up[up.index("--content-type") + 1] == "application/json"
    for call in fake.calls:
        assert call[0] == "aws"
        assert call[call.index("--endpoint-url") + 1] == "https://ACCT.r2.cloudflarestorage.com"
        assert "super-secret-value" not in " ".join(call)
    with pytest.raises(common.BucketMissingError):
        b.get("radarx/nothing")
    with pytest.raises(common.BucketMissingError):
        b.sha256("radarx/nothing")


def test_s3_failure_message_has_no_credentials() -> None:
    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 255, b"", b"An error occurred (AccessDenied)")

    b = common.S3Bucket("b", "https://x.r2.cloudflarestorage.com", runner=failing)
    with pytest.raises(common.ReleaseError, match="AccessDenied"):
        b.list_objects("radarx/")


def test_s3_bucket_from_env_needs_account_and_bucket() -> None:
    with pytest.raises(common.ReleaseError):
        common.S3Bucket.from_env({})
    b = common.S3Bucket.from_env({"R2_ACCOUNT_ID": "abc123", "R2_BUCKET": "dl"})
    assert b.endpoint_url == "https://abc123.r2.cloudflarestorage.com" and b.bucket == "dl"


def test_promote_works_against_the_s3_implementation(tmp_path: Path) -> None:
    """The same promote, with the bucket behind the (faked) aws command line."""
    local = common.LocalBucket(tmp_path / "src")
    prepared_version(tmp_path, local, "0.3.0")
    fake = FakeAws(snapshot(local))
    b = common.S3Bucket("bk", "https://ACCT.r2.cloudflarestorage.com", runner=fake.run,
                        popen=fake.popen)  # fmt: skip
    promote_mod.promote(b, "v0.3.0")
    assert fake.objects["radarx/latest.json"] == fake.objects["radarx/v0.3.0/latest.json"]
    assert json.loads(fake.objects["radarx/releases.json"])["latest"] == "0.3.0"
