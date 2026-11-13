"""Bootstrap and relocation contract for the Vitis lane interpreter.

The lane is its own CPython 3.12 stack on the SSD: ``python-3.12-embed`` is the
portable base and ``.venv-vitis`` is its package environment, laid out exactly
like ``.venv-brain`` on ``python-3.13-embed``.  These tests build the lane from
tiny stand-in assets so the installer itself is exercised: wheel closure,
markers, RECORD integrity, ``.data`` payloads, and the relative-only layout
that lets the tree move to another drive.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from scripts import bootstrap_vitis_lane as lane


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={digest.decode('ascii')}"


def _wheel(
    folder: Path,
    name: str,
    version: str,
    tag: str,
    files: dict[str, bytes],
    *,
    requires: tuple[str, ...] = (),
    corrupt: str | None = None,
) -> Path:
    dist_info = f"{name}-{version}.dist-info"
    metadata = "\n".join(
        ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
        + [f"Requires-Dist: {item}" for item in requires]
    ).encode("utf-8") + b"\n"
    entries = dict(files)
    entries[f"{dist_info}/METADATA"] = metadata
    entries[f"{dist_info}/WHEEL"] = (
        f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n".encode("utf-8")
    )
    record_lines = [f"{path},{_record_hash(data)},{len(data)}" for path, data in entries.items()]
    record_lines.append(f"{dist_info}/RECORD,,")
    entries[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")
    if corrupt is not None:
        entries[corrupt] = entries[corrupt] + b"tampered"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in entries.items():
            archive.writestr(member, data)
    return path


def _embed_zip(folder: Path, *, dll: str = "python312.dll", version: str = "3.12.10") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"python-{version}-embed-amd64.zip"
    stem = dll.removesuffix(".dll")
    with zipfile.ZipFile(path, "w") as archive:
        for member in (
            "python.exe", "pythonw.exe", dll, "python3.dll", "vcruntime140.dll",
            "vcruntime140_1.dll", "_ctypes.pyd", "libffi-8.dll", f"{stem}.zip",
            "LICENSE.txt",
        ):
            archive.writestr(member, f"fixture {member}\n")
        archive.writestr(f"{stem}._pth", f"{stem}.zip\n.\n\n# Uncomment to run site.main()\n#import site\n")
    return path


def _assets(tmp_path: Path, **overrides) -> Path:
    assets = tmp_path / "assets"
    _embed_zip(assets, **overrides.pop("embed", {}))
    wheels = assets / "cp312-deps"
    specs = {
        "onnxruntime_vitisai": dict(
            version="1.23.2", tag="cp312-cp312-win_amd64",
            files={"onnxruntime/__init__.py": b"__version__ = '1.23.2'\n",
                   "onnxruntime/capi/onnxruntime.dll": b"ort"},
            requires=("voe==1.7.0", "numpy>=1.21.6", "coloredlogs"),
        ),
        "voe": dict(
            version="1.7.0", tag="py3-none-win_amd64",
            files={"voe/__init__.py": b"",
                   "voe-1.7.0.data/data/lib/site-packages/onnxruntime/capi/onnxruntime_vitisai_ep.dll": b"ep"},
        ),
        "numpy": dict(
            version="1.26.4", tag="cp312-cp312-win_amd64",
            files={"numpy/__init__.py": b"__version__ = '1.26.4'\n"},
        ),
        "coloredlogs": dict(
            version="15.0.1", tag="py2.py3-none-any",
            files={"coloredlogs/__init__.py": b""},
            requires=("humanfriendly (>=9.1)", "capturer (>=2.4) ; extra == 'cron'"),
        ),
        "humanfriendly": dict(
            version="10.0", tag="py2.py3-none-any",
            files={"humanfriendly/__init__.py": b""},
            requires=(
                'monotonic ; python_version == "2.7"',
                'pyreadline3 ; sys_platform == "win32" and python_version>="3.8"',
            ),
        ),
        "pyreadline3": dict(
            version="3.5.6", tag="py3-none-any",
            files={"pyreadline3/__init__.py": b"", "readline.py": b""},
        ),
    }
    for name, spec in overrides.pop("wheels", {}).items():
        if spec is None:
            specs.pop(name)
        else:
            specs[name] = {**specs.get(name, {}), **spec}
    for name, spec in specs.items():
        _wheel(wheels, name, spec["version"], spec["tag"], spec["files"],
               requires=spec.get("requires", ()), corrupt=spec.get("corrupt"))
    assert not overrides
    return assets


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_bootstrap_builds_the_embed_base_and_its_venv(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()

    receipt = lane.bootstrap(_assets(tmp_path), root)

    embed = root / "python-3.12-embed"
    venv = root / ".venv-vitis"
    assert (embed / "python.exe").is_file()
    assert _lines(embed / "python312._pth") == ["python312.zip", "."]
    scripts = venv / "Scripts"
    for name in ("python.exe", "python312.dll", "python312.zip", "_ctypes.pyd", "vcruntime140.dll"):
        assert (scripts / name).is_file(), name
    assert not (scripts / "pythonw.exe").exists()
    assert _lines(scripts / "python312._pth") == [
        "python312.zip", ".", r"..\Lib\site-packages", r"..\..", "import site",
    ]
    cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "home = ..\\python-3.12-embed" in cfg.splitlines()
    assert "include-system-site-packages = false" in cfg.splitlines()

    site = venv / "Lib" / "site-packages"
    assert (site / "onnxruntime" / "__init__.py").is_file()
    assert (site / "onnxruntime" / "capi" / "onnxruntime_vitisai_ep.dll").read_bytes() == b"ep"
    assert (site / "pyreadline3" / "__init__.py").is_file()
    assert (site / "onnxruntime_vitisai-1.23.2.dist-info" / "METADATA").is_file()
    assert not (site / "voe-1.7.0.data").exists()

    lock = json.loads((venv / lane.LOCK_NAME).read_text(encoding="utf-8"))
    assert lock["python"] == "3.12.10"
    assert sorted(item["name"] for item in lock["distributions"]) == [
        "coloredlogs", "humanfriendly", "numpy", "onnxruntime-vitisai", "pyreadline3", "voe",
    ]
    assert all(len(item["sha256"]) == 64 for item in lock["distributions"])
    assert receipt["distributions"] == 6
    assert lane.check_lane(root)["ok"] is True


def test_the_lane_relocates_with_the_product_tree(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    lane.bootstrap(_assets(tmp_path), root)

    moved = tmp_path / "other drive" / "Omega"
    moved.parent.mkdir()
    shutil.copytree(root, moved)

    assert lane.check_lane(moved)["ok"] is True
    for path in (moved / ".venv-vitis").glob("*"):
        if path.is_file():
            assert str(tmp_path) not in path.read_text(encoding="utf-8", errors="replace")
    assert str(tmp_path) not in (moved / ".venv-vitis" / "Scripts" / "python312._pth").read_text(
        encoding="utf-8"
    )


def test_a_missing_dependency_fails_and_leaves_nothing_behind(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()

    with pytest.raises(lane.BootstrapError, match="humanfriendly"):
        lane.bootstrap(_assets(tmp_path, wheels={"humanfriendly": None}), root)

    assert sorted(path.name for path in root.iterdir()) == []


def test_an_embed_that_is_not_cpython_312_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()

    with pytest.raises(lane.BootstrapError, match="3.12"):
        lane.bootstrap(_assets(tmp_path, embed={"dll": "python313.dll", "version": "3.13.14"}), root)


def test_a_wheel_built_for_another_interpreter_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    assets = _assets(tmp_path, wheels={"numpy": {"tag": "cp313-cp313-win_amd64"}})

    with pytest.raises(lane.BootstrapError, match="numpy"):
        lane.bootstrap(assets, root)


def test_a_wheel_whose_record_does_not_match_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    assets = _assets(tmp_path, wheels={"voe": {"corrupt": "voe/__init__.py"}})

    with pytest.raises(lane.BootstrapError, match="RECORD"):
        lane.bootstrap(assets, root)


def test_rerunning_the_bootstrap_replaces_the_lane(tmp_path: Path) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    assets = _assets(tmp_path)
    lane.bootstrap(assets, root)
    stale = root / ".venv-vitis" / "Lib" / "site-packages" / "stale.py"
    stale.write_text("left over\n", encoding="utf-8")

    lane.bootstrap(assets, root)

    assert not stale.exists()
    assert lane.check_lane(root)["ok"] is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda root: (root / ".venv-vitis" / "pyvenv.cfg").write_text(
            "home = C:\\Users\\builder\\python-3.12-embed\n", encoding="utf-8"), "home"),
        (lambda root: (root / "python-3.12-embed" / "python312._pth").write_text(
            "python312.zip\n.\nimport site\n", encoding="utf-8"), "import site"),
        (lambda root: shutil.rmtree(
            root / ".venv-vitis" / "Lib" / "site-packages" / "onnxruntime_vitisai-1.23.2.dist-info"),
         "onnxruntime-vitisai"),
        (lambda root: (root / ".venv-vitis" / "Scripts" / "python312._pth").write_text(
            "python312.zip\n.\nC:\\host\\site-packages\nimport site\n", encoding="utf-8"), "_pth"),
    ],
)
def test_the_relocation_check_rejects_a_host_bound_or_incomplete_lane(
    tmp_path: Path, mutate, message: str
) -> None:
    root = tmp_path / "Omega"
    root.mkdir()
    lane.bootstrap(_assets(tmp_path), root)
    mutate(root)

    with pytest.raises(lane.VitisLaneInvalid, match=message):
        lane.check_lane(root)


def test_the_bootstrap_reads_only_the_declared_asset_shape(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    _embed_zip(assets, version="3.12.9")

    with pytest.raises(lane.BootstrapError, match="exactly one"):
        lane.bootstrap(assets, tmp_path)
