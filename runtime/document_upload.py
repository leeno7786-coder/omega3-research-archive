"""Extract text from staged operator uploads for document creation."""

from __future__ import annotations

from pathlib import Path

from core.paths import GUARDIAN_STATE_ROOT

UPLOAD_ROOT = GUARDIAN_STATE_ROOT / "uploads"
_ALLOWED_KINDS = frozenset({"image", "doc", "other"})


def _normalize_upload_ref(upload_ref: str) -> tuple[str, str]:
    ref = str(upload_ref or "").strip()
    if ref.startswith("/api/uploads/"):
        ref = ref[len("/api/uploads/") :]
    ref = ref.lstrip("/")
    if not ref or ".." in ref or ref.startswith(("/", "\\")):
        raise ValueError(f"invalid upload_ref: path traversal rejected ({upload_ref!r})")
    parts = ref.replace("\\", "/").split("/")
    if len(parts) != 2:
        raise ValueError(f"invalid upload_ref: expected kind/name ({upload_ref!r})")
    kind, name = parts[0], parts[1]
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"invalid upload_ref: unknown kind {kind!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"invalid upload_ref: path traversal rejected ({upload_ref!r})")
    return kind, name


def _resolve_upload_path(upload_ref: str) -> Path:
    kind, name = _normalize_upload_ref(upload_ref)
    path = UPLOAD_ROOT / kind / name
    try:
        resolved = path.resolve()
        upload_root = UPLOAD_ROOT.resolve()
        if upload_root not in resolved.parents and resolved != upload_root:
            raise ValueError(f"invalid upload_ref: path escapes uploads root ({upload_ref!r})")
    except FileNotFoundError:
        pass
    if not path.is_file():
        raise FileNotFoundError(f"upload not found: {upload_ref}")
    return path


def extract_upload_text(upload_ref: str) -> str:
    """Extract text from a staged upload under guardian/state/uploads/.

    Supports .txt/.md (direct read), .docx (python-docx), .pdf (pypdf).
    Raises on unreadable or unsupported types.
    """
    path = _resolve_upload_path(upload_ref)
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts: list[str] = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    text = cell.text.strip()
                    if text:
                        parts.append(text)
        return "\n".join(parts)
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    raise ValueError(f"unsupported upload type for extraction: {suffix or path.name!r}")
