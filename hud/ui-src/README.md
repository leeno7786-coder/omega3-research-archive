# HUD modular source (`hud/ui-src/`)

Incremental extraction of `hud/omega_interface.html` into panel and core modules. The FastAPI server still serves the **built** monolith at `GET /` — same-origin, no CORS changes.

## Quick live test

**HUD only** (substrate paused, real panels/API):

```powershell
.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone
```

Open `http://127.0.0.1:13310/` — pills, panels, settings, memory graph, chat composer.

**Full cognition loop** (pressure ticks + cascade):

```powershell
.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py
.\\.venv-brain\\Scripts\\python.exe boot.py
```

Boot spawns the HUD in a background thread and opens the browser when configured.

Verify bundle before commit:

```powershell
.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py --check
pytest tests/hud/test_hud_ui_modules.py -q
```

## Layout

```text
hud/ui-src/
  manifest.json       # module list + injection markers
  shell.html          # HTML head, CSS, body markup
  scripts.html        # script template with injection markers (not served)
  core/               # boot, telemetry, polling, canvas, voice/vision, memory-graph, …
  panels/             # panel modules + register.js
hud/scripts/build_hud.py
```

## Build

```powershell
.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py
```

Assembly: `shell.html` + `scripts.html` (modules injected at markers) → `hud/omega_interface.html`.

## Extraction status (complete)

All operator-facing script logic is modular under `hud/ui-src/`:

- **Bundled panels** + `register.js`
- **Core**: boot, telemetry, chat feed/wiring, polling (status/telemetry/v2 + orchestration), live-wiring (chrome/SSE/atmosphere/composer/vision), canvas-live, voice-runtime, vision-runtime, canvas + dashboards + heroes, canvas-panels, omega-ui, panel-fx, **memory-graph**
- **Shell glue** (intentionally inline in `scripts.html`): `_statusClassForTag`, `row`, `section`, LIVE WIRING IIFE wrappers

`scripts.html` holds inject markers only — no stale baked bundles.

The modular source tree is the only frontend source surface. The server serves
the generated `hud/omega_interface.html` bundle; it does not serve these modules
individually.
