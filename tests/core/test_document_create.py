"""DOCUMENT_CREATE Phase 1 — RED-first rail tests (mirror IMAGE_CREATE)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from auto_loop.auto import MemoryEnvelope, build_prompt_pack, execute
from meta_loop.meta_a import MetaAInput, build_execution_action_pack
from pressure_loop.contributors import ALLOWED_INTENTS, from_operator_message


def _mem():
    return MemoryEnvelope(
        memory_required=False,
        memory_status="skipped",
        memory_reason=None,
        memory_scope=(),
        items=(),
        error=None,
    )


def _pack(**kw):
    base = {
        "action": "DOCUMENT_CREATION",
        "tool": "document_tool",
        "execution_inputs": {},
        "source_action_goal": {},
    }
    base.update(kw)
    return base


def _mi(intent: str, *, message: str = "", **meta):
    contributors = []
    if message or meta:
        row_meta = {"message": message, **meta}
        contributors.append(
            {
                "source": "operator",
                "type": "operator_command",
                "weight": 3.0,
                "metadata": row_meta,
            }
        )
    return MetaAInput(
        episode_id="ep_1",
        pressure={"reason": "operator_command", "contributors": contributors},
        trajectory_snapshot={},
        action_goal={
            "episode_id": "ep_1",
            "intent": intent,
            "target": message,
            "confidence": 0.9,
            "reason": "test",
        },
    )


def test_document_create_in_allowed_intents():
    assert "DOCUMENT_CREATE" in ALLOWED_INTENTS
    idx = ALLOWED_INTENTS.index("DOCUMENT_CREATE")
    assert ALLOWED_INTENTS[idx - 1] == "IMAGE_CREATE"
    assert ALLOWED_INTENTS[idx + 1] == "IDLE"


def test_from_operator_message_threads_document_fields():
    c = from_operator_message(
        {
            "message": "meeting notes from standup",
            "directive_intent": "DOCUMENT_CREATE",
            "format": "pdf",
            "template": "generic",
            "upload_ref": "abc123",
        }
    )
    assert c.metadata["directive_intent"] == "DOCUMENT_CREATE"
    assert c.metadata["format"] == "pdf"
    assert c.metadata["template"] == "generic"
    assert c.metadata["upload_ref"] == "abc123"


def test_meta_a_document_create_branch():
    pack = build_execution_action_pack(
        _mi(
            "DOCUMENT_CREATE",
            message="Quarterly report outline",
            format="docx",
            template="generic",
        )
    )
    assert pack.action == "DOCUMENT_CREATION"
    assert pack.tool == "document_tool"
    inputs = pack.execution_inputs
    spec = inputs.get("document_spec") or {}
    assert spec.get("format") == "docx"
    assert spec.get("template") == "generic"
    assert "Quarterly report outline" in str(spec.get("text") or "")


def test_promptpack_document_creation_type():
    pp = build_prompt_pack(
        _pack(
            execution_inputs={
                "document_spec": {
                    "format": "md",
                    "template": "generic",
                    "text": "standup notes",
                }
            }
        ),
        _mem(),
    )
    assert pp.prompt_type == "DOCUMENT_SYSTEM"
    assert "standup notes" in (pp.user_prompt or "")


def test_document_renderer_md_passthrough():
    from runtime.document_renderer import render_document

    out = render_document("# Hello\n\nBody text.", "md", title="Hello")
    assert isinstance(out, str)
    assert "# Hello" in out


def test_document_renderer_docx_bytes():
    from runtime.document_renderer import render_document

    out = render_document("# Title\n\nParagraph.", "docx", title="Title")
    assert isinstance(out, bytes)
    assert len(out) > 100


def test_document_renderer_pdf_bytes():
    from runtime.document_renderer import render_document

    out = render_document("# Title\n\nParagraph.", "pdf", title="Title")
    assert isinstance(out, bytes)
    assert out[:4] == b"%PDF"


_SAMPLE_MD = "# Title\n\nThe quick brown fox jumps over the lazy dog."


def _extract_docx_text(docx_bytes: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    return "\n".join(p.text for p in doc.paragraphs)


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_document_renderer_docx_and_pdf_contain_content_not_literal_body():
    from runtime.document_renderer import render_document

    needle = "quick brown fox"
    docx_bytes = render_document(_SAMPLE_MD, "docx", title="Title")
    pdf_bytes = render_document(_SAMPLE_MD, "pdf", title="Title")

    docx_text = _extract_docx_text(docx_bytes)
    pdf_text = _extract_pdf_text(pdf_bytes)

    assert needle in docx_text, f"docx missing content; got: {docx_text!r}"
    assert "{body}" not in docx_text, f"docx has literal {{body}}; got: {docx_text!r}"

    assert needle in pdf_text, f"pdf missing content; got: {pdf_text!r}"
    assert "{body}" not in pdf_text, f"pdf has literal {{body}}; got: {pdf_text!r}"


def test_document_store_persist_and_list(tmp_path, monkeypatch):
    from runtime import document_store as ds

    root = tmp_path / "documents"
    monkeypatch.setattr(ds, "DOCUMENT_ROOT", root)
    when = datetime(2026, 6, 8, 12, 30, 0, tzinfo=timezone.utc)
    path = ds.persist_document("# Notes\n\nDone.", "Standup Notes", "md", when=when)
    assert path.exists()
    assert path.suffix == ".md"
    items = ds.list_documents(limit=10)
    assert len(items) == 1
    assert items[0]["format"] == "md"
    assert items[0]["name"].endswith(".md")


def test_document_creation_execute_end_to_end(tmp_path, monkeypatch):
    from runtime import document_store as ds

    root = tmp_path / "documents"
    monkeypatch.setattr(ds, "DOCUMENT_ROOT", root)

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

    generate_calls = []

    def fake_generate(**kw):
        generate_calls.append(kw)
        return {"text": "# Report\n\nStructured content from model."}

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **kw: fake_generate(**kw),
    )

    pack = _pack(
        execution_inputs={
            "document_spec": {
                "format": "md",
                "template": "generic",
                "text": "raw standup bullets",
            }
        }
    )
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp,
        "TEXT_REASONING",
        pack,
        _mem(),
        "ep_doc_1",
        runtime_client=FakeRuntime(),
    )
    assert res["failure_kind"] is None
    art = res["outputs"]["document_artifact"]
    assert art["format"] == "md"
    assert art["path"]
    assert Path(art["path"]).exists()
    assert art["bytes_len"] > 0
    assert generate_calls
    assert generate_calls[0]["operation"] == "auto_document"


# ── Phase 2: templates + upload extraction ──────────────────────────────

_PHASE2_TEMPLATES = {
    "report": "Executive Summary",
    "letter": "Dear {{Recipient}}",
    "meeting_minutes": "Meeting Minutes",
    "summary": "TL;DR",
    "resume": "## Experience",
    "readme_spec": "## Installation",
}


@pytest.mark.parametrize("name,marker", list(_PHASE2_TEMPLATES.items()))
def test_phase2_template_loads_distinct_structure(name, marker):
    from runtime.document_templates import load_template_parts

    skeleton, instructions = load_template_parts(name)
    assert marker in skeleton, f"{name} skeleton missing {marker!r}"
    assert instructions.startswith("Output ONLY valid Markdown.")


def test_build_fill_prompt_routes_selected_template_not_generic_fallback():
    from runtime.document_templates import build_fill_prompt

    prompt = build_fill_prompt(
        template="report",
        user_prompt="Q3 sales figures",
        fmt="md",
    )
    assert "Executive Summary" in prompt
    assert "Q3 sales figures" in prompt
    assert "{{content}}" not in prompt


def _stage_upload(tmp_path: Path, name: str, content: bytes) -> str:
    """Write a file under tmp uploads root; return kind/name ref."""
    kind = "doc"
    dest_dir = tmp_path / "uploads" / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_bytes(content)
    return f"{kind}/{name}"


def test_extract_upload_text_txt_and_md(tmp_path, monkeypatch):
    from runtime import document_upload as du

    root = tmp_path / "uploads"
    monkeypatch.setattr(du, "UPLOAD_ROOT", root)

    ref_txt = _stage_upload(tmp_path, "notes.txt", b"plain text content")
    ref_md = _stage_upload(tmp_path, "notes.md", b"# Heading\n\nmarkdown body")

    assert "plain text content" in du.extract_upload_text(ref_txt)
    assert "markdown body" in du.extract_upload_text(ref_md)
    assert du.extract_upload_text("/api/uploads/" + ref_txt) == du.extract_upload_text(ref_txt)


def test_extract_upload_text_docx(tmp_path, monkeypatch):
    from docx import Document

    from runtime import document_upload as du

    root = tmp_path / "uploads"
    monkeypatch.setattr(du, "UPLOAD_ROOT", root)

    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("docx paragraph alpha")
    doc.add_table(rows=1, cols=1)
    doc.tables[0].rows[0].cells[0].text = "table cell beta"
    doc.save(buf)

    ref = _stage_upload(tmp_path, "source.docx", buf.getvalue())
    text = du.extract_upload_text(ref)
    assert "docx paragraph alpha" in text
    assert "table cell beta" in text


def test_extract_upload_text_pdf(tmp_path, monkeypatch):
    from reportlab.pdfgen import canvas

    from runtime import document_upload as du

    root = tmp_path / "uploads"
    monkeypatch.setattr(du, "UPLOAD_ROOT", root)

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "pdf extractable line")
    c.save()

    ref = _stage_upload(tmp_path, "source.pdf", buf.getvalue())
    assert "pdf extractable line" in du.extract_upload_text(ref)


def test_extract_upload_text_unsupported_extension_raises(tmp_path, monkeypatch):
    from runtime import document_upload as du

    root = tmp_path / "uploads"
    monkeypatch.setattr(du, "UPLOAD_ROOT", root)

    ref = _stage_upload(tmp_path, "photo.png", b"\x89PNG")
    with pytest.raises(ValueError, match="unsupported upload type"):
        du.extract_upload_text(ref)


def test_extract_upload_text_rejects_path_traversal(tmp_path, monkeypatch):
    from runtime import document_upload as du

    root = tmp_path / "uploads"
    monkeypatch.setattr(du, "UPLOAD_ROOT", root)

    with pytest.raises(ValueError, match="path traversal|invalid"):
        du.extract_upload_text("../../etc/passwd")


def test_document_creation_with_upload_ref_includes_extracted_text(tmp_path, monkeypatch):
    from runtime import document_store as ds
    from runtime import document_upload as du

    doc_root = tmp_path / "documents"
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(ds, "DOCUMENT_ROOT", doc_root)
    monkeypatch.setattr(du, "UPLOAD_ROOT", upload_root)

    needle = "UPLOADED_STANDUP_NOTES_UNIQUE"
    ref = _stage_upload(tmp_path, "standup.txt", needle.encode("utf-8"))

    generate_calls = []

    def fake_generate(**kw):
        generate_calls.append(kw)
        return {"text": kw.get("prompt") or ""}

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **kw: fake_generate(**kw),
    )

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

    pack = _pack(
        execution_inputs={
            "document_spec": {
                "format": "md",
                "template": "generic",
                "text": "format these notes",
                "upload_ref": ref,
            }
        }
    )
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp,
        "TEXT_REASONING",
        pack,
        _mem(),
        "ep_doc_upload",
        runtime_client=FakeRuntime(),
    )
    assert res["failure_kind"] is None
    assert generate_calls
    assert needle in generate_calls[0]["prompt"]
    art = res["outputs"]["document_artifact"]
    assert needle in Path(art["path"]).read_text(encoding="utf-8")


def test_document_creation_bad_upload_ref_fails_before_generation_and_emits_failure_log(tmp_path, monkeypatch):
    import json

    from guardian.telemetry import writer
    from runtime import document_store as ds
    from runtime import document_upload as du

    doc_root = tmp_path / "documents"
    upload_root = tmp_path / "uploads"
    monkeypatch.setattr(ds, "DOCUMENT_ROOT", doc_root)
    monkeypatch.setattr(du, "UPLOAD_ROOT", upload_root)
    writer.FAILURE_LOG.unlink(missing_ok=True)

    prompt_only = "prompt only content UNIQUE"
    generate_calls = []

    def fake_generate(**kw):
        generate_calls.append(kw)
        return {"text": "# Doc\n\n" + (kw.get("prompt") or "")}

    monkeypatch.setattr(
        "runtime.socket.invoke_runtime_generate",
        lambda **kw: fake_generate(**kw),
    )

    class FakeRuntime:
        def health_body(self):
            return {"status": "ok", "capabilities": {"coding_model": True}}

    pack = _pack(
        execution_inputs={
            "document_spec": {
                "format": "md",
                "template": "generic",
                "text": prompt_only,
                "upload_ref": "doc/missing_file.txt",
            }
        }
    )
    pp = build_prompt_pack(pack, _mem())
    res = execute(
        pp,
        "TEXT_REASONING",
        pack,
        _mem(),
        "ep_doc_bad_upload",
        runtime_client=FakeRuntime(),
    )
    assert res["failure_kind"] == "document_extract_failed"
    assert "missing_file.txt" in res["failure_reason"]
    assert generate_calls == []
    assert "document_artifact" not in res["outputs"]

    failures = [
        json.loads(line)
        for line in writer.FAILURE_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        r.get("phase") == "document_extract_failed" and r.get("status") == "fail"
        for r in failures
    )
