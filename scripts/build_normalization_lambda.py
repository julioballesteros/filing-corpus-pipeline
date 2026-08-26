"""Build the reproducible arm64 Lambda ZIP for SEC HTML normalization."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
LOCKFILE = ROOT / "uv.lock"
DEFAULT_OUTPUT = ROOT / "build" / "lambda" / "normalization-lambda.zip"
PACKAGE_ROOT = Path("filing_corpus_pipeline")
SOURCE_PATHS = (
    PACKAGE_ROOT / "__init__.py",
    PACKAGE_ROOT / "domain",
    PACKAGE_ROOT / "entrypoints" / "__init__.py",
    PACKAGE_ROOT / "entrypoints" / "errors.py",
    PACKAGE_ROOT / "entrypoints" / "normalization_lambda.py",
    PACKAGE_ROOT / "normalization",
    PACKAGE_ROOT / "registry",
    PACKAGE_ROOT / "storage",
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="normalization-lambda-") as temporary:
        staging = Path(temporary)
        _install_lxml(staging, version=_locked_version("lxml"))
        _copy_application(staging)
        _write_zip(staging, output)
    _verify_zip(output)
    print(f"Built {output} ({output.stat().st_size} bytes)")


def _locked_version(package_name: str) -> str:
    lock = tomllib.loads(LOCKFILE.read_text())
    for package in lock["package"]:
        if package["name"] == package_name:
            return str(package["version"])
    raise RuntimeError(f"{package_name!r} is missing from {LOCKFILE}")


def _install_lxml(staging: Path, *, version: str) -> None:
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(staging),
            "--python-version",
            "3.13",
            "--python-platform",
            "aarch64-manylinux2014",
            "--only-binary",
            ":all:",
            "--no-deps",
            f"lxml=={version}",
        ],
        cwd=ROOT,
        check=True,
    )


def _copy_application(staging: Path) -> None:
    for relative in SOURCE_PATHS:
        source = SOURCE_ROOT / relative
        destination = staging / relative
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _write_zip(staging: Path, output: Path) -> None:
    temporary_output = output.with_suffix(".zip.tmp")
    with zipfile.ZipFile(
        temporary_output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(staging.rglob("*")):
            if not path.is_file() or path.suffix == ".pyc":
                continue
            relative = path.relative_to(staging).as_posix()
            info = zipfile.ZipInfo(relative, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    temporary_output.replace(output)


def _verify_zip(output: Path) -> None:
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    required = {
        "filing_corpus_pipeline/entrypoints/normalization_lambda.py",
        "filing_corpus_pipeline/normalization/sec_html.py",
    }
    missing = required - names
    if missing:
        raise RuntimeError(
            f"Lambda ZIP is missing application files: {sorted(missing)}"
        )
    if not any(
        name.startswith("lxml/etree.cpython-313-") and "aarch64-linux-gnu.so" in name
        for name in names
    ):
        raise RuntimeError(
            "Lambda ZIP is missing the CPython 3.13 arm64 lxml extension"
        )


if __name__ == "__main__":
    main()
