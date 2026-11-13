"""Hardware prerequisites for the real portable Vitis placement test.

AMD Ryzen AI 1.7.1 lists 32.0.203.280 as its minimum Windows driver and
PCI vendor 1022/device 1502 or 17F0 for the supported NPU families:
https://ryzenai.docs.amd.com/en/1.7.1/inst.html
Provider import or inference errors after these checks are test failures.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from core.paths import vitis_python_path

MINIMUM_DRIVER = (32, 0, 203, 280)
SUPPORTED_IDS = (r"PCI\VEN_1022&DEV_1502", r"PCI\VEN_1022&DEV_17F0")


def unsupported_driver_reason(drivers: list[dict[str, str]]) -> str | None:
    supported = [
        driver for driver in drivers
        if str(driver.get("DeviceID", "")).upper().startswith(SUPPORTED_IDS)
    ]
    if not supported:
        return "no supported AMD NPU (PCI 1022:1502 or 1022:17F0) is installed"
    versions = [tuple(int(part) for part in row["DriverVersion"].split(".")) for row in supported]
    if any(len(version) != 4 for version in versions):
        raise ValueError("AMD NPU driver must have a four-part version")
    if max(versions) < MINIMUM_DRIVER:
        observed = ", ".join(row["DriverVersion"] for row in supported)
        return f"AMD NPU driver {observed} is below Ryzen AI 1.7.1 minimum 32.0.203.280"
    return None


def require_vitis_hardware() -> None:
    if os.name != "nt":
        pytest.skip("portable Vitis hardware placement requires Windows")
    if not vitis_python_path().is_file():
        pytest.skip("portable Vitis interpreter is absent; run scripts/bootstrap_vitis_lane.py")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "@(Get-CimInstance Win32_PnPSignedDriver | "
         "Where-Object { $_.DeviceID -like 'PCI*VEN_1022*' } | "
         "Select-Object DeviceID,DriverVersion) | ConvertTo-Json -Compress"],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    raw = result.stdout.strip()
    records = json.loads(raw) if raw else []
    reason = unsupported_driver_reason([records] if isinstance(records, dict) else records)
    if reason:
        pytest.skip(reason)
