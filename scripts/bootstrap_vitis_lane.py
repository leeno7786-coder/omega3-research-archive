"""Build the Vitis lane interpreter from local assets: embed base + its venv.

``onnxruntime-vitisai`` is published for CPython 3.12 only, so the NPU provider
child (``runtime.vitis_worker``) runs on its own portable stack, laid out
exactly like the brain's:

    python-3.12-embed/   the portable base (its ._pth never imports site)
    .venv-vitis/         its package environment
        pyvenv.cfg       home = ..\\python-3.12-embed
        Scripts/         python.exe + runtime DLLs, python312._pth binding
                         ..\\Lib\\site-packages and ..\\.. (the product root)
        Lib/site-packages
        omega-vitis-lane.json   what was installed, from which assets

Run it with the tooling interpreter (AGENTS.md section 1):

    .\\.venv-brain\\Scripts\\python.exe -m scripts.bootstrap_vitis_lane --assets <dir>

``<dir>`` holds exactly one ``python-3.12.*-embed-amd64.zip`` and the wheels
under ``cp312-deps/``.  Nothing is downloaded and pip is not used: each wheel
is checked against its own RECORD and unpacked, and the dependency closure is
resolved from the wheels' metadata for CPython 3.12 on win_amd64.  A missing,
incompatible, or tampered wheel fails the whole build and leaves the existing
lane untouched.  ``check_lane`` is the relocation check the build ends with:
every path the lane records is relative, so the tree moves with the SSD.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from core.paths import OMEGA_ROOT

EMBED_DIR = "python-3.12-embed"
VENV_DIR = ".venv-vitis"
LOCK_NAME = "omega-vitis-lane.json"
ROOT_REQUIREMENTS = ("onnxruntime-vitisai", "numpy")
VENV_PTH = ("python312.zip", ".", r"..\Lib\site-packages", r"..\..", "import site")
EMBED_PTH = ("python312.zip", ".")
_EMBED_NAME = re.compile(r"^python-(\d+\.\d+\.\d+)-embed-amd64\.zip$")
_HOST_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|(?<![A-Za-z0-9_/:])/(home|Users|root|tmp)/")
_SCRIPTS_SKIP = {"pythonw.exe", "python312._pth", "license.txt", "python.cat"}

# The marker environment of the interpreter being built, not of this process.
_TARGET_ENV = {
    "implementation_name": "cpython",
    "implementation_version": "3.12.10",
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_python_implementation": "CPython",
    "platform_release": "10",
    "platform_system": "Windows",
    "platform_version": "10.0",
    "python_full_version": "3.12.10",
    "python_version": "3.12",
    "sys_platform": "win32",
    "extra": "",
}


class BootstrapError(RuntimeError):
    """The lane could not be built from these assets; nothing was replaced."""


class VitisLaneInvalid(RuntimeError):
    """A lane on disk is incomplete or bound to a host path."""


@dataclass
class _Wheel:
    path: Path
    name: str
    version: str
    sha256: str
    requires: list[Requirement] = field(default_factory=list)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_lines(path: Path, lines: tuple[str, ...] | list[str]) -> None:
    # Bytes, so Windows newline translation cannot rewrite what the lane reads.
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _supported(tags: Any) -> bool:
    for tag in tags:
        if tag.platform not in {"win_amd64", "any"}:
            continue
        if tag.interpreter in {"cp312", "py3", "py312"} and tag.abi in {"cp312", "none"}:
            return True
        if tag.abi == "abi3" and re.fullmatch(r"cp3(\d+)", tag.interpreter):
            if int(tag.interpreter[3:]) <= 12:
                return True
    return False


def _read_wheel(path: Path) -> _Wheel:
    name, version, _build, tags = parse_wheel_filename(path.name)
    if not _supported(tags):
        raise BootstrapError(f"{path.name} is not installable on CPython 3.12 win_amd64")
    with zipfile.ZipFile(path) as archive:
        metadata = next(
            (item for item in archive.namelist()
             if item.count("/") == 1 and item.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata is None:
            raise BootstrapError(f"{path.name} carries no dist-info METADATA")
        lines = archive.read(metadata).decode("utf-8").splitlines()
    requires = [
        Requirement(line.split(":", 1)[1].strip())
        for line in lines
        if line.startswith("Requires-Dist:")
    ]
    return _Wheel(path, canonicalize_name(name), str(version), _sha256(path), requires)


def _find_assets(assets: Path) -> tuple[Path, str, list[_Wheel]]:
    embeds = sorted(item for item in assets.glob("python-*-embed-amd64.zip") if item.is_file())
    if len(embeds) != 1:
        raise BootstrapError(
            f"expected exactly one python-*-embed-amd64.zip in {assets}, found {len(embeds)}"
        )
    embed = embeds[0]
    match = _EMBED_NAME.match(embed.name)
    if match is None or not match.group(1).startswith("3.12."):
        raise BootstrapError(f"{embed.name} is not a CPython 3.12 embeddable distribution")
    wheel_dir = assets / "cp312-deps"
    if not wheel_dir.is_dir():
        raise BootstrapError(f"wheel directory is missing: {wheel_dir}")
    wheels = [_read_wheel(item) for item in sorted(wheel_dir.glob("*.whl"))]
    return embed, match.group(1), wheels


def _resolve(wheels: list[_Wheel]) -> list[_Wheel]:
    by_name: dict[str, _Wheel] = {}
    for wheel in wheels:
        if wheel.name in by_name:
            raise BootstrapError(f"more than one wheel provides {wheel.name}")
        by_name[wheel.name] = wheel
    chosen: dict[str, _Wheel] = {}
    pending = [(Requirement(item), "the Vitis lane") for item in ROOT_REQUIREMENTS]
    while pending:
        requirement, parent = pending.pop(0)
        if requirement.marker is not None and not Marker(str(requirement.marker)).evaluate(
            dict(_TARGET_ENV)
        ):
            continue
        name = canonicalize_name(requirement.name)
        wheel = by_name.get(name)
        if wheel is None:
            raise BootstrapError(f"no wheel satisfies {requirement} (required by {parent})")
        if not requirement.specifier.contains(wheel.version, prereleases=True):
            raise BootstrapError(
                f"{wheel.path.name} does not satisfy {requirement} (required by {parent})"
            )
        if name in chosen:
            continue
        chosen[name] = wheel
        pending.extend((item, wheel.name) for item in wheel.requires)
    return [chosen[name] for name in sorted(chosen)]


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _contained(base: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise BootstrapError(f"unsafe archive member path: {relative}")
    return base.joinpath(*parts)


def _install_wheel(wheel: _Wheel, venv: Path, site: Path) -> None:
    with zipfile.ZipFile(wheel.path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        record_name = next(
            (item.filename for item in members
             if item.filename.count("/") == 1 and item.filename.endswith(".dist-info/RECORD")),
            None,
        )
        if record_name is None:
            raise BootstrapError(f"{wheel.path.name} carries no RECORD")
        hashes: dict[str, str] = {}
        for row in archive.read(record_name).decode("utf-8").splitlines():
            if row.strip():
                path, digest, *_ = row.rsplit(",", 2)
                hashes[path.strip('"')] = digest
        for member in members:
            data = archive.read(member)
            if member.filename != record_name:
                expected = hashes.get(member.filename, "")
                if not expected or expected != _record_hash(data):
                    raise BootstrapError(
                        f"{wheel.path.name}: {member.filename} does not match its RECORD"
                    )
            top, _, rest = member.filename.partition("/")
            if top.endswith(".data") and rest:
                scheme, _, inner = rest.partition("/")
                if scheme in {"purelib", "platlib"}:
                    target = _contained(site, inner)
                elif scheme == "data":
                    site_prefix = "lib/site-packages/"
                    if inner.casefold().startswith(site_prefix):
                        target = _contained(site, inner[len(site_prefix):])
                    else:
                        target = _contained(venv, inner)
                else:
                    continue  # scripts/headers: the product venv ships no entry points
            else:
                target = _contained(site, member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _build_embed(embed_zip: Path, target: Path) -> None:
    with zipfile.ZipFile(embed_zip) as archive:
        names = {item.casefold() for item in archive.namelist()}
        if "python312.dll" not in names:
            raise BootstrapError(f"{embed_zip.name} does not carry python312.dll (CPython 3.12)")
        target.mkdir(parents=True)
        for member in archive.infolist():
            if member.is_dir():
                continue
            destination = _contained(target, member.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))
    # The embed is a base, never a runtime: it must not reach any site-packages.
    _write_lines(target / "python312._pth", EMBED_PTH)


def _build_venv(embed: Path, venv: Path, python_version: str) -> Path:
    scripts = venv / "Scripts"
    site = venv / "Lib" / "site-packages"
    scripts.mkdir(parents=True)
    site.mkdir(parents=True)
    for item in sorted(embed.iterdir()):
        if item.is_file() and item.name.casefold() not in _SCRIPTS_SKIP and (
            item.name.casefold() == "python.exe"
            or item.suffix.casefold() in {".dll", ".pyd", ".zip"}
        ):
            shutil.copy2(item, scripts / item.name)
    _write_lines(scripts / "python312._pth", VENV_PTH)
    _write_lines(
        venv / "pyvenv.cfg",
        [
            rf"home = ..\{EMBED_DIR}",
            "implementation = CPython",
            f"version_info = {python_version}",
            "include-system-site-packages = false",
            "omega-portable = true",
            "omega-lane = vitis",
        ],
    )
    return site


def bootstrap(assets: Path, omega_root: Path = OMEGA_ROOT) -> dict[str, Any]:
    """Build the lane in a staging directory, check it, then swap it into place."""
    assets = Path(assets)
    omega_root = Path(omega_root)
    embed_zip, python_version, wheels = _find_assets(assets)
    chosen = _resolve(wheels)

    staging = omega_root / f".vitis-lane-staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        embed = staging / EMBED_DIR
        venv = staging / VENV_DIR
        _build_embed(embed_zip, embed)
        site = _build_venv(embed, venv, python_version)
        for wheel in chosen:
            _install_wheel(wheel, venv, site)
        lock = {
            "schema": 1,
            "lane": "vitis",
            "python": python_version,
            "embed": {"file": embed_zip.name, "sha256": _sha256(embed_zip)},
            "distributions": [
                {"name": item.name, "version": item.version,
                 "wheel": item.path.name, "sha256": item.sha256}
                for item in chosen
            ],
        }
        (venv / LOCK_NAME).write_bytes(
            (json.dumps(lock, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )
        check_lane(staging)
        for name in (EMBED_DIR, VENV_DIR):
            final = omega_root / name
            retired = omega_root / f".{name}.retired-{os.getpid()}"
            if final.exists():
                final.rename(retired)
            (staging / name).rename(final)
            if retired.exists():
                shutil.rmtree(retired)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    receipt = check_lane(omega_root)
    receipt["distributions"] = len(chosen)
    return receipt


def check_lane(omega_root: Path = OMEGA_ROOT) -> dict[str, Any]:
    """Static relocation check: complete, and bound only by relative paths."""
    root = Path(omega_root)
    embed = root / EMBED_DIR
    venv = root / VENV_DIR
    for path in (
        embed / "python.exe", embed / "python312.dll", embed / "python312._pth",
        venv / "Scripts" / "python.exe", venv / "Scripts" / "python312.dll",
        venv / "Scripts" / "python312._pth", venv / "pyvenv.cfg", venv / LOCK_NAME,
    ):
        if not path.is_file():
            raise VitisLaneInvalid(f"Vitis lane file is missing: {path.relative_to(root)}")

    embed_pth = [line.strip() for line in (embed / "python312._pth").read_text("utf-8").splitlines()]
    if "import site" in embed_pth:
        raise VitisLaneInvalid(
            f"{EMBED_DIR}/python312._pth enables import site; the embed must stay a base"
        )
    venv_pth = [
        line.strip() for line in (venv / "Scripts" / "python312._pth").read_text("utf-8").splitlines()
        if line.strip()
    ]
    if venv_pth != list(VENV_PTH):
        raise VitisLaneInvalid(
            f"{VENV_DIR}/Scripts/python312._pth does not bind the lane's own site-packages "
            f"and product root: {venv_pth}"
        )
    cfg = (venv / "pyvenv.cfg").read_text("utf-8").splitlines()
    if rf"home = ..\{EMBED_DIR}" not in [line.strip() for line in cfg]:
        raise VitisLaneInvalid(f"{VENV_DIR}/pyvenv.cfg home does not name its sibling {EMBED_DIR}")

    for path in (venv / "pyvenv.cfg", venv / LOCK_NAME, venv / "Scripts" / "python312._pth"):
        if _HOST_PATH.search(path.read_text("utf-8")):
            raise VitisLaneInvalid(f"{path.relative_to(root)} records a host absolute path")

    lock = json.loads((venv / LOCK_NAME).read_text("utf-8"))
    site = venv / "Lib" / "site-packages"
    installed = {item.name.casefold() for item in site.glob("*.dist-info")} if site.is_dir() else set()
    names = set()
    for item in lock.get("distributions") or []:
        name, version = str(item["name"]), str(item["version"])
        names.add(canonicalize_name(name))
        dist_info = f"{name.replace('-', '_')}-{version}.dist-info".casefold()
        if dist_info not in installed:
            raise VitisLaneInvalid(f"distribution {name} {version} is not installed in {VENV_DIR}")
    missing_roots = [item for item in ROOT_REQUIREMENTS if canonicalize_name(item) not in names]
    if missing_roots:
        raise VitisLaneInvalid(f"lane lock does not record {', '.join(missing_roots)}")
    return {"ok": True, "python": str(lock.get("python", "")), "locked": len(names)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bootstrap_vitis_lane", description=__doc__.splitlines()[0])
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--omega-root", type=Path, default=OMEGA_ROOT)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.check_only and args.assets is None:
        parser.error("--assets is required unless --check-only")
    receipt = check_lane(args.omega_root) if args.check_only else bootstrap(args.assets, args.omega_root)
    sys.stdout.write(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
