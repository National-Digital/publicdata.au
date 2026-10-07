import json
from pathlib import Path

import pytest

from publicdata import extension
from publicdata.store import sha256_file

ROOT = Path(__file__).resolve().parents[2]


def test_the_spatial_extension_pin_is_for_the_pinned_duckdb():
    pin = json.loads(extension.PIN.read_text())
    reqs = (ROOT / "pipeline" / "requirements.txt").read_text().splitlines()
    assert f"duckdb=={pin['duckdb']}" in reqs, "raise duckdb in spatial-extension.json too"
    path = f"/v{pin['duckdb']}/{pin['platform']}/"
    assert path in pin["upstream"], "run the Spatial extension workflow for the new DuckDB"
    assert pin["url"].endswith(f"{path}{pin['build']}/spatial.duckdb_extension.gz")


def _extension(path, duckdb="1.5.6", platform="linux_amd64", build="04270fe"):
    import gzip

    fields = ["4", platform, f"v{duckdb}", build, "CPP", "", "", ""]
    meta = b"".join(f.encode().ljust(32, b"\0") for f in reversed(fields))
    with gzip.open(path, "wb") as f:
        f.write(b"\x7fELF" + bytes(64) + meta + bytes(256))
    return path


def _pin_for(gz, **over):
    import duckdb

    return {
        "duckdb": duckdb.__version__,
        "platform": "linux_amd64",
        "build": "04270fe",
        "url": "r2://publicdata-raw/_toolchain/x/spatial.duckdb_extension.gz",
        "upstream": "https://extensions.example/spatial.duckdb_extension.gz",
        "sha256": sha256_file(gz),
    } | over


def test_the_installed_extension_is_the_pinned_build():
    import duckdb

    pin = json.loads(extension.PIN.read_text())
    (path,) = (
        duckdb.connect()
        .execute("SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'spatial'")
        .fetchone()
    )
    assert extension.footer(Path(path)) == {k: pin[k] for k in ("platform", "duckdb", "build")}


@pytest.fixture
def fetch(monkeypatch, tmp_path):
    """install() with its download and DuckDB's install replaced: returns what each was given."""
    import duckdb

    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(k, raising=False)
    seen = {"urls": [], "installed": []}
    src = {"gz": None}

    def download(url, dest):
        seen["urls"].append(url)
        dest.write_bytes(src["gz"].read_bytes())

    class Con:
        def install_extension(self, path, force_install=False):
            seen["installed"].append((Path(path).name, force_install))

    monkeypatch.setattr(extension, "_download", download)
    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: Con())
    return seen, src


def test_install_takes_the_pinned_bytes_from_duckdb_without_r2_credentials(
    fetch, monkeypatch, tmp_path
):
    seen, src = fetch
    src["gz"] = _extension(tmp_path / "x.gz")
    pin = _pin_for(src["gz"])
    monkeypatch.setattr(extension, "_pin", lambda: pin)
    extension.install()
    assert seen["urls"] == [pin["upstream"]]
    assert seen["installed"] == [("spatial.duckdb_extension", True)]


def test_install_takes_our_copy_with_r2_credentials(fetch, monkeypatch, tmp_path):
    from publicdata import r2

    seen, src = fetch
    src["gz"] = _extension(tmp_path / "x.gz")
    pin = _pin_for(src["gz"])
    monkeypatch.setattr(extension, "_pin", lambda: pin)
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.setenv(k, "x")
    got = []

    class S3:
        def download_file(self, bucket, key, dest):
            got.append((bucket, key))
            Path(dest).write_bytes(src["gz"].read_bytes())

    monkeypatch.setattr(r2, "client", lambda: S3())
    extension.install()
    assert got == [("publicdata-raw", "_toolchain/x/spatial.duckdb_extension.gz")]
    assert seen["urls"] == [] and len(seen["installed"]) == 1


def test_install_refuses_bytes_that_are_not_the_pinned_build(fetch, monkeypatch, tmp_path):
    seen, src = fetch
    src["gz"] = _extension(tmp_path / "x.gz")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(src["gz"], sha256="0" * 64))
    with pytest.raises(extension.ExtensionError, match="SHA-256"):
        extension.install()
    assert seen["installed"] == []


def test_install_refuses_an_extension_for_another_duckdb(fetch, monkeypatch, tmp_path):
    seen, src = fetch
    src["gz"] = _extension(tmp_path / "x.gz", duckdb="1.4.0")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(src["gz"]))
    with pytest.raises(extension.ExtensionError, match="1.4.0"):
        extension.install()
    assert seen["installed"] == []


def test_install_refuses_a_pin_for_another_duckdb_before_fetching(fetch, monkeypatch, tmp_path):
    seen, src = fetch
    src["gz"] = _extension(tmp_path / "x.gz")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(src["gz"], duckdb="0.0.1"))
    with pytest.raises(extension.ExtensionError, match="Spatial extension workflow"):
        extension.install()
    assert seen["urls"] == []
