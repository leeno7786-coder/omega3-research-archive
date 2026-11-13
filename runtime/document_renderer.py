"""Pure-python document render pipeline (md -> html -> docx/pdf)."""

from __future__ import annotations

import io
from typing import Optional, Union


def _emit_render_fail(error: str, *, fmt: str) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "runtime",
            "document_render_failed",
            status="fail",
            ok=False,
            error=error,
            format=fmt,
        )
    except Exception:
        pass


def _markdown_to_html(markdown_text: str, *, title: str = "") -> str:
    import markdown as md_lib

    body = md_lib.markdown(
        markdown_text or "",
        extensions=["tables", "fenced_code"],
    )
    safe_title = (title or "Document").replace("<", "").replace(">", "")
    return (
        "<!DOCTYPE html><html><head>"
        f"<meta charset='utf-8'><title>{safe_title}</title>"
        f"</head><body>{body}</body></html>"
    )


def _render_docx(html: str) -> bytes:
    from htmldocx import HtmlToDocx

    buf = io.BytesIO()
    doc = HtmlToDocx().parse_html_string(html)
    doc.save(buf)
    return buf.getvalue()


def _render_pdf(html: str) -> bytes:
    from xhtml2pdf import pisa

    buf = io.BytesIO()
    status = pisa.CreatePDF(html, dest=buf)
    if status.err:
        raise RuntimeError(f"xhtml2pdf failed with err={status.err}")
    return buf.getvalue()


def render_document(
    markdown_text: str,
    fmt: str,
    *,
    title: str = "",
    css: Optional[str] = None,
) -> Union[bytes, str]:
    """Render markdown to md (passthrough), docx, or pdf bytes."""
    fmt_norm = str(fmt or "md").lower().lstrip(".")
    try:
        if fmt_norm == "md":
            return markdown_text or ""
        html = _markdown_to_html(markdown_text, title=title)
        if css:
            html = html.replace("</head>", f"<style>{css}</style></head>")
        if fmt_norm == "docx":
            return _render_docx(html)
        if fmt_norm == "pdf":
            return _render_pdf(html)
        raise ValueError(f"unsupported document format: {fmt!r}")
    except Exception as exc:
        _emit_render_fail(str(exc), fmt=fmt_norm)
        raise
