from __future__ import annotations

import json
import subprocess
import sys


def test_replay_profile_isolation_preserves_the_product_encoder_seat(tmp_path, monkeypatch):
    import core.paths as core_paths
    from memory import retina
    from memory.determinism_replay import _isolated_memory_modules

    product_root = core_paths.OMEGA_ROOT
    replay_root = tmp_path / "isolated-replay"
    resident = object()
    resolved_roots = []

    def encoder():
        if not resolved_roots:
            resolved_roots.append(core_paths.OMEGA_ROOT)
            if core_paths.OMEGA_ROOT != product_root:
                raise RuntimeError("required model resolved from writable replay profile")
        return resident

    monkeypatch.setattr(retina, "encoder", encoder)
    with _isolated_memory_modules(replay_root):
        assert core_paths.OMEGA_ROOT == replay_root
        assert retina.encoder() is resident
    assert resolved_roots == [product_root]
    assert core_paths.OMEGA_ROOT == product_root


def test_determinism_replay_harness_reports_identical_runs(tmp_path):
    from memory.determinism_replay import run_determinism_replay

    report = run_determinism_replay(base_dir=tmp_path / "replay")

    assert report["ok"], report["errors"]
    assert report["comparison"]["equal"]
    assert report["comparison"]["first_hash"] == report["comparison"]["second_hash"]
    assert report["mutation_probes"]["ok"], report["mutation_probes"]

    first, second = report["runs"]
    assert first["scoring"]["verified_expected"] < first["scoring"]["total_expected"]
    assert first["scoring"]["safe_fail_closed"]["unknown"] >= 1
    assert first["scoring"]["safe_fail_closed"]["ambiguous"] >= 1
    # The verdict topicality gate (operator adjudication 2026-07-18:
    # router-as-topicality-authority + zero-overlap floor, strict unknown)
    # restores the both-lane conflict verdict: residue can no longer ground a
    # "verified" past a quarantined conflict on EITHER rail.
    conflict = next(q for q in first["queries"] if q["label"] == "conflict_query")
    assert (conflict.get("graph_verdict") or {}).get("status") == "conflict", conflict
    assert (conflict.get("dme_verdict") or {}).get("status") == "conflict", conflict
    assert first["boundary_assertions"]["ok"], first["boundary_assertions"]
    assert first["fingerprints"] == second["fingerprints"]
    assert first["snapshot"]["snapshot_hash"] == second["snapshot"]["snapshot_hash"]
    assert first["dme_parity"]["status"] == "ok"
    assert second["dme_parity"]["status"] == "ok"

    # The 2026-07-17 verdict-grounding gap is CLOSED (topicality gate). No gap
    # may exist silently: a future KNOWN_VERDICT_GAPS declaration must be a
    # deliberate, operator-visible act — and this assert makes it one.
    assert not report["known_gaps"], report["known_gaps"]

    for query in first["queries"]:
        assert query["ok"], query
        if query["expected_status"] == "verified" and query["success_mode"] == "exact":
            assert query["graph_expected_rank"] == 1
            assert query["dme_expected_rank"] is not None
            assert query["graph_verdict"]["status"] == "verified"
            assert query["dme_verdict"]["status"] == "verified"
            assert query["graph_verdict"]["answer_payload"] is not None
            assert query["dme_verdict"]["answer_payload"] is not None
        elif query["expected_status"] == "verified" and query["success_mode"] == "graph_only":
            assert query["graph_verdict"]["status"] == "verified"
            assert query["graph_verdict"]["answer_payload"] is not None
            # The DME leg's graph_only contract is the LOBE BOUNDARY, not refusal
            # (see _run_query: the graded Thought Engine legitimately surfaces
            # in-bounds candidates) — the harness ok computation enforces it; the
            # old strict None pin encoded the retired strict-intersection engine.
        elif query["expected_status"] == "verified":
            assert query["graph_verdict"]["status"] == "verified"
            assert query["dme_verdict"]["status"] == "verified"
        else:
            assert query["graph_verdict"]["status"] in {"unknown", "ambiguous", "conflict"}
            assert query["dme_verdict"]["status"] in {"unknown", "ambiguous", "conflict"}
            assert query["graph_verdict"]["answer_payload"] is None
            assert query["dme_verdict"]["answer_payload"] is None


def test_memory_determinism_replay_cli_returns_json(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/memory_determinism_replay.py",
            "--base-dir",
            str(tmp_path / "cli-replay"),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["comparison"]["equal"] is True


def test_memory_nondeterminism_scanner_has_no_unapproved_findings():
    from scripts.memory_nondeterminism_scan import scan

    assert scan() == []
