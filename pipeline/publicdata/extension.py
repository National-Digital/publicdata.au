"""DuckDB's spatial extension, installed from the build pinned in spatial-extension.json.

The build code does not import this module: it runs once per runner, before a build, and the
versions are keyed on the installed extension's own version (cache.spatial_version).
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .records import one_row
from .store import sha256_file

PIN = Path(__file__).with_name("spatial-extension.json")
BUCKET = "publicdata-raw"
REPOSITORY = "https://extensions.duckdb.org"


class ExtensionError(RuntimeError):
    pass


def _pin(path: Path = PIN) -> dict[str, str]:
    pin: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return pin


def footer(path: Path) -> dict[str, str]:
    """The build, DuckDB release and platform a DuckDB extension file declares in its footer.

    DuckDB itself reads (and signs) the footer before it loads an extension.
    """
    with path.open("rb") as f:
        f.seek(-512, os.SEEK_END)
        meta = f.read(256)
    fields = [meta[i : i + 32].rstrip(b"\0").decode() for i in range(0, 256, 32)][::-1]
    return {"platform": fields[1], "duckdb": fields[2].removeprefix("v"), "build": fields[3]}


def _gunzip(src: Path, dest: Path) -> Path:
    with gzip.open(src, "rb") as f, dest.open("wb") as out:
        shutil.copyfileobj(f, out, 1 << 20)
    return dest


def _download(url: str, dest: Path) -> None:
    import requests  # noqa: PLC0415 - imported when the command runs

    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def _r2_credentials() -> bool:
    return all(
        os.environ.get(k)
        for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_ID", "CLOUDFLARE_ACCOUNT_ID")
    )


def install() -> None:
    """Install the spatial extension build pinned in spatial-extension.json.

    It is checked against its SHA-256 and DuckDB release, once, so that a build only loads it and
    stays offline. A runner with the R2 credentials takes our copy; anyone else takes the same
    bytes from DuckDB.
    """
    import duckdb  # noqa: PLC0415 - imported when the command runs

    pin = _pin()
    if pin["duckdb"] != duckdb.__version__ or f"/v{pin['duckdb']}/" not in pin["upstream"]:
        msg = (
            f"{PIN.name} pins the spatial extension for DuckDB {pin['duckdb']}, and "
            f"DuckDB {duckdb.__version__} is installed; run the Spatial extension workflow"
        )
        raise ExtensionError(msg)
    with tempfile.TemporaryDirectory() as tmp:
        gz = Path(tmp) / "spatial.duckdb_extension.gz"
        if _r2_credentials():
            from .r2 import client  # noqa: PLC0415 - the deploy extra

            bucket, key = pin["url"].removeprefix("r2://").split("/", 1)
            client().download_file(bucket, key, str(gz))
            print(f"spine: fetched {pin['url']}")
        else:
            _download(pin["upstream"], gz)
            print(f"spine: fetched {pin['upstream']}")
        got = sha256_file(gz)
        if got != pin["sha256"]:
            msg = (
                f"the spatial extension fetched has SHA-256 {got}, and {PIN.name} pins "
                f"{pin['sha256']}"
            )
            raise ExtensionError(msg)
        ext = _gunzip(gz, Path(tmp) / "spatial.duckdb_extension")
        meta = footer(ext)
        want = {k: pin[k] for k in meta}
        if meta != want:
            msg = f"the spatial extension fetched is {meta}, not {want}"
            raise ExtensionError(msg)
        duckdb.connect().install_extension(str(ext), force_install=True)


def mirror(pin_path: Path = PIN) -> dict[str, str]:
    """Copy the spatial extension DuckDB serves for the installed release to R2 and pin it.

    The download is first checked byte for byte against what DuckDB's own INSTALL fetches and
    loads.
    """
    import duckdb  # noqa: PLC0415 - imported when the command runs
    from botocore.exceptions import ClientError  # noqa: PLC0415 - the deploy extra

    from .r2 import client  # noqa: PLC0415 - the deploy extra

    con = duckdb.connect()
    platform: str = one_row(con.execute("PRAGMA platform"))[0]
    release = duckdb.__version__
    upstream = f"{REPOSITORY}/v{release}/{platform}/spatial.duckdb_extension.gz"
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        gz = t / "spatial.duckdb_extension.gz"
        _download(upstream, gz)
        ext = _gunzip(gz, t / "spatial.duckdb_extension")
        own = duckdb.connect(config={"extension_directory": str(t / "duckdb")})
        own.install_extension("spatial")
        own.load_extension("spatial")
        (installed,) = one_row(
            own.execute(
                "SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'spatial'"
            )
        )
        if sha256_file(ext) != sha256_file(Path(installed)):
            msg = f"{upstream} is not the build DuckDB's own INSTALL fetched"
            raise ExtensionError(msg)
        meta = footer(ext)
        if (meta["duckdb"], meta["platform"]) != (release, platform) or not re.fullmatch(
            r"[0-9a-f]{7,40}", meta["build"]
        ):
            msg = f"{upstream} declares {meta}"
            raise ExtensionError(msg)
        sha = sha256_file(gz)
        key = f"_toolchain/duckdb/v{release}/{platform}/{meta['build']}/spatial.duckdb_extension.gz"
        s3 = client()
        try:
            held = s3.head_object(Bucket=BUCKET, Key=key)["Metadata"].get("sha256", "")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey"):
                raise
            held = None
        if held is None:
            s3.upload_file(
                str(gz),
                BUCKET,
                key,
                ExtraArgs={"ContentType": "application/gzip", "Metadata": {"sha256": sha}},
            )
            print(f"spine: put {BUCKET}/{key}")
        elif held != sha:
            msg = f"{BUCKET}/{key} holds other bytes ({held}); not replaced"
            raise ExtensionError(msg)
    pin = {
        "duckdb": release,
        "platform": platform,
        "build": meta["build"],
        "url": f"r2://{BUCKET}/{key}",
        "upstream": upstream,
        "sha256": sha,
    }
    pin_path.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    return pin
