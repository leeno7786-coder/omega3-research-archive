# DOCUMENT_CREATE — Phase 2 Spec (templates + upload extraction)

**Author:** claude · **Date:** 2026-06-08 · **Builds on:** Phase 1 (`dbb06fae` + fix `ec74f275`, gate 881, ACCEPTED).
**Predecessor docs:** `DOCUMENT_CREATE_SPEC.md` + `DOCUMENT_CREATE_IMPL_MAP.md` (the Phase-2 rows).

Phase 1 shipped the rail with the **generic** template and no upload extraction. Phase 2 adds:
(A) the 6 remaining templates, (B) upload→text extraction, (C) the HUD template dropdown + upload picker.

The plumbing already exists: `runtime/document_templates.py::load_template_parts(name)` loads any
`config/document_templates/<name>/{skeleton.md,instructions.md}` (falls back to generic), and the AUTO
branch has the extraction stub at `auto_loop/execute/run.py:722-724`. So this is mostly additive.

## A. Template keys (canonical — HUD dropdown value == directory name == loader key)

`generic` (exists), `report`, `letter`, `meeting_minutes`, `summary`, `resume`, `readme_spec`.

Create `config/document_templates/<key>/skeleton.md` + `instructions.md` for the 6 new keys, content below.
Skeletons use `{{...}}` placeholder hints; the model fills them. Instructions always start with
"Output ONLY valid Markdown." and forbid fabricating facts. The operator can edit any of these later —
they're just markdown files.

### report
**skeleton.md**
```
# {{title}}

## Executive Summary
{{1-3 sentence overview of purpose + key conclusion}}

## Background
{{context and scope}}

## Findings
{{main findings; create subsections (### ) as the content warrants}}

## Analysis
{{interpretation of the findings}}

## Recommendations
{{numbered, actionable recommendations}}

## Conclusion
{{closing summary}}
```
**instructions.md**
```
Output ONLY valid Markdown. Structure the operator's input into a formal report using the skeleton's
sections. Infer subsections under Findings from the content. Be objective and concise. Omit a section
only if the input gives nothing for it; never invent facts, numbers, or sources.
```

### letter
**skeleton.md**
```
{{Sender Name}}
{{Sender contact}}

{{Date}}

{{Recipient Name}}
{{Recipient address}}

Dear {{Recipient}},

{{body paragraphs}}

Sincerely,
{{Sender Name}}
```
**instructions.md**
```
Output ONLY valid Markdown. Turn the operator's input into a professional letter following the skeleton.
Default to a formal, courteous tone. Fill sender/recipient/date from the input if present; otherwise
leave a clear placeholder like [Sender Name]. Do not invent specific names, addresses, or dates.
```

### meeting_minutes
**skeleton.md**
```
# Meeting Minutes — {{topic}}

**Date/Time:** {{when}}
**Attendees:** {{names}}

## Agenda
{{agenda items}}

## Discussion
{{concise notes per agenda item}}

## Decisions
{{decisions made}}

## Action Items
{{- [ ] Owner — task — due date}}

## Next Meeting
{{when / topics}}
```
**instructions.md**
```
Output ONLY valid Markdown. Extract attendees, decisions, and action items from the operator's notes.
Format action items as a checklist with an owner and due date when stated. Keep discussion notes concise.
Leave a placeholder if a field is absent; do not fabricate attendees, dates, or decisions.
```

### summary
**skeleton.md**
```
# {{title}}

## TL;DR
{{1-3 sentence summary}}

## Key Points
{{- the essential points as bullets}}

## Details
{{optional expanded detail}}

## Takeaways
{{- actionable or notable takeaways}}
```
**instructions.md**
```
Output ONLY valid Markdown. Condense the operator's input. Lead with a tight TL;DR, then key points as
bullets. Preserve essential facts and numbers; drop filler. The output must be shorter than the source.
```

### resume
**skeleton.md**
```
# {{Full Name}}
{{email · phone · location · links}}

## Summary
{{2-3 sentence professional summary}}

## Experience
**{{Role}}** — {{Organization}} ({{dates}})
{{- accomplishment bullets, action-verb led}}

## Education
{{degree — institution ({{year}})}}

## Skills
{{- grouped skills}}
```
**instructions.md**
```
Output ONLY valid Markdown. Structure the operator's career info into a clean resume using the skeleton.
List experience reverse-chronologically; write accomplishment bullets led by action verbs. Infer sections
from the input; leave placeholders for missing contact fields. Do not invent employers, dates, or credentials.
```

### readme_spec
**skeleton.md**
```
# {{Project Name}}

{{one-line description}}

## Overview
{{what it is and why}}

## Features
{{- key features}}

## Installation
{{fenced code block with setup commands}}

## Usage
{{how to use, with examples}}

## Configuration
{{config options}}

## License
{{license, or [LICENSE]}}
```
**instructions.md**
```
Output ONLY valid Markdown. Structure the operator's input into a project README / spec. Use fenced code
blocks for commands and examples. Keep headings clear. Include only sections the input supports; leave a
placeholder for License if unspecified.
```

## B. Upload → text extraction (fill the stub at `auto_loop/execute/run.py:722-724`)

New helper module **`runtime/document_upload.py`**:

```python
def extract_upload_text(upload_ref: str) -> str:
    """Extract text from a staged upload under guardian/state/uploads/.
    Supports .txt/.md (direct), .docx (python-docx), .pdf (pypdf). Raises on unreadable.
    """
```
- Resolve `upload_ref` to a path under `GUARDIAN_STATE_ROOT/uploads` with the SAME path-traversal guards the
  image/upload endpoints use (reject `..`, absolute escapes; the resolved path must stay within uploads root).
  `upload_ref` is the `<kind>/<name>` the HUD got back from `POST /api/upload` (strip any `/api/uploads/` prefix).
- `.txt`/`.md` → read UTF-8. `.docx` → `from docx import Document`; join paragraph text (+ table cells).
  `.pdf` → `from pypdf import PdfReader`; join `page.extract_text()`. Image/OCR is Phase 3 — raise a clear
  "unsupported upload type for extraction" for other extensions.

Wire into the AUTO branch (replace the `pass` stub), failure-soft + emitting:
```python
source_text = prompt
if upload_ref:
    try:
        from runtime.document_upload import extract_upload_text
        extracted = extract_upload_text(str(upload_ref))
        source_text = (prompt + "\n\n" + extracted).strip() if prompt else extracted
    except Exception as exc:
        from guardian.terminal_feedback import emit_data
        emit_data("auto", "document_extract_failed", status="degraded", ok=False,
                  error=str(exc), upload_ref=str(upload_ref))
        source_text = prompt  # degrade to prompt-only; never fail the whole doc on extraction
```
Semantics: when both a prompt and an upload are given, the **prompt is the instruction** and the **upload is
the source material** (prompt first, then extracted text). Upload-only → extracted text is the source.

## C. HUD — template dropdown + upload picker (Phase-2 rows of the impl map §12a)

In the Document flyout (`hud/omega_interface.html`):
- Add a `<select>` for template with the 7 keys (friendly labels: "Generic / clean up", "Report", "Letter",
  "Meeting Minutes", "Summary", "Résumé", "README / Spec"); default `generic`.
- Add a file `<input>` (upload picker): on selection, `POST /api/upload` → take the returned `<kind>/<name>`
  (or url) as `upload_ref`. Show the chosen filename.
- `window.Build.generateDocument` already threads `template` + `upload_ref` (Phase 1) — just populate them
  from the dropdown + picker. `appendDocumentMessage` already renders the download card.

## D. Failure-log

`document_extract_failed` (above) is the one new failure path — additive `emit_data(status="degraded", ok=False)`,
consistent with the failure-only-log surface (`FAILURE_LOG_COVERAGE.md`). No other new failure paths.

## E. RED-first tests (IRON LAW)

In `tests/core/test_document_create.py` (extend), per behavior, paste the watch-it-fail:
1. **Each of the 6 templates loads** — `load_template_parts("report")` etc. returns a non-fallback skeleton
   (assert a section unique to that template appears, e.g. "Executive Summary" for report) and instructions.
2. **build_fill_prompt uses the selected template** — the fill prompt for `report` contains the report
   skeleton's headings (proves template selection actually routes, not silently falling back to generic).
3. **Extraction per format** — stage a tiny .txt, .md, .docx (python-docx), .pdf (reportlab/pypdf) under a temp
   uploads dir; `extract_upload_text` returns the known content for each; an unsupported ext raises.
4. **Path-traversal guard** — `extract_upload_text("../../etc/passwd")`-style ref is rejected.
5. **AUTO branch wiring** — a DOCUMENT_CREATION pack with an upload_ref produces a doc whose rendered content
   includes the uploaded text (end-to-end, mock the model fill to echo source_text); a bad upload_ref degrades
   to prompt-only AND emits `document_extract_failed` to `writer.FAILURE_LOG` (filter by reason).

## F. Constraints

- Additive; do not change Phase-1 behavior (generic still works; format render unchanged).
- Pure-python only (python-docx, pypdf already available; no system binaries).
- **Do NOT touch `browser_agent_extension/*`** — the operator is editing it concurrently (decoupled lane).
  Commit Phase 2 with explicit pathspecs covering only the doc-system files + the new templates/module/tests.
- Failure-soft emits, no redundant inner try/except around `emit_data`.

## Deliverable

6 template dirs + `runtime/document_upload.py` + the AUTO stub wired + the HUD dropdown/picker, all RED-first.
Full gate green (`.venv-brain\Scripts\python.exe -m pytest tests/ -o addopts="" -p no:cacheprovider -q`).
Bus reply with RED evidence per behavior + commit. Claude re-verifies (gate + induced: each template fills a
distinct structure; an uploaded .docx/.pdf's text reaches the rendered output).
