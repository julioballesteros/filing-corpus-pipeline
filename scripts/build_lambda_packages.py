"""Build reproducible arm64 deployment ZIPs for every pipeline Lambda."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
LOCKFILE = ROOT / "uv.lock"
DEFAULT_OUTPUT_DIRECTORY = ROOT / "build" / "lambda"
PACKAGE_ROOT = Path("filing_corpus_pipeline")
COMMON_SOURCE_PATHS = (
    PACKAGE_ROOT / "__init__.py",
    PACKAGE_ROOT / "models.py",
    PACKAGE_ROOT / "domain",
    PACKAGE_ROOT / "runtime",
)
RUNTIME_DEPENDENCIES = ("pydantic",)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class LambdaPackage:
    """Build-time definition for one independently deployable Lambda."""

    name: str
    source_paths: tuple[Path, ...]
    dependencies: tuple[str, ...] = RUNTIME_DEPENDENCIES

    @property
    def handler_path(self) -> str:
        return f"filing_corpus_pipeline/{self.name}/handler.py"


PACKAGES = {
    package.name: package
    for package in (
        LambdaPackage(
            name="discovery",
            source_paths=(
                *COMMON_SOURCE_PATHS,
                PACKAGE_ROOT / "discovery",
                PACKAGE_ROOT / "sources",
                PACKAGE_ROOT / "storage" / "__init__.py",
                PACKAGE_ROOT / "storage" / "s3.py",
            ),
        ),
        LambdaPackage(
            name="acquisition",
            source_paths=(
                *COMMON_SOURCE_PATHS,
                PACKAGE_ROOT / "acquisition",
                PACKAGE_ROOT / "registry",
                PACKAGE_ROOT / "sources",
                PACKAGE_ROOT / "storage" / "__init__.py",
                PACKAGE_ROOT / "storage" / "dynamodb.py",
                PACKAGE_ROOT / "storage" / "raw_documents.py",
                PACKAGE_ROOT / "storage" / "s3.py",
            ),
        ),
        LambdaPackage(
            name="normalization",
            source_paths=(
                *COMMON_SOURCE_PATHS,
                PACKAGE_ROOT / "normalization",
                PACKAGE_ROOT / "registry",
                PACKAGE_ROOT / "storage",
            ),
            dependencies=(*RUNTIME_DEPENDENCIES, "lxml"),
        ),
    )
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--function",
        action="append",
        choices=tuple(PACKAGES),
        dest="functions",
        help="Build only this function; may be supplied more than once.",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    args = parser.parse_args()
    output_directory = args.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)

    names = args.functions or list(PACKAGES)
    for name in names:
        package = PACKAGES[name]
        output = output_directory / f"{name}-lambda.zip"
        _build(package, output)
        print(f"Built {output} ({output.stat().st_size} bytes)")


def _build(package: LambdaPackage, output: Path) -> None:
    with tempfile.TemporaryDirectory(prefix=f"{package.name}-lambda-") as temporary:
        staging = Path(temporary)
        _install_dependencies(staging, package.dependencies)
        _copy_application(staging, package.source_paths)
        _write_zip(staging, output)
    _verify_zip(output, package)


def _locked_requirements(root_names: tuple[str, ...]) -> list[str]:
    """Resolve the complete, exactly pinned runtime closure from ``uv.lock``."""
    lock = tomllib.loads(LOCKFILE.read_text())
    packages = {package["name"]: package for package in lock["package"]}
    selected: set[str] = set()
    pending = list(root_names)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        package = packages.get(name)
        if package is None:
            raise RuntimeError(f"{name!r} is missing from {LOCKFILE}")
        selected.add(name)
        pending.extend(
            dependency["name"]
            for dependency in package.get("dependencies", ())
            if "name" in dependency
        )
    return [f"{name}=={packages[name]['version']}" for name in sorted(selected)]


def _install_dependencies(staging: Path, dependencies: tuple[str, ...]) -> None:
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
            *_locked_requirements(dependencies),
        ],
        cwd=ROOT,
        check=True,
    )


def _copy_application(staging: Path, source_paths: tuple[Path, ...]) -> None:
    for relative in source_paths:
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


def _verify_zip(output: Path, package: LambdaPackage) -> None:
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
    required = {
        package.handler_path,
        "filing_corpus_pipeline/models.py",
        "pydantic/__init__.py",
    }
    missing = required - names
    if missing:
        raise RuntimeError(f"Lambda ZIP is missing files: {sorted(missing)}")
    legacy_modules = {
        name
        for name in names
        if name.startswith("filing_corpus_pipeline/adapters/")
        or name.startswith("filing_corpus_pipeline/entrypoints/")
    }
    if legacy_modules:
        raise RuntimeError(
            f"Lambda ZIP contains obsolete architecture modules: "
            f"{sorted(legacy_modules)}"
        )
    if not any(
        name.startswith("pydantic_core/_pydantic_core.cpython-313-")
        and "aarch64-linux-gnu.so" in name
        for name in names
    ):
        raise RuntimeError(
            "Lambda ZIP is missing the CPython 3.13 arm64 pydantic-core extension"
        )
    has_lxml = any(
        name.startswith("lxml/etree.cpython-313-") and "aarch64-linux-gnu.so" in name
        for name in names
    )
    if package.name == "normalization" and not has_lxml:
        raise RuntimeError(
            "Normalization ZIP is missing the CPython 3.13 arm64 lxml extension"
        )
    if package.name != "normalization" and has_lxml:
        raise RuntimeError(f"{package.name} ZIP unexpectedly contains lxml")


if __name__ == "__main__":
    main()
