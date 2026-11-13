# DOCUMENT_CREATE — Design Spec (locked)

**Author:** claude · **Date:** 2026-06-08 · **Status:** decisions locked, ready to map+build
**Pattern:** clone the proven `IMAGE_CREATE` rail (cognition-mediated, deterministic-dispatch
creation). This is NOT a new subsystem.

## Operator-locked decisions

1. **New intent `DOCUMENT_CREATE`** — a *user-directed* creation intent. Per doctrine, the
   "exactly 8 `ALLOWED_INTENTS`" is the cognition-INFERRED set; user-directed creation intents are
   open-ended and reached ONLY via the panel's `directive_intent`. Add `DOCUMENT_CREATE` to
   `ALLOWED_INTENTS` so the directive validates + Global A maps it verbatim. No autonomous
   word-hint fallback required.
2. **PDF engine: pure-python only** (`xhtml2pdf`/`pisa` or `reportlab`) — NO system binaries
   (weasyprint/pandoc/wkhtmltopdf/LibreOffice are banned for dist portability). Plainer styling
   accepted.
3. **Template starter set (ship these 7):** Report, Letter, Meeting Minutes, Summary, Résumé,
   README/Spec, + a generic "clean it up" default.
4. **Upload extraction scope v1:** txt / md / docx / pdf (text). **Image + OCR deferred** to a
   later phase.

## The rail (mirror of IMAGE_CREATE — each row is the contract)

| Image piece (exists, verified) | Document sibling to build |
|---|---|
| HUD image panel → `POST /api/chat {text, intent:"IMAGE_CREATE"}` (`omega_interface.html`) | HUD **Document panel** → `POST /api/chat {text, intent:"DOCUMENT_CREATE", format, template, upload_ref?}` |
| `hud/server.py` chat handler threads `directive_intent`/`weight` (~3354-3367) | thread `format` (`md`\|`docx`\|`pdf`), `template`, `upload_ref` through the same `chat_kwargs` |
| `pressure_loop/operator_chat.py` threads directive_intent (198-239) | thread the new fields into the message metadata |
| `pressure_loop/contributors.py` validates intent ∈ ALLOWED_INTENTS (106-110) | `DOCUMENT_CREATE` added to `ALLOWED_INTENTS` (12-21) |
| `global_loop/intent/run.py` maps directive verbatim (59-64) | unchanged (directive passthrough already generic) |
| `config/meta.yaml: IMAGE_CREATE → IMAGE_CREATION` (59) | add `DOCUMENT_CREATE → DOCUMENT_CREATION` |
| `meta_loop/tool/run.py` capability map `image → image_tool` | add `document → document_tool`; new `DOCUMENT_CREATE` branch sets tool + carries format/template/input |
| `config/tools.yaml: image_tool {runtime:true}` | add `document_tool` (**NOT** runtime socket — local: model-fill + deterministic render) |
| `auto_loop/execute/run.py` IMAGE_CREATION branch → `persist_generated_image` | new `DOCUMENT_CREATION` branch → model-fill → render → `persist_document` |
| `runtime/image_store.py` → `guardian/state/images/<date>/<file>` | `runtime/document_store.py` → `guardian/state/documents/<date>/<ts>_<slug>.<ext>` |
| `_attach_image_urls` → `/api/images/...` (`hud/server.py`) | `_attach_document_url` → `/api/documents/<date>/<name>` |
| `GET /api/images/{date}/{name}` serves (3053) | `GET /api/documents/{date}/{name}` serves **with `Content-Disposition: attachment`** (= the download link) |
| `GET /api/images` list (3113) → Created Images gallery | `GET /api/documents` list → **Created Documents** library in Settings |
| `/api/chat` returns `image:{path,url}` | returns `document:{path,url,format,title}` → HUD renders inline **download link** |

## The one genuinely new mechanism: model-fill + deterministic render

`document_tool` execution (inside the AUTO `DOCUMENT_CREATION` branch):

1. **(optional) Extract** — if `upload_ref` present, extract text from the staged upload
   (`guardian/state/uploads/...`): txt/md read; docx via `python-docx`; pdf via a pure-python
   extractor (`pypdf`/`pdfminer.six`). Reuse the existing `file_reader` tool where possible.
   Image/OCR deferred.
2. **Fill** — the resident generate model (cognition's organ) is handed a cognition-authored
   prompt = *(selected template skeleton + fill-instructions + the user input/extracted text)*
   and emits **Markdown** (the filled document body). This is the normal AUTO generation step;
   the model only fills, it does not pick the template or format (cognition/the panel did).
3. **Render** (deterministic, pure-python) — common intermediate is **HTML**:
   - `md` → passthrough (write the model's markdown).
   - markdown → HTML via the pure-python `markdown` lib.
   - HTML → **docx** via `htmldocx` (pure-python) — *or* a `python-docx` markdown mapper if
     `htmldocx` proves unreliable on Windows/dist (Cursor validates).
   - HTML → **pdf** via `xhtml2pdf`/`pisa` (pure-python); template CSS supplies styling.
4. **Persist** — `persist_document(rendered_bytes_or_text, title, fmt)` → canonical store; episode
   carries the PATH (not bytes), exactly like images.

**Determinism preserved:** cognition (the panel's directive + template + format) decides; the model
is the organ that fills; the render is mechanical code. Same doctrine as IMAGE_CREATE.

## Templates

- Live in `config/document_templates/<name>/` (shipped with the app; user templates later).
- Each = a markdown skeleton with placeholders + a short fill-instruction fragment (+ optional CSS
  for PDF/docx styling). 7 templates per the locked set; "generic" = clean-up/structure-as-is.
- Selection: explicit in the panel (dropdown), default "generic". (Cognition-chosen template is a
  Phase-3 nicety.)

## Failure-log integration (consistent with the just-built surface)

Every failure branch emits to the bounded failure log, additively:
- `document_tool` extract/fill/render failures → `emit_data("auto", "document_<step>_failed",
  status="fail"|"degraded", ok=False, error=...)`.
- `runtime/document_store.py::persist_document` → mirror `image_store`'s `_emit_image_store`
  (`persist_document` ok/fail emit).
- HUD `/api/documents` serve/list failures → `emit_data("hud", ...)` like the image endpoints.

## Phasing

- **Phase 1 — MVP rail:** paste text → choose format → `DOCUMENT_CREATE` → light structuring (generic
  template) → render (md/docx/pdf) → store → Created Documents library **+ download link**.
  Proves the whole rail end-to-end.
- **Phase 2 — automation:** the 7 templates (LLM fills premade skeletons) + upload→extraction→fill.
- **Phase 3 — polish:** cognition-chosen templates, multi-section/structured fills, image+OCR uploads.

## New dependencies (all pip, no system binaries)

`markdown`, `htmldocx` (or python-docx mapper), `xhtml2pdf` (or `reportlab`), `python-docx`,
`pypdf`/`pdfminer.six` (upload extraction). Cursor validates each imports + renders on the dist
target before committing.

## Out of scope (explicit)

Image/OCR upload extraction (deferred); cognition-auto template selection (Phase 3); editing/
re-rendering stored docs; cloud delivery. The download link is IN scope (Phase 1) — it's ~free
given the image-serving precedent.
