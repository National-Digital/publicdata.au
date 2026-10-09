import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pytest

from publicdata import extension, r2
from publicdata.store import sha256_file

from .conftest import ROOT, present


def test_the_spatial_extension_pin_is_for_the_pinned_duckdb() -> None:
    pin = json.loads(extension.PIN.read_text())
    reqs = (ROOT / "pipeline" / "requirements.txt").read_text().splitlines()
    assert f"duckdb=={pin['duckdb']}" in reqs, "raise duckdb in spatial-extension.json too"
    path = f"/v{pin['duckdb']}/{pin['platform']}/"
    assert path in pin["upstream"], "run the Spatial extension workflow for the new DuckDB"
    assert pin["url"].endswith(f"{path}{pin['build']}/spatial.duckdb_extension.gz")


def _extension(
    path: Path, duckdb: str = "1.5.6", platform: str = "linux_amd64", build: str = "04270fe"
) -> Path:
    fields = ["4", platform, f"v{duckdb}", build, "CPP", "", "", ""]
    meta = b"".join(f.encode().ljust(32, b"\0") for f in reversed(fields))
    with gzip.open(path, "wb") as f:
        f.write(b"\x7fELF" + bytes(64) + meta + bytes(256))
    return path


def _pin_for(gz: Path, **over: str) -> dict[str, str]:
    return {
        "duckdb": duckdb.__version__,
        "platform": "linux_amd64",
        "build": "04270fe",
        "url": "r2://publicdata-raw/_toolchain/x/spatial.duckdb_extension.gz",
        "upstream": f"https://extensions.example/v{duckdb.__version__}/linux_amd64/spatial.duckdb_extension.gz",
        "sha256": sha256_file(gz),
    } | over


def test_the_installed_extension_is_the_pinned_build() -> None:
    pin = json.loads(extension.PIN.read_text())
    (path,) = present(
        duckdb.connect()
        .execute("SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'spatial'")
        .fetchone()
    )
    assert extension.footer(Path(path)) == {k: pin[k] for k in ("platform", "duckdb", "build")}


@dataclass
class Fetch:
    """What install() gave its download and DuckDB's install, and the bytes the download serves."""

    urls: list[str] = field(default_factory=list)
    installed: list[tuple[str, bool]] = field(default_factory=list)
    gz: Path | None = None


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch) -> Fetch:
    """install() with its download and DuckDB's install replaced."""
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(k, raising=False)
    seen = Fetch()

    def download(url: str, dest: Path) -> None:
        seen.urls.append(url)
        dest.write_bytes(present(seen.gz).read_bytes())

    class Con:
        def install_extension(self, path: str, *, force_install: bool = False) -> None:
            seen.installed.append((Path(path).name, force_install))

    def connect(*_a: object, **_k: object) -> Con:
        return Con()

    monkeypatch.setattr(extension, "_download", download)
    monkeypatch.setattr(duckdb, "connect", connect)
    return seen


def test_install_takes_the_pinned_bytes_from_duckdb_without_r2_credentials(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetch.gz = _extension(tmp_path / "x.gz")
    pin = _pin_for(fetch.gz)
    monkeypatch.setattr(extension, "_pin", lambda: pin)
    extension.install()
    assert fetch.urls == [pin["upstream"]]
    assert fetch.installed == [("spatial.duckdb_extension", True)]


def test_install_takes_our_copy_with_r2_credentials(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gz = fetch.gz = _extension(tmp_path / "x.gz")
    pin = _pin_for(gz)
    monkeypatch.setattr(extension, "_pin", lambda: pin)
    for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.setenv(k, "x")
    got: list[tuple[str, str]] = []

    class S3:
        def download_file(self, bucket: str, key: str, dest: str) -> None:
            got.append((bucket, key))
            Path(dest).write_bytes(gz.read_bytes())

    monkeypatch.setattr(r2, "client", S3)
    extension.install()
    assert got == [("publicdata-raw", "_toolchain/x/spatial.duckdb_extension.gz")]
    assert fetch.urls == []
    assert len(fetch.installed) == 1


def test_install_refuses_bytes_that_are_not_the_pinned_build(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gz = fetch.gz = _extension(tmp_path / "x.gz")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(gz, sha256="0" * 64))
    with pytest.raises(extension.ExtensionError, match="SHA-256"):
        extension.install()
    assert fetch.installed == []


def test_install_refuses_an_extension_for_another_duckdb(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gz = fetch.gz = _extension(tmp_path / "x.gz", duckdb="1.4.0")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(gz))
    with pytest.raises(extension.ExtensionError, match=r"1\.4\.0"):
        extension.install()
    assert fetch.installed == []


def test_install_refuses_a_pin_for_another_duckdb_before_fetching(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gz = fetch.gz = _extension(tmp_path / "x.gz")
    monkeypatch.setattr(extension, "_pin", lambda: _pin_for(gz, duckdb="0.0.1"))
    with pytest.raises(extension.ExtensionError, match="Spatial extension workflow"):
        extension.install()
    assert fetch.urls == []


def test_install_sends_a_half_raised_pin_to_the_workflow(
    fetch: Fetch, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fetch.gz = _extension(tmp_path / "x.gz")
    pin = _pin_for(fetch.gz, upstream="https://extensions.example/v0.0.1/linux_amd64/x.gz")
    monkeypatch.setattr(extension, "_pin", lambda: pin)
    with pytest.raises(extension.ExtensionError, match="Spatial extension workflow"):
        extension.install()
    assert fetch.urls == []
