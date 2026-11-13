from __future__ import annotations

from guardian.telemetry.writer import _is_failure


def test_bad_request_validation_not_failure_sink():
    assert not _is_failure(
        {
            "run_id": "live-boot",
            "status": "fail",
            "failure_class": "bad_request",
            "phase": "chat",
        }
    )


def test_preboot_substrate_gap_not_failure_sink():
    assert not _is_failure(
        {
            "run_id": "no-run",
            "status": "degraded",
            "failure_class": "stt_model_missing",
            "phase": "stt_transcribe",
        }
    )


def test_preboot_stt_inference_miss_not_failure_sink():
    assert not _is_failure(
        {
            "run_id": "no-run",
            "event": "inference",
            "operation": "stt",
            "phase": "stt",
            "status": "fail",
            "ok": False,
        }
    )


def test_preboot_tts_inference_miss_not_failure_sink():
    assert not _is_failure(
        {
            "run_id": "no-run",
            "event": "inference",
            "operation": "tts",
            "phase": "tts",
            "status": "fail",
            "ok": False,
        }
    )


def test_live_boot_substrate_gap_is_failure_sink():
    assert _is_failure(
        {
            "run_id": "49ae2b13-f62a-482a-a029-c752bcc2b366",
            "status": "degraded",
            "failure_class": "stt_model_missing",
            "phase": "stt_transcribe",
        }
    )


def test_live_boot_real_failure_still_logged():
    assert _is_failure(
        {
            "run_id": "49ae2b13-f62a-482a-a029-c752bcc2b366",
            "status": "fail",
            "failure_class": "chat_chain_raised",
            "phase": "chat",
        }
    )
