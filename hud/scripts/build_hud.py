"""Bundle hud/ui-src panel modules into hud/omega_interface.html.

Usage (from repo root):

    .\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py
    .\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py --check   # exit 1 if bundle is stale

Assembly order:
  1. shell.html → HTML/CSS/body markup
  2. script template (omega_interface.html with <!-- HUD_UI_SRC:SHELL --> marker)
  3. Inject canvas, register, core slots, panels between marker comments
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
MANIFEST = UI_SRC / "manifest.json"
SHELL = UI_SRC / "shell.html"
SCRIPT_TEMPLATE = UI_SRC / "scripts.html"
MONOLITH = ROOT / "hud" / "omega_interface.html"

_SHELL_MARKER = "<!-- HUD_UI_SRC:SHELL -->"

_BUILD_STAMP_RE = re.compile(
    r"<!-- hud/ui-src build: [^>]+ -->",
    re.MULTILINE,
)
_PANEL_BUILT_RE = re.compile(
    r"// ── hud/ui-src panels \(built [^)]+\) ──",
    re.MULTILINE,
)
_REGISTER_BUILT_RE = re.compile(
    r"// ── hud/ui-src register \(built [^)]+\) ──",
    re.MULTILINE,
)
_CORE_BUILT_RE = re.compile(
    r"// ── hud/ui-src core \(built [^)]+\) ──",
    re.MULTILINE,
)
_CANVAS_BUILT_RE = re.compile(
    r"// ── hud/ui-src canvas \(built [^)]+\) ──",
    re.MULTILINE,
)
_TELEMETRY_BUILT_RE = re.compile(
    r"// ── hud/ui-src telemetry \(built [^)]+\) ──",
    re.MULTILINE,
)
_CHAT_FEED_BUILT_RE = re.compile(
    r"// ── hud/ui-src chat-feed \(built [^)]+\) ──",
    re.MULTILINE,
)
_CHAT_WIRING_BUILT_RE = re.compile(
    r"// ── hud/ui-src chat-wiring \(built [^)]+\) ──",
    re.MULTILINE,
)
_BOOT_BUILT_RE = re.compile(
    r"// ── hud/ui-src boot \(built [^)]+\) ──",
    re.MULTILINE,
)
_CANVAS_LIVE_BUILT_RE = re.compile(
    r"// ── hud/ui-src canvas-live \(built [^)]+\) ──",
    re.MULTILINE,
)
_LIVE_CHROME_BUILT_RE = re.compile(
    r"// ── hud/ui-src live-chrome \(built [^)]+\) ──",
    re.MULTILINE,
)
_LIVE_COMPOSER_BUILT_RE = re.compile(
    r"// ── hud/ui-src live-composer \(built [^)]+\) ──",
    re.MULTILINE,
)
_LIVE_SSE_BUILT_RE = re.compile(
    r"// ── hud/ui-src live-sse \(built [^)]+\) ──",
    re.MULTILINE,
)
_LIVE_ATMOS_BUILT_RE = re.compile(
    r"// ── hud/ui-src live-atmos \(built [^)]+\) ──",
    re.MULTILINE,
)
_LIVE_VISION_BUILT_RE = re.compile(
    r"// ── hud/ui-src live-vision \(built [^)]+\) ──",
    re.MULTILINE,
)
_VOICE_RUNTIME_BUILT_RE = re.compile(
    r"// ── hud/ui-src voice-runtime \(built [^)]+\) ──",
    re.MULTILINE,
)
_VISION_RUNTIME_BUILT_RE = re.compile(
    r"// ── hud/ui-src vision-runtime \(built [^)]+\) ──",
    re.MULTILINE,
)
_CANVAS_PANELS_BUILT_RE = re.compile(
    r"// ── hud/ui-src canvas-panels \(built [^)]+\) ──",
    re.MULTILINE,
)
_VISION_FACEWATCH_BUILT_RE = re.compile(
    r"// ── hud/ui-src vision-facewatch \(built [^)]+\) ──",
    re.MULTILINE,
)
_VOICE_KOKORO_BUILT_RE = re.compile(
    r"// ── hud/ui-src voice-kokoro \(built [^)]+\) ──",
    re.MULTILINE,
)
_VOICE_STT_BUILT_RE = re.compile(
    r"// ── hud/ui-src voice-stt \(built [^)]+\) ──",
    re.MULTILINE,
)
_VOICE_WAKE_BUILT_RE = re.compile(
    r"// ── hud/ui-src voice-wake \(built [^)]+\) ──",
    re.MULTILINE,
)
_OMEGA_UI_BUILT_RE = re.compile(
    r"// ── hud/ui-src omega-ui \(built [^)]+\) ──",
    re.MULTILINE,
)
_PANEL_FX_BUILT_RE = re.compile(
    r"// ── hud/ui-src panel-fx \(built [^)]+\) ──",
    re.MULTILINE,
)
_MEMORY_GRAPH_BUILT_RE = re.compile(
    r"// ── hud/ui-src memory-graph \(built [^)]+\) ──",
    re.MULTILINE,
)
_SIDE_DRAWER_BUILT_RE = re.compile(
    r"// ── hud/ui-src side-drawer \(built [^)]+\) ──",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^// SECTION:(\w+)\s*$", re.MULTILINE)

_CORE_INJECT_SLOTS: dict[str, tuple[str, str, str, str]] = {
    "boot": (
        "boot_inject_marker_start",
        "boot_inject_marker_end",
        "boot_inject_indent",
        "boot",
    ),
    "default": (
        "core_inject_marker_start",
        "core_inject_marker_end",
        "core_inject_indent",
        "core",
    ),
    "telemetry": (
        "telemetry_inject_marker_start",
        "telemetry_inject_marker_end",
        "telemetry_inject_indent",
        "telemetry",
    ),
    "chat_feed": (
        "chat_feed_inject_marker_start",
        "chat_feed_inject_marker_end",
        "chat_feed_inject_indent",
        "chat-feed",
    ),
    "chat_wiring": (
        "chat_wiring_inject_marker_start",
        "chat_wiring_inject_marker_end",
        "chat_wiring_inject_indent",
        "chat-wiring",
    ),
    "canvas_live": (
        "canvas_live_inject_marker_start",
        "canvas_live_inject_marker_end",
        "canvas_live_inject_indent",
        "canvas-live",
    ),
    "live_chrome": (
        "live_chrome_inject_marker_start",
        "live_chrome_inject_marker_end",
        "live_chrome_inject_indent",
        "live-chrome",
    ),
    "live_composer": (
        "live_composer_inject_marker_start",
        "live_composer_inject_marker_end",
        "live_composer_inject_indent",
        "live-composer",
    ),
    "live_sse": (
        "live_sse_inject_marker_start",
        "live_sse_inject_marker_end",
        "live_sse_inject_indent",
        "live-sse",
    ),
    "live_atmos": (
        "live_atmos_inject_marker_start",
        "live_atmos_inject_marker_end",
        "live_atmos_inject_indent",
        "live-atmos",
    ),
    "live_vision": (
        "live_vision_inject_marker_start",
        "live_vision_inject_marker_end",
        "live_vision_inject_indent",
        "live-vision",
    ),
    "voice_runtime": (
        "voice_runtime_inject_marker_start",
        "voice_runtime_inject_marker_end",
        "voice_runtime_inject_indent",
        "voice-runtime",
    ),
    "vision_runtime": (
        "vision_runtime_inject_marker_start",
        "vision_runtime_inject_marker_end",
        "vision_runtime_inject_indent",
        "vision-runtime",
    ),
    "canvas_panels": (
        "canvas_panels_inject_marker_start",
        "canvas_panels_inject_marker_end",
        "canvas_panels_inject_indent",
        "canvas-panels",
    ),
    "vision_facewatch": (
        "vision_facewatch_inject_marker_start",
        "vision_facewatch_inject_marker_end",
        "vision_facewatch_inject_indent",
        "vision-facewatch",
    ),
    "voice_kokoro": (
        "voice_kokoro_inject_marker_start",
        "voice_kokoro_inject_marker_end",
        "voice_kokoro_inject_indent",
        "voice-kokoro",
    ),
    "voice_stt": (
        "voice_stt_inject_marker_start",
        "voice_stt_inject_marker_end",
        "voice_stt_inject_indent",
        "voice-stt",
    ),
    "voice_wake": (
        "voice_wake_inject_marker_start",
        "voice_wake_inject_marker_end",
        "voice_wake_inject_indent",
        "voice-wake",
    ),
    "omega_ui": (
        "omega_ui_inject_marker_start",
        "omega_ui_inject_marker_end",
        "omega_ui_inject_indent",
        "omega-ui",
    ),
    "panel_fx": (
        "panel_fx_inject_marker_start",
        "panel_fx_inject_marker_end",
        "panel_fx_inject_indent",
        "panel-fx",
    ),
    "memory_graph": (
        "memory_graph_inject_marker_start",
        "memory_graph_inject_marker_end",
        "memory_graph_inject_indent",
        "memory-graph",
    ),
    "side_drawer": (
        "side_drawer_inject_marker_start",
        "side_drawer_inject_marker_end",
        "side_drawer_inject_indent",
        "side-drawer",
    ),
}


def _normalize_for_check(html: str) -> str:
    """Strip volatile build timestamps so --check is deterministic."""
    html = _BUILD_STAMP_RE.sub("<!-- hud/ui-src build: CHECK -->", html)
    html = _PANEL_BUILT_RE.sub("// ── hud/ui-src panels (built CHECK) ──", html)
    html = _REGISTER_BUILT_RE.sub("// ── hud/ui-src register (built CHECK) ──", html)
    html = _CORE_BUILT_RE.sub("// ── hud/ui-src core (built CHECK) ──", html)
    html = _CANVAS_BUILT_RE.sub("// ── hud/ui-src canvas (built CHECK) ──", html)
    html = _TELEMETRY_BUILT_RE.sub("// ── hud/ui-src telemetry (built CHECK) ──", html)
    html = _CHAT_FEED_BUILT_RE.sub("// ── hud/ui-src chat-feed (built CHECK) ──", html)
    html = _CHAT_WIRING_BUILT_RE.sub("// ── hud/ui-src chat-wiring (built CHECK) ──", html)
    html = _BOOT_BUILT_RE.sub("// ── hud/ui-src boot (built CHECK) ──", html)
    html = _CANVAS_LIVE_BUILT_RE.sub("// ── hud/ui-src canvas-live (built CHECK) ──", html)
    html = _LIVE_CHROME_BUILT_RE.sub("// ── hud/ui-src live-chrome (built CHECK) ──", html)
    html = _LIVE_COMPOSER_BUILT_RE.sub("// ── hud/ui-src live-composer (built CHECK) ──", html)
    html = _LIVE_SSE_BUILT_RE.sub("// ── hud/ui-src live-sse (built CHECK) ──", html)
    html = _LIVE_ATMOS_BUILT_RE.sub("// ── hud/ui-src live-atmos (built CHECK) ──", html)
    html = _LIVE_VISION_BUILT_RE.sub("// ── hud/ui-src live-vision (built CHECK) ──", html)
    html = _VOICE_RUNTIME_BUILT_RE.sub("// ── hud/ui-src voice-runtime (built CHECK) ──", html)
    html = _VISION_RUNTIME_BUILT_RE.sub("// ── hud/ui-src vision-runtime (built CHECK) ──", html)
    html = _CANVAS_PANELS_BUILT_RE.sub("// ── hud/ui-src canvas-panels (built CHECK) ──", html)
    html = _VISION_FACEWATCH_BUILT_RE.sub(
        "// ── hud/ui-src vision-facewatch (built CHECK) ──", html
    )
    html = _VOICE_KOKORO_BUILT_RE.sub("// ── hud/ui-src voice-kokoro (built CHECK) ──", html)
    html = _VOICE_STT_BUILT_RE.sub("// ── hud/ui-src voice-stt (built CHECK) ──", html)
    html = _VOICE_WAKE_BUILT_RE.sub("// ── hud/ui-src voice-wake (built CHECK) ──", html)
    html = _OMEGA_UI_BUILT_RE.sub("// ── hud/ui-src omega-ui (built CHECK) ──", html)
    html = _PANEL_FX_BUILT_RE.sub("// ── hud/ui-src panel-fx (built CHECK) ──", html)
    html = _MEMORY_GRAPH_BUILT_RE.sub("// ── hud/ui-src memory-graph (built CHECK) ──", html)
    html = _SIDE_DRAWER_BUILT_RE.sub("// ── hud/ui-src side-drawer (built CHECK) ──", html)
    return html


def _load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _indent_block(body: str, spaces: int) -> str:
    prefix = " " * spaces
    lines: list[str] = []
    for ln in body.splitlines():
        lines.append(f"{prefix}{ln}" if ln else "")
    return "\n".join(lines).rstrip() + "\n"


def _split_live_wiring_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for ln in body.splitlines():
        m = _SECTION_RE.match(ln.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections.setdefault(current, []).append(ln)
    return {k: "\n".join(v).rstrip() + "\n" for k, v in sections.items()}


def _core_module_body(entry: dict, path: Path) -> str:
    body = path.read_text(encoding="utf-8").rstrip()
    section = entry.get("section")
    if not section:
        return body
    sections = _split_live_wiring_sections(body)
    if section not in sections:
        raise SystemExit(f"Missing SECTION:{section} in {path} (entry {entry.get('id')!r})")
    return sections[section].rstrip()


def _core_slot_bundle(manifest: dict, entries: list[dict], label: str, indent: int) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"{' ' * indent}// ── hud/ui-src {label} (built {stamp}) ──",
    ]
    for entry in entries:
        rel = entry["file"]
        path = UI_SRC / rel
        if not path.is_file():
            raise SystemExit(f"Missing core module: {path}")
        lines.append(f"{' ' * indent}// @module {rel}")
        body = _core_module_body(entry, path)
        lines.append(_indent_block(body, indent).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _core_bundles_by_slot(manifest: dict) -> dict[str, str]:
    grouped: dict[str, list[dict]] = {}
    for entry in manifest.get("core", []):
        slot = entry.get("inject", "default")
        if slot not in _CORE_INJECT_SLOTS:
            raise SystemExit(f"Unknown core inject slot {slot!r} for {entry.get('file')}")
        grouped.setdefault(slot, []).append(entry)

    bundles: dict[str, str] = {}
    for slot, entries in grouped.items():
        _start_key, _end_key, indent_key, label = _CORE_INJECT_SLOTS[slot]
        indent = int(manifest.get(indent_key, manifest.get("core_inject_indent", 6)))
        bundles[slot] = _core_slot_bundle(manifest, entries, label, indent)
    return bundles


def _canvas_bundle(manifest: dict) -> str:
    lines: list[str] = [
        "    // ── hud/ui-src canvas "
        f"(built {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}) "
        "──",
    ]
    for entry in manifest.get("canvas", []):
        rel = entry["file"]
        path = UI_SRC / rel
        if not path.is_file():
            raise SystemExit(f"Missing canvas module: {path}")
        lines.append(f"    // @module {rel}")
        body = path.read_text(encoding="utf-8").rstrip()
        for ln in body.splitlines():
            lines.append(f"    {ln}" if ln else "")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _register_bundle() -> str:
    register = UI_SRC / "panels" / "register.js"
    if not register.is_file():
        raise SystemExit(f"Missing register module: {register}")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = [
        f"    // ── hud/ui-src register (built {stamp}) ──",
        "    // @module panels/register.js",
    ]
    for ln in register.read_text(encoding="utf-8").splitlines():
        lines.append(f"    {ln}" if ln else "")
    return "\n".join(lines).rstrip() + "\n"


def _panel_bundle(manifest: dict) -> str:
    lines: list[str] = [
        "    // ── hud/ui-src panels "
        f"(built {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}) "
        "──",
    ]
    for entry in manifest.get("panels", []):
        rel = entry["file"]
        path = UI_SRC / rel
        if not path.is_file():
            raise SystemExit(f"Missing panel module: {path}")
        lines.append(f"    // @module {rel}")
        body = path.read_text(encoding="utf-8").rstrip()
        for ln in body.splitlines():
            lines.append(f"    {ln}" if ln else "")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _replace_injection_block(
    html: str,
    manifest: dict,
    *,
    start_key: str,
    end_key: str,
    bundle: str,
    label: str,
    end_indent: int | None = None,
) -> str:
    start = manifest[start_key]
    end = manifest[end_key]
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )
    end_line = f"{' ' * end_indent}{end}" if end_indent else end
    block = f"{start}\n{bundle}{end_line}"
    if not pattern.search(html):
        raise SystemExit(
            f"{label} injection markers not found in script template. Expected {start!r} … {end!r}"
        )
    return pattern.sub(lambda _m: block, html, count=1)


def _upsert_build_stamp(html: str) -> str:
    stamp = (
        f"<!-- hud/ui-src build: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} -->"
    )
    if _BUILD_STAMP_RE.search(html):
        return _BUILD_STAMP_RE.sub(stamp, html, count=1)
    return html.replace('<html lang="en">', f'<html lang="en">\n  {stamp}', 1)


def _load_script_template() -> str:
    if not SCRIPT_TEMPLATE.is_file():
        raise SystemExit(f"Missing script template: {SCRIPT_TEMPLATE}")
    text = SCRIPT_TEMPLATE.read_text(encoding="utf-8")
    if _SHELL_MARKER not in text:
        raise SystemExit(
            f"Script template must contain {_SHELL_MARKER!r} — run phase-10 shell splice first"
        )
    return text.split(_SHELL_MARKER, 1)[1].lstrip("\n")


def _assemble_shell() -> str:
    if not SHELL.is_file():
        raise SystemExit(f"Missing shell: {SHELL}")
    return SHELL.read_text(encoding="utf-8").rstrip() + "\n"


def bundle(*, check: bool = False) -> int:
    if not MANIFEST.is_file():
        raise SystemExit(f"Missing manifest: {MANIFEST}")

    manifest = _load_manifest()
    core_bundles = _core_bundles_by_slot(manifest)
    canvas_bundle = _canvas_bundle(manifest)
    register_bundle = _register_bundle()
    panel_bundle = _panel_bundle(manifest)

    next_html = _assemble_shell() + _load_script_template()

    if manifest.get("canvas"):
        next_html = _replace_injection_block(
            next_html,
            manifest,
            start_key="canvas_inject_marker_start",
            end_key="canvas_inject_marker_end",
            bundle=canvas_bundle,
            label="Canvas",
            end_indent=4,
        )
    next_html = _replace_injection_block(
        next_html,
        manifest,
        start_key="register_inject_marker_start",
        end_key="register_inject_marker_end",
        bundle=register_bundle,
        label="Register",
        end_indent=4,
    )
    for slot, bundle_text in core_bundles.items():
        start_key, end_key, indent_key, label = _CORE_INJECT_SLOTS[slot]
        next_html = _replace_injection_block(
            next_html,
            manifest,
            start_key=start_key,
            end_key=end_key,
            bundle=bundle_text,
            label=label.capitalize(),
            end_indent=int(manifest.get(indent_key, manifest.get("core_inject_indent", 6))),
        )
    next_html = _replace_injection_block(
        next_html,
        manifest,
        start_key="inject_marker_start",
        end_key="inject_marker_end",
        bundle=panel_bundle,
        label="Panel",
    )
    next_html = _upsert_build_stamp(next_html)

    if check:
        current = MONOLITH.read_text(encoding="utf-8")
        if _normalize_for_check(next_html) != _normalize_for_check(current):
            print(
                "omega_interface.html is stale — run: "
                ".\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py"
            )
            return 1
        print("omega_interface.html is up to date")
        return 0

    MONOLITH.write_text(next_html, encoding="utf-8")
    core_n = len(manifest.get("core", []))
    canvas_n = len(manifest.get("canvas", []))
    panel_n = len(manifest.get("panels", []))
    print(
        f"Bundled shell + {core_n} core + {canvas_n} canvas + register + "
        f"{panel_n} panel(s) -> {MONOLITH} ({MONOLITH.stat().st_size:,} bytes)"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Bundle hud/ui-src into omega_interface.html")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the committed bundle does not match ui-src",
    )
    args = parser.parse_args()
    raise SystemExit(bundle(check=args.check))


if __name__ == "__main__":
    main()
