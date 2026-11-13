import json

from core import boot_status


def test_boot_status_round_trips_atomically_under_the_product_root(tmp_path) -> None:
    written = boot_status.write_boot_status(
        tmp_path,
        phase="memory",
        percent=64,
        detail="Preparing memory services",
        pid=1234,
    )

    assert written["phase"] == "memory"
    assert written["percent"] == 64
    assert written["pid"] == 1234
    assert boot_status.read_boot_status(tmp_path) == written
    assert boot_status.boot_status_path(tmp_path).is_file()


def test_boot_status_rejects_unknown_phases_and_unbounded_progress(tmp_path) -> None:
    for phase in ("", "done", "failed\nnot-safe"):
        try:
            boot_status.write_boot_status(tmp_path, phase=phase, percent=20, detail="x")
        except ValueError:
            pass
        else:
            raise AssertionError(f"phase {phase!r} should be rejected")

    for percent in (-1, 101):
        try:
            boot_status.write_boot_status(tmp_path, phase="starting", percent=percent, detail="x")
        except ValueError:
            pass
        else:
            raise AssertionError(f"percent {percent} should be rejected")


def test_boot_status_payload_is_bounded_and_has_no_partial_temp_file(tmp_path) -> None:
    boot_status.write_boot_status(tmp_path, phase="starting", percent=1, detail="x" * 1000)

    path = boot_status.boot_status_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["detail"]) <= boot_status.MAX_DETAIL_LENGTH
    assert not path.with_suffix(path.suffix + ".tmp").exists()
