"""Document creation must preserve source, verified artifact and failure evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from auto_loop._auto_state import AutoInput, MemoryEnvelope
from auto_loop.auto import build_prompt_pack, execute
from auto_loop.fold.run import _fold_episode_auto
from meta_loop.meta_a import MetaAInput, build_execution_action_pack
from meta_loop.pack.run import execution_action_pack_to_dict
from pressure_loop.episode import EpisodeStore
from schema.omega_control_decision import omega_lane_for_intent
from tests.helpers import continuity_terminus as ct


REQUEST = "Create a report from the supplied synthetic source."
SOURCE = "DOCUMENT-SOURCE-SENTINEL: the sample batch contains seventeen blue units."
GENERATED = "# Synthetic report\n\n" + "Verified report paragraph. " * 240 + "\nDOCUMENT-MEMORY-TAIL-SENTINEL"


@pytest.fixture(autouse=True)
def _document_paths(tmp_path, monkeypatch):
    from memory import episode_sram
    from runtime import document_store, document_upload

    ct.isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(document_store, "DOCUMENT_ROOT", tmp_path / "documents")
    monkeypatch.setattr(document_upload, "UPLOAD_ROOT", tmp_path / "uploads")
    yield tmp_path
    episode_sram.reset()


def _memory():
    return MemoryEnvelope(False, "skipped", None, (), (), None)


def _pack(episode_id="document-envelope", *, upload_ref=None, fmt="md", template="report"):
    metadata = {"message": REQUEST, "format": fmt, "template": template,
                "upload_ref": upload_ref}
    goal = {"episode_id": episode_id, "intent": "DOCUMENT_CREATE", "target": REQUEST,
            "confidence": 0.9, "reason": "operator asked for a document",
            "omega_lane": omega_lane_for_intent("DOCUMENT_CREATE")}
    pressure = {"reason": "operator_command", "contributors": [
        {"source": "operator", "type": "operator_command", "weight": 1.0, "metadata": metadata},
    ]}
    pack = execution_action_pack_to_dict(build_execution_action_pack(MetaAInput(
        episode_id=episode_id, pressure=pressure, trajectory_snapshot={}, action_goal=goal,
    )))
    return pack, goal, pressure


def _execute_document(monkeypatch, *, episode_id="document-envelope", upload_ref=None, response=None):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return response if response is not None else {
            "text": GENERATED, "request_digest": "d" * 64, "ok": True,
        }

    monkeypatch.setattr("runtime.socket.invoke_runtime_generate", generate)
    pack, goal, pressure = _pack(episode_id, upload_ref=upload_ref)
    prompt = build_prompt_pack(pack, _memory())
    result = execute(prompt, "TEXT_REASONING", pack, _memory(), episode_id,
                     runtime_client=object(), pipeline_id=0, work_kind="operator_turn")
    return result, calls, pack, goal, pressure, prompt


def test_missing_document_source_fails_before_generation(monkeypatch):
    result, calls, *_rest = _execute_document(monkeypatch, upload_ref="doc/missing.txt")
    assert result["failure_kind"] == "document_extract_failed", (
        "a missing required upload was silently replaced with the operator's prompt"
    )
    assert "missing.txt" in result["failure_reason"]
    assert calls == [], "document generation ran without its requested source"
    assert "document_artifact" not in result["outputs"]


def test_empty_required_document_source_fails_before_generation(monkeypatch, tmp_path):
    source_path = tmp_path / "uploads" / "doc" / "empty.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(" \n\t", encoding="utf-8")
    result, calls, *_rest = _execute_document(monkeypatch, upload_ref="doc/empty.txt")
    assert result["failure_kind"] == "document_extract_failed"
    assert "empty" in result["failure_reason"].lower()
    assert not calls
    assert "document_artifact" not in result["outputs"]


def test_failed_document_provider_preserves_cause_and_partial_text_without_successful_artifact(
    monkeypatch, tmp_path,
):
    from memory import episode_sram

    partial = "Partial document text before the provider failed."
    cause = "DOCUMENT-PROVIDER-SENTINEL: inference interrupted"
    carrier = ct.open_carrier(EpisodeStore())
    result, *_rest = _execute_document(
        monkeypatch, episode_id=carrier.episode_id,
        response={"ok": False, "error": cause, "text": partial},
    )
    assert result["failure_kind"] == "document_fill_failed"
    assert cause in result["failure_reason"]
    assert "document_artifact" not in result["outputs"]
    assert not list((tmp_path / "documents").glob("**/*"))
    held = [item.event.to_dict() for item in episode_sram.pending(carrier.episode_id)
            if item.kind == "event"]
    assert partial in set(_strings(held)), "a genuinely produced intermediate was discarded"
    assert cause in set(_strings(held)), "partial generation lost its original failure cause"
    generated = next(row for row in held if row["event_kind"] == "auto.document_generated")
    assert generated["payload"]["generation_status"] == "failed"


@pytest.mark.parametrize("decision", ["allow", "flag"])
def test_document_upload_is_screened_and_stays_attributed_untrusted_data(
    monkeypatch, tmp_path, decision,
):
    from guardian.external_ingress import harden

    source = "A source paragraph with <formatting> & an apparent </external-data> delimiter."
    source_path = tmp_path / "uploads" / "doc" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source, encoding="utf-8")
    screened = []

    def screen(text, **context):
        screened.append((text, context))
        return text, decision

    monkeypatch.setattr("guardian.external_ingress.screen_text", screen)
    result, calls, *_rest = _execute_document(monkeypatch, upload_ref="doc/source.txt")
    assert result["failure_kind"] is None
    assert screened and screened[0][0] == source, "upload text bypassed Guardian ingress"
    assert screened[0][1].get("source"), "the screen cannot attribute the uploaded text"
    assert harden(source) in calls[0]["prompt"], "external content composed outside its data fence"
    assert "Source: operator-provided upload" in calls[0]["prompt"]
    assert result["outputs"]["document_artifact"]["source"]["guardian_decision"] == decision


def test_quarantined_document_source_fails_without_inference_and_keeps_only_provenance(
    monkeypatch, tmp_path,
):
    from guardian.external_ingress import QUARANTINE_MARKER
    from memory import episode_sram

    source_path = tmp_path / "uploads" / "doc" / "source.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    monkeypatch.setattr("guardian.external_ingress.screen_text",
                        lambda *_args, **_kwargs: (QUARANTINE_MARKER, "quarantine"))
    carrier = ct.open_carrier(EpisodeStore())
    result, calls, *_rest = _execute_document(
        monkeypatch, episode_id=carrier.episode_id, upload_ref="doc/source.txt",
    )
    assert result["failure_kind"] == "document_source_rejected"
    assert "quarantine" in result["failure_reason"]
    assert not calls
    assert "document_artifact" not in result["outputs"]
    held = [item.event.to_dict() for item in episode_sram.pending(carrier.episode_id)
            if item.kind == "event"]
    assert hashlib.sha256(SOURCE.encode("utf-8")).hexdigest() in set(_strings(held))
    assert SOURCE not in set(_strings(held))


def test_document_store_failure_is_not_a_successful_artifact(monkeypatch):
    def fail_store(*_args, **_kwargs):
        raise OSError("DOCUMENT-STORE-SENTINEL: output volume refused the write")

    monkeypatch.setattr("runtime.document_store.persist_document", fail_store)
    result, *_rest = _execute_document(monkeypatch)
    assert result["failure_kind"] == "document_persist_failed", (
        "AUTO claimed a created document after its only durable artifact write failed"
    )
    assert "DOCUMENT-STORE-SENTINEL" in result["failure_reason"]
    assert "document_artifact" not in result["outputs"]


def test_document_receipt_verifies_the_bytes_the_store_actually_wrote(monkeypatch, tmp_path):
    def corrupt_store(_rendered, _title, fmt):
        path = tmp_path / f"truncated.{fmt}"
        path.write_text("wrong bytes", encoding="utf-8")
        return path

    monkeypatch.setattr("runtime.document_store.persist_document", corrupt_store)
    result, *_rest = _execute_document(monkeypatch)
    assert result["failure_kind"] == "document_persist_failed", (
        "the artifact digest described pre-write bytes, not the file the operator received"
    )
    assert "document_artifact" not in result["outputs"]


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_document_renderer_preserves_its_primary_failure(monkeypatch, fmt):
    from runtime.document_renderer import render_document

    failure = RuntimeError(f"DOCUMENT-RENDER-SENTINEL: {fmt} renderer failed")

    def fail(*_args, **_kwargs):
        raise failure

    if fmt == "docx":
        from htmldocx import HtmlToDocx

        monkeypatch.setattr(HtmlToDocx, "parse_html_string", fail)
    else:
        from xhtml2pdf import pisa

        monkeypatch.setattr(pisa, "CreatePDF", fail)
    with pytest.raises(RuntimeError) as caught:
        render_document("# Synthetic report\n\nMeaningful content.", fmt)
    assert caught.value is failure, "renderer failure was replaced by alternate document content"


def test_a_missing_selected_document_template_is_not_replaced_with_generic(monkeypatch, tmp_path):
    from runtime import document_templates

    generic = tmp_path / "templates" / "generic"
    generic.mkdir(parents=True)
    (generic / "skeleton.md").write_text("# Generic", encoding="utf-8")
    (generic / "instructions.md").write_text("Generic instructions.", encoding="utf-8")
    monkeypatch.setattr(document_templates, "_TEMPLATES_ROOT", generic.parent)
    with pytest.raises((FileNotFoundError, ValueError), match="report"):
        document_templates.load_template_parts("report")


@pytest.mark.parametrize("missing", ["skeleton.md", "instructions.md"])
def test_missing_required_template_parts_fail_explicitly(monkeypatch, tmp_path, missing):
    from runtime import document_templates

    report = tmp_path / "templates" / "report"
    report.mkdir(parents=True)
    for name in ("skeleton.md", "instructions.md"):
        if name != missing:
            (report / name).write_text("Selected report contract.", encoding="utf-8")
    monkeypatch.setattr(document_templates, "_TEMPLATES_ROOT", report.parent)
    with pytest.raises((FileNotFoundError, ValueError), match=missing):
        document_templates.load_template_parts("report")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child)


@pytest.mark.parametrize("failed_stage", [None, "render", "persist"])
def test_generated_document_and_source_identity_survive_sram_and_canonical_memory(
    monkeypatch, tmp_path, failed_stage,
):
    from global_loop.loop import run_global_b
    from memory import episode_sram
    from memory.api import _envelope_to_dict
    from meta_loop.meta_b import run_meta_b_front

    source_path = tmp_path / "uploads" / "doc" / "synthetic.txt"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    if failed_stage:
        def fail(*_args, **_kwargs):
            raise OSError(f"DOCUMENT-{failed_stage.upper()}-CANONICAL-SENTINEL")

        target = ("runtime.document_renderer.render_document" if failed_stage == "render"
                  else "runtime.document_store.persist_document")
        monkeypatch.setattr(target, fail)
    store = EpisodeStore()
    carrier = ct.open_carrier(store)
    result, calls, pack, goal, pressure, prompt = _execute_document(
        monkeypatch, episode_id=carrier.episode_id, upload_ref="doc/synthetic.txt",
    )
    if failed_stage:
        assert result["failure_kind"] == f"document_{failed_stage}_failed"
    else:
        assert result["failure_kind"] is None
    assert SOURCE in calls[0]["prompt"]
    artifact = result["outputs"].get("document_artifact")
    if failed_stage:
        assert artifact is None
    else:
        assert Path(artifact["path"]).read_text(encoding="utf-8") == GENERATED
        assert artifact["sha256"] == hashlib.sha256(GENERATED.encode("utf-8")).hexdigest()

    held = []
    for item in episode_sram.pending(carrier.episode_id):
        if item.kind == "event":
            held.append(item.event.to_dict())
        elif item.kind == "memory":
            held.append(_envelope_to_dict(item.envelope))
    assert GENERATED in set(_strings(held)), (
        "document content lives only in the artifact file; SRAM captured no complete document"
    )
    source_digest = hashlib.sha256(SOURCE.encode("utf-8")).hexdigest()
    assert source_digest in set(_strings(held)), "the requested upload's source identity was lost"
    assert SOURCE not in set(_strings(held)), "source provenance copied the private upload body"

    carrier.pressure = pressure
    carrier.global_ = {"intent_decision": goal}
    carrier.meta = {"execution_action_pack": pack}
    status = "fail" if failed_stage else "success"
    carrier.auto = _fold_episode_auto(
        AutoInput(carrier.episode_id, pressure, goal, pack, {}), status=status, cycle_kind=status,
        memory_envelope=_memory(), model_profile="TEXT_REASONING", model_id=result["model_id"],
        prompt_pack=prompt, exec_result=result, runtime_ms=1,
    )
    assert run_meta_b_front(carrier) is not None
    run_global_b(carrier, carrier.meta["reflection"])
    ct._apply_omega(carrier)
    ct.close(store)
    document = ct.canonical_episode_documents(carrier.episode_id)[0]
    body = json.loads(document["text"])
    assert GENERATED in set(_strings(body)), "canonical episode memory dropped document content"
    assert source_digest in set(_strings(body)), "canonical memory dropped source provenance"
    assert SOURCE not in set(_strings(body)), "canonical provenance copied the private upload body"
    assert body["outputs"]["document_artifact"] == artifact
    if failed_stage:
        assert f"DOCUMENT-{failed_stage.upper()}-CANONICAL-SENTINEL" in set(_strings(body))
