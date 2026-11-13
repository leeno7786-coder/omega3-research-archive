# DOCUMENT_CREATE — Implementation Touch-Point Map

**Author:** devin · **Date:** 2026-06-08 · **Status:** map-only (no code changes)
**Spec:** `docs/architecture/DOCUMENT_CREATE_SPEC.md` (locked, operator-approved)
**Pattern:** mirror the proven `IMAGE_CREATE` rail end-to-end

---

## 1. ALLOWED_INTENTS augmentation

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `pressure_loop/contributors.py` | `pressure_loop/contributors.py` |
| Lines | `12-21` | `12-21` (same block) |
| Change | `IMAGE_CREATE` is the 6th intent | Add `"DOCUMENT_CREATE",` between `IMAGE_CREATE` and `IDLE` |
| Notes | `ALLOWED_INTENTS` is a `Tuple[str, ...]` (`SELF_INQUIRY` removed — collapsed to `IDLE`; awakening owns reflection) | Order: `RESPOND`, `PROACTIVE_RESPONSE`, `RESOLVE_QUERY`, `LEARN`, `USE_TOOL`, `IMAGE_CREATE`, **DOCUMENT_CREATE**, `IDLE` |
| Phase | 1 |

---

## 2. Thread `format` / `template` / `upload_ref` through `/api/chat`

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/server.py` | `hud/server.py` |
| Lines | `3354-3367` (chat handler body) | `3354-3367` (same block) |
| Current image path | `intent = payload.get("intent")` → `chat_kwargs["directive_intent"] = str(intent).strip().upper()` (`3354-3359`) | Thread `format`, `template`, `upload_ref` when present: after `face_recognition` block (`3362-3363`), add:<br>`fmt = payload.get("format")` → `if fmt: chat_kwargs["format"] = str(fmt).lower()`<br>`tmpl = payload.get("template")` → `if tmpl: chat_kwargs["template"] = str(tmpl)`<br>`upload_ref = payload.get("upload_ref")` → `if upload_ref: chat_kwargs["upload_ref"] = str(upload_ref)` |
| Notes | `face_recognition` is already threaded as a model (`3362-3363`) | The new fields ride the same `chat_kwargs` dict into `handle_operator_chat` |
| Phase | 1 |

---

## 3. Thread new fields into operator message metadata

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `pressure_loop/operator_chat.py` | `pressure_loop/operator_chat.py` |
| Lines | `193-239` (`handle_operator_chat` signature + msg construction) | `193-239` (same function) |
| Current image path | `directive_intent: Optional[str] = None` param (`198`); `msg = {"message": clean, "ingress_class": ingress_class, "weight": eff_weight}` (`233-236`); `if directive: msg["directive_intent"] = directive` (`238-239`) | Add params: `format: Optional[str] = None`, `template: Optional[str] = None`, `upload_ref: Optional[str] = None` after `weight`. Thread into `msg`: `if format: msg["format"] = format`, `if template: msg["template"] = template`, `if upload_ref: msg["upload_ref"] = upload_ref`. |
| Notes | `from_operator_message(msg)` (`240`) already carries arbitrary metadata through `meta = dict(msg)` (`98`), so the fields reach the Contributor automatically. | No change needed to `from_operator_message` — metadata passthrough is already generic. |
| Phase | 1 |

---

## 4. `global_loop/intent/run.py` — directive passthrough

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `global_loop/intent/run.py` | `global_loop/intent/run.py` |
| Lines | `52-66` (`run` function) | `52-66` (same function) |
| Current image path | `directive = str((dom.metadata or {}).get("directive_intent") or "").strip().upper()` (`59`); `if directive in ALLOWED_INTENTS: return directive` (`60-61`) | **No code change required.** The directive passthrough is already fully generic — any intent in `ALLOWED_INTENTS` (including the new `DOCUMENT_CREATE`) routes verbatim. |
| Notes | The only image-specific logic is `_IMAGE_HINTS` regex fallback (`9-12`, `37-38`) — DOCUMENT_CREATE has no word-hint fallback by design (panel-only directive). | Verify `DOCUMENT_CREATE` is NOT added to `_IMAGE_HINTS` or `_TASK_HINTS` — it must stay panel-only. |
| Phase | 1 (verification only) |

---

## 5. `config/meta.yaml` — intent→action mapping

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `config/meta.yaml` | `config/meta.yaml` |
| Lines | `51-61` (`meta.action` block) | `51-61` (same block) |
| Current image path | `meta_actions` list includes `IMAGE_CREATION` (`51`); `intent_to_action` map: `IMAGE_CREATE: IMAGE_CREATION` (`59`) | Add `DOCUMENT_CREATION` to `meta_actions` list. Add `DOCUMENT_CREATE: DOCUMENT_CREATION` to `intent_to_action` map. |
| Notes | `meta_actions` controls what Meta A considers an executable action. `intent_to_action` is the deterministic conversion table. | `DOCUMENT_CREATION` is the action name (noun form), mirroring `IMAGE_CREATION`. |
| Phase | 1 |

---

## 6. `meta_loop/tool/run.py` — capability map + DOCUMENT_CREATE branch

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `meta_loop/tool/run.py` | `meta_loop/tool/run.py` |
| Lines | `131-136` (`IMAGE_CREATE` branch) | After `131-136` (new `elif` branch) |
| Current image path | `elif intent == "IMAGE_CREATE":` (`131`); `state.tool = "image_tool"` (`132`); `state.tool_reason = "Image generation requires image_tool capability"` (`133`); `spec = target_str or text or "image"` (`134`); `state.execution_inputs = {"image_spec": spec}` (`135`); `state.execution_intent = "Plan image creation (no rendering in Meta A)"` (`136`) | New `elif intent == "DOCUMENT_CREATE":` branch:<br>`state.tool = "document_tool"`<br>`state.tool_reason = "Document creation requires document_tool capability"`<br>Carry `format`, `template`, `upload_ref` from `state` (they arrive via Contributor metadata):<br>`state.execution_inputs = {"format": state.contributor_meta.get("format", "md"), "template": state.contributor_meta.get("template", "generic"), "upload_ref": state.contributor_meta.get("upload_ref"), "prompt": target_str or text or ""}`<br>`state.execution_intent = "Plan document creation (no rendering in Meta A)"` |
| Notes | `MetaAState` does not currently expose `contributor_meta` as a typed field — verify it carries `metadata` ( Contributor metadata is available via `state.text` / `state.target_str` origin). If `MetaAState` only has `text` and `target_str`, the fields must be extracted from the Contributor object upstream or stored on state during intake. | **Discovery:** `MetaAState` definition needs inspection. If it lacks contributor metadata access, the `meta_loop/intake/run.py` phase must stash `contributor.metadata` onto state before tool phase runs. |
| Phase | 1 |

**Additional capability map line:**

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `meta_loop/tool/run.py` | `meta_loop/tool/run.py` |
| Lines | `_CAPABILITY_TOOL_MAP_DEFAULT` dict (`9-22`) | Add `"document": "document_tool"` to `_CAPABILITY_TOOL_MAP_DEFAULT` |
| Notes | `image` → `image_tool` is already present (`14`) | `document` → `document_tool` mirrors it exactly |
| Phase | 1 |

---

## 7. `config/tools.yaml` — `document_tool` registry entry

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `config/tools.yaml` | `config/tools.yaml` |
| Lines | `27-30` (`image_tool` block) | Add after `image_tool` block |
| Current image path | `image_tool:` with `runtime: true`, `capabilities: [image_generation]` | New `document_tool:` block:<br>`document_tool:`<br>&nbsp;&nbsp;`runtime: false` ← **NOT** a runtime socket<br>&nbsp;&nbsp;`capabilities:`<br>&nbsp;&nbsp;&nbsp;&nbsp;`- document_generation` |
| Notes | `runtime: true` means the tool is backed by the portable-lite socket. `document_tool` is **local** (model-fill + deterministic render), so `runtime: false`. | The tool dispatch layer (`runtime_tool_dispatch.py`) must handle `runtime: false` tools by routing to local schema implementations instead of the socket. Verify this path exists or is trivial to add. |
| Phase | 1 |

---

## 8. `auto_loop/execute/run.py` — DOCUMENT_CREATION branch

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `auto_loop/execute/run.py` | `auto_loop/execute/run.py` |
| Lines | `602-696` (`IMAGE_CREATION` branch in `execute()`) | After `696` (new `elif action == "DOCUMENT_CREATION":` block) |
| Current image path | `elif action == "IMAGE_CREATION":` (`602`); validates `pack.get("tool") == "image_tool"` (`603`); calls `invoke_runtime_tool("image_tool", ...)` (`612`); builds `artifact` dict with bytes/sha256/dimensions/prompt (`644-650`); emits `image_artifact` data + event (`655-669`); calls `persist_generated_image(img, prompt)` (`678-680`); stores `artifact["path"]` and `artifact["filename"]` (`681-682`); sets `outputs["image_artifact"] = artifact` (`695`) | New `DOCUMENT_CREATION` branch:<br>1. Validate `pack.get("tool") == "document_tool"` → `failure_kind = "invalid_execution_pack"` if mismatch.<br>2. Extract `format`, `template`, `upload_ref`, `prompt` from `pack.get("execution_inputs")` or `prompt_pack.user_prompt`.<br>3. **(Optional) Extract** — if `upload_ref`: read from `guardian/state/uploads/<kind>/<upload_ref>`; use `file_reader` tool logic for txt/md; `python-docx` for docx; `pypdf` for pdf.<br>4. **Fill** — build a cognition prompt = template skeleton + fill-instructions + user prompt / extracted text; call `_generate(operation="auto_document", ...)` to get markdown output.<br>5. **Render** — `md` → passthrough; markdown → HTML via `markdown` lib; HTML → docx via `htmldocx` (or `python-docx` mapper fallback); HTML → pdf via `xhtml2pdf` (or `reportlab` fallback).<br>6. **Persist** — `from runtime.document_store import persist_document`; `saved = persist_document(rendered_bytes, title, format)`; `artifact["path"] = str(saved)`; `artifact["filename"] = saved.name`.<br>7. `outputs["document_artifact"] = artifact` with keys: `path`, `filename`, `format`, `title`, `sha256`, `bytes_len`.<br>8. `model_id = "document_tool"`.

| Notes | `persist_generated_image` is imported at branch time (`678`) to avoid module-load coupling. Same pattern for `persist_document`. | Failure-log integration (per spec): each step (extract/fill/render/persist) emits via `emit_data("auto", "document_<step>_failed", ...)` on exception. |
| Phase | Phase 1 (MVP: generic template, md/docx/pdf render, no upload); Phase 2 (7 templates + upload extraction) |

---

## 9. `runtime/document_store.py` — canonical store skeleton

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `runtime/image_store.py` | **NEW FILE:** `runtime/document_store.py` |
| Lines | `1-103` (full module) | Clone full module, then adapt |
| Current image path | `IMAGE_ROOT = GUARDIAN_STATE_ROOT / "images"` (`24`); `_slug(prompt)` (`43-48`); `persist_generated_image(img_bytes, prompt, *, when)` (`51-75`); `list_generated_images(limit)` (`78-103`); `_emit_image_store(event_name, *, ok, **data)` (`27-40`) | **NEW:**<br>`DOCUMENT_ROOT = GUARDIAN_STATE_ROOT / "documents"` (mirrors `IMAGE_ROOT`)<br>`_slug(title)` — same regex slug logic, `or "document"` default<br>`persist_document(bytes_or_text: Union[bytes, str], title: str, fmt: str, *, when)` → path shape: `guardian/state/documents/<date>/<ts>_<slug>.<fmt>` (e.g. `.md`, `.docx`, `.pdf`)<br>`list_documents(limit)` → newest-first list with `date`, `name`, `path`, `size`, `mtime`, `format`<br>`_emit_document_store(event_name, *, ok, **data)` — mirror `_emit_image_store` exactly |
| Notes | `image_store` writes `.png` bytes; `document_store` writes either text (for `.md`) or bytes (for `.docx`/`.pdf`). The `bytes_or_text` param should accept `Union[bytes, str]` and encode to UTF-8 for `.md`. | `document_store.py` does **not** exist yet (`find_file_by_name` returned nothing). |
| Phase | 1 |

---

## 10. HUD serving + URL attachment — documents

### 10a. `_attach_document_url` (clone of `_attach_image_urls`)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/server.py` | `hud/server.py` |
| Lines | `71-105` (`_attach_image_urls`) | Add new function after `_attach_image_urls` |
| Current image path | `_IMAGE_ROOT = GUARDIAN_STATE_ROOT / "images"` (`68`); `_to_url` maps `guardian/state/images/<date>/<name>.png` → `/api/images/<date>/<name>.png` | New `_DOCUMENT_ROOT = GUARDIAN_STATE_ROOT / "documents"` (add near `_IMAGE_ROOT` at `68`). New `_attach_document_url(result)` — identical logic but `_DOCUMENT_ROOT` and prefix `/api/documents/`. |
| Phase | 1 |

### 10b. `GET /api/documents/{date}/{name}` — serve with `Content-Disposition: attachment`

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/server.py` | `hud/server.py` |
| Lines | `3053-3111` (`api_images_get`) | Add after `3111` (after `api_images_list`) |
| Current image path | `@app.get("/api/images/{date}/{name}")` (`3053`); `FileResponse(str(path), media_type="image/png")` (`3111`) | New `@app.get("/api/documents/{date}/{name}")` — same date validation regex, same path-traversal guards, same `resolved.parents` check against `_DOCUMENT_ROOT`. **Critical difference:** return `FileResponse(str(path), media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename=\"{name}\""})` so browser treats it as a download. |
| Notes | Failure-log: mirror the `emit_phase` / `emit_data` blocks in `api_images_get` for the document endpoint. | The `Content-Disposition: attachment` header is the only semantic difference from the image endpoint. |
| Phase | 1 |

### 10c. `GET /api/documents` — list endpoint

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/server.py` | `hud/server.py` |
| Lines | `3113-3136` (`api_images_list`) | Add after `3136` |
| Current image path | `@app.get("/api/images")` (`3113`); calls `list_generated_images(limit)` (`3118`); adds `url` field (`3122`); pops `path` (`3123`); returns `{"ok": True, "count": ..., "images": items}` | New `@app.get("/api/documents")` — calls `list_documents(limit)` (from `runtime/document_store`); adds `url = f"/api/documents/{it['date']}/{it['name']}"`; pops `path`; returns `{"ok": True, "count": ..., "documents": items}`. |
| Phase | 1 |

### 10d. Chat return-path document attachment

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/server.py` | `hud/server.py` |
| Lines | `3382-3386` (image attachment after chat result) | After `3386` |
| Current image path | `img = (result or {}).get("image")` (`3384`); `if isinstance(img, dict) and img.get("path"): _attach_image_urls(img)` (`3385-3386`) | Add: `doc = (result or {}).get("document")`; `if isinstance(doc, dict) and doc.get("path"): _attach_document_url(doc)` |
| Notes | `operator_chat._response_from_episode` (`156`) already pulls `image_artifact` from `auto.outputs`. Need to add `document_artifact` pull there too. | See touch-point 11 for `_response_from_episode` change. |
| Phase | 1 |

---

## 11. `pressure_loop/operator_chat.py` — `_response_from_episode` document artifact

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `pressure_loop/operator_chat.py` | `pressure_loop/operator_chat.py` |
| Lines | `152-156` (image artifact extraction) | After `156` |
| Current image path | `image_artifact = (auto.get("outputs") or {}).get("image_artifact")` (`155`); `image = dict(image_artifact) if isinstance(image_artifact, dict) else None` (`156`); `ok = (bool(response_text) or image is not None) and not failure_kind` (`158`) | Add: `document_artifact = (auto.get("outputs") or {}).get("document_artifact")`; `document = dict(document_artifact) if isinstance(document_artifact, dict) else None`; update `ok` line: `ok = (bool(response_text) or image is not None or document is not None) and not failure_kind`; add `"document": document` to the return dict. |
| Phase | 1 |

---

## 12. `omega_interface.html` — Document panel + Created Documents library

### 12a. Document create flyout (clone of image-gen flyout)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/omega_interface.html` | `hud/omega_interface.html` |
| Lines | `16781-16836` (composer actions injection — `btnCreate` + flyout) | Extend the composer actions block |
| Current image path | `btnCreate` title: `"Generate an image..."` (`16784`); flyout label: `"image · prompt"` (`16807`); textarea placeholder: `"a calligraphic sigil..."` (`16810`); calls `window.OmegaUI.createImage(p)` (`16830`) | Add a **second** flyout or convert the single `btnCreate` into a split button / two buttons (`create image` / `create document`). Minimal Phase 1: add `btnCreateDoc` next to `btnCreate`; new flyout label `"document · prompt"`; add `<select>` for format (`md`/`docx`/`pdf`, default `md`) and template (Phase 2); placeholder `"meeting minutes from the last standup..."`; on submit, call `window.OmegaUI.createDocument(p, {format, template, upload_ref})`. |
| Notes | The `btnCreate` event listener toggles the flyout (`16824`). A document sibling needs its own listener or a unified flyout with mode tabs. | The `window.OmegaUI.createDocument` function does not exist yet — it must be added mirroring `createImage` (`17466`). |
| Phase | Phase 1 (basic flyout: prompt + format dropdown); Phase 2 (template dropdown + upload_ref) |

### 12b. `window.Build.generateDocument` (clone of `window.Build.generate`)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/omega_interface.html` | `hud/omega_interface.html` |
| Lines | `15975-16004` (`window.Build.generate`) | Add `window.Build.generateDocument(prompt, opts)` after `window.Build.generate` |
| Current image path | Posts `{text: prompt, intent: "IMAGE_CREATE"}` to `/api/chat` (`15984`); returns `normalized` | New `generateDocument`: posts `{text: prompt, intent: "DOCUMENT_CREATE", format: opts.format, template: opts.template, upload_ref: opts.upload_ref}`; returns normalized document result (no `normalizeImageResult` — document result is already flat). |
| Phase | 1 |

### 12c. `window.OmegaUI.createDocument` (clone of `createImage`)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/omega_interface.html` | `hud/omega_interface.html` |
| Lines | `17466-17477` (`createImage`) | Add `createDocument(prompt, opts)` after `createImage` |
| Current image path | Calls `window.Build.generate(prompt, {render: false})` (`17469`); renders via `appendImageMessage(result)` (`17473`) | New `createDocument`: calls `window.Build.generateDocument(prompt, {render: false, ...opts})`; renders via new `appendDocumentMessage(result)` (renders a download-link card, not an image). |
| Phase | 1 |

### 12d. `appendDocumentMessage` (new, analogous to `appendImageMessage`)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/omega_interface.html` | `hud/omega_interface.html` |
| Lines | `9589-9636` (`appendImageMessage`) | Add `appendDocumentMessage(result)` after `appendImageMessage` |
| Current image path | Renders `image-card` with `<img>`, meta stats, download link, prompt footer, saved note | New `appendDocumentMessage`: renders a `document-card` div with:<br>- Document icon / title<br>- Format badge (`md`/`docx`/`pdf`)<br>- Download link (`<a href="result.url" download="result.filename">↓ download</a>`)<br>- Prompt / description footer<br>- Saved note: `"Saved to Settings → Created Documents."` |
| Phase | 1 |

### 12e. Settings: Created Documents gallery (clone of Created Images)

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | `hud/omega_interface.html` | `hud/omega_interface.html` |
| Lines | `997-1114` (CSS: `.created-images-gallery`, `.created-images-grid`, etc.) | Clone all `.created-images-*` CSS classes to `.created-documents-*` (or reuse `.created-images-*` if generic enough — verify). |
| Lines | `12576-12658` (`_renderSettingsCreatedImages`) | Add `_renderSettingsCreatedDocuments(panel)` — identical structure but fetches `/api/documents?limit=50`, renders document cards with download links instead of `<img>` thumbnails. |
| Lines | `13313` (settings nav button) | Add `<button class="settings-function" data-settings-view="created_documents">created documents<span>generated document library</span></button>` after `created_images`. |
| Lines | `13521-13522` (settings view panel) | Add `<div class="settings-view" data-settings-view-panel="created_documents"></div>` after `created_images`. |
| Lines | `13571-13578` (settings tab switch handler) | Add `if (id === 'created_documents') { ... _renderSettingsCreatedDocuments(panel) ... }` after `created_images`. |
| Phase | 1 |

---

## 13. NEW renderer module — `runtime/document_renderer.py`

| | Image source (exists) | Document sibling to build |
|---|---|---|
| File | *(none — image render happens inside portable-lite runtime socket)* | **NEW FILE:** `runtime/document_renderer.py` |
| Lines | N/A | Full new module |
| Function | `invoke_runtime_tool("image_tool", ...)` does the render remotely | Local pure-python render pipeline:<br>`render_document(markdown_text: str, fmt: str, *, title: str = "", css: Optional[str] = None) -> Union[bytes, str]`<br>1. `fmt == "md"` → return `markdown_text` (str)<br>2. `html = markdown.markdown(markdown_text, extensions=["tables", "fenced_code"])` (wrap in `<html><body>...</body></html>`)<br>3. `fmt == "docx"` → `from htmldocx import HtmlToDocx; h2d = HtmlToDocx(); doc = h2d.parse_html_string(html); buf = io.BytesIO(); doc.save(buf); return buf.getvalue()` (or python-docx fallback)<br>4. `fmt == "pdf"` → `from xhtml2pdf import pisa; buf = io.BytesIO(); pisa.CreatePDF(html, dest=buf); return buf.getvalue()` (or reportlab fallback)<br>5. Failure-log: `emit_data("runtime", "document_render_failed", ...)` on exception. |
| Notes | All pure-python per spec. No system binaries. | Cursor must validate each dependency on the Windows dist target before committing. |
| Phase | 1 |

---

## 14. Failure-log integration (consistent with existing surface)

Every failure branch emits to the bounded failure log, additively:

| Emit site | Source to clone from | New emit location | Phase |
|---|---|---|---|
| `runtime/image_store.py::_emit_image_store` (`27-40`) | `persist_generated_image` ok/fail | `runtime/document_store.py::_emit_document_store` — mirror exactly | 1 |
| `auto_loop/execute/run.py` image persist fail (`683-694`) | `emit_data("auto", "image_persist_failed", ...)` | `emit_data("auto", "document_persist_failed", ...)` in `DOCUMENT_CREATION` branch | 1 |
| `hud/server.py` `api_images_get` fail phases | `emit_phase("hud", "images_get", ...)` | `emit_phase("hud", "documents_get", ...)` in `api_documents_get` | 1 |
| `hud/server.py` `api_images_list` fail phases | `emit_data("hud", "images_list", ...)` | `emit_data("hud", "documents_list", ...)` in `api_documents_list` | 1 |
| `runtime/document_renderer.py` render fail | *(new)* | `emit_data("runtime", "document_render_failed", ...)` | 1 |
| `auto_loop/execute/run.py` DOCUMENT_CREATION branch | *(new)* | `emit_data("auto", "document_extract_failed", ...)` / `"document_fill_failed"` / `"document_render_failed"` per step | 1 |

---

## 15. Dependency validation notes

Tested on Windows (current dev environment):

| Package | Status | Notes |
|---|---|---|
| `markdown` | **MISSING** | `ModuleNotFoundError` — needs `pip install markdown` |
| `htmldocx` | **MISSING** | `ModuleNotFoundError` — needs `pip install htmldocx` |
| `xhtml2pdf` | **MISSING** | `ModuleNotFoundError` — needs `pip install xhtml2pdf` |
| `python-docx` | **MISSING** | `ModuleNotFoundError` (imported as `docx`) — needs `pip install python-docx` |
| `pypdf` | **PRESENT** | Imports cleanly; use for PDF upload text extraction |
| `reportlab` | **MISSING** | `ModuleNotFoundError` — fallback PDF engine; needs `pip install reportlab` |
| `pdfminer.six` | **NOT TESTED** | Alternative to `pypdf` for upload extraction; test if `pypdf` proves insufficient |

**Binary dependency check:** All packages above are pure-Python wheels on PyPI. No system binaries (weasyprint/pandoc/wkhtmltopdf/LibreOffice) required. **Verified per operator-locked decision #2.**

**Recommendation:** Cursor should install all missing deps into `.venv-brain` and run a 1-line import + minimal render smoke test on Windows before committing:
```python
python -c "import markdown; from markdown import markdown; print(markdown('# hello'))"
python -c "from htmldocx import HtmlToDocx; print('htmldocx ok')"
python -c "from xhtml2pdf import pisa; print('xhtml2pdf ok')"
python -c "from docx import Document; print('python-docx ok')"
python -c "from reportlab.pdfgen import canvas; print('reportlab ok')"
```

---

## 16. Phase summary

| Touch-point | Phase 1 (MVP rail) | Phase 2 (automation) | Phase 3 (polish) |
|---|---|---|---|
| ALLOWED_INTENTS | ✓ Add `DOCUMENT_CREATE` | — | — |
| `/api/chat` threading | ✓ `format`, `template`, `upload_ref` | — | — |
| `operator_chat.py` | ✓ Thread fields into `msg` | — | — |
| `global_loop/intent/run.py` | ✓ Verify generic passthrough | — | — |
| `config/meta.yaml` | ✓ Add `DOCUMENT_CREATE → DOCUMENT_CREATION` | — | — |
| `meta_loop/tool/run.py` | ✓ Add capability map + `DOCUMENT_CREATE` branch | — | Cognition-chosen template (Phase 3) |
| `config/tools.yaml` | ✓ Add `document_tool {runtime: false}` | — | — |
| `auto_loop/execute/run.py` | ✓ `DOCUMENT_CREATION` branch: generic template, md/docx/pdf render, persist | ✓ Upload extraction + 7 template skeletons | Multi-section fills, image+OCR |
| `runtime/document_store.py` | ✓ New file cloned from `image_store.py` | — | — |
| `_attach_document_url` | ✓ New function in `hud/server.py` | — | — |
| `GET /api/documents/{date}/{name}` | ✓ New endpoint with `Content-Disposition: attachment` | — | — |
| `GET /api/documents` | ✓ New list endpoint | — | — |
| `omega_interface.html` flyout | ✓ Basic prompt + format dropdown | ✓ Template dropdown + upload picker | — |
| `omega_interface.html` gallery | ✓ `created_documents` settings tab | — | — |
| `runtime/document_renderer.py` | ✓ New module: md→HTML→docx/pdf | ✓ Template CSS styling | — |
| Failure-log emits | ✓ All new paths emit per spec | — | — |
| Dependencies | ✓ `markdown`, `htmldocx`, `xhtml2pdf`, `python-docx` installed + smoke-tested | — | — |

---

## 17. Deferred / out of scope (per spec)

- Image/OCR upload extraction (deferred to later phase)
- Cognition-auto template selection (Phase 3 nicety)
- Editing / re-rendering stored docs
- Cloud delivery
- Real-time collaborative document editing

---

## 18. Files touched summary

| File | Action | Lines of interest |
|---|---|---|
| `pressure_loop/contributors.py` | Edit | `12-21` |
| `hud/server.py` | Edit + add functions/endpoints | `68`, `71-105`, `3053-3136`, `3354-3386`, plus new `_attach_document_url`, `api_documents_get`, `api_documents_list` |
| `pressure_loop/operator_chat.py` | Edit | `193-239`, `152-156` |
| `global_loop/intent/run.py` | Verify (no edit expected) | `52-66` |
| `config/meta.yaml` | Edit | `51-61` |
| `meta_loop/tool/run.py` | Edit | `9-22`, `131-136` |
| `config/tools.yaml` | Edit | After `27-30` |
| `auto_loop/execute/run.py` | Edit | Add new `elif action == "DOCUMENT_CREATION"` block after `696` |
| `runtime/document_store.py` | **Create** | Clone from `runtime/image_store.py` |
| `runtime/document_renderer.py` | **Create** | New module |
| `hud/omega_interface.html` | Edit | `997-1114`, `12576-12658`, `13313`, `13521-13522`, `13571-13578`, `15975-16004`, `16781-16836`, `17466-17477` |
