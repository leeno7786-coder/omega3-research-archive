import pytest

from tests.helpers.vitis_hardware import unsupported_driver_reason


@pytest.mark.parametrize("version", ["32.0.203.280", "32.0.203.314", "32.0.20102.3930"])
def test_supported_driver_is_not_skipped(version):
    assert unsupported_driver_reason([
        {"DeviceID": r"PCI\VEN_1022&DEV_17F0&REV_20", "DriverVersion": version},
    ]) is None


def test_old_driver_reports_the_observed_version_and_required_minimum():
    reason = unsupported_driver_reason([
        {"DeviceID": r"PCI\VEN_1022&DEV_1502&REV_00", "DriverVersion": "32.0.202.100"},
    ])
    assert "32.0.202.100" in reason
    assert "32.0.203.280" in reason


def test_unrelated_device_does_not_admit_a_live_hardware_test():
    assert "no supported AMD NPU" in unsupported_driver_reason([
        {"DeviceID": r"PCI\VEN_1022&DEV_0001", "DriverVersion": "32.0.203.314"},
    ])


def test_malformed_driver_metadata_fails_instead_of_skipping():
    with pytest.raises(ValueError):
        unsupported_driver_reason([
            {"DeviceID": r"PCI\VEN_1022&DEV_17F0", "DriverVersion": "invalid"},
        ])
