#!/usr/bin/env bash
# Build the Linux counterpart of Omega.exe: a one-file executable of
# scripts/omega_launcher.py, frozen with PyInstaller from the SSD's own Linux
# interpreter (AGENTS.md section 1 -- never a host Python). Mirrors
# scripts/build_launcher.ps1. Output: <product>/Omega-Linux, and a copy at the
# SSD root beside Omega.exe when the product sits one level below it.
# The wizard loads no ML stack at run time; the excludes stop PyInstaller from
# following function-level imports deep in the product into torch/scipy/etc.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$SOURCE_ROOT/python-3.13-linux/bin/python3"
BUILD_DIR="$SOURCE_ROOT/.launcher-build-linux"

if [[ ! -x "$PYTHON" ]]; then
  echo "portable Linux interpreter is missing: $PYTHON" >&2
  exit 1
fi

rm -rf "$BUILD_DIR"
trap 'rm -rf "$BUILD_DIR"' EXIT
cd "$SOURCE_ROOT"

# _tkinter carries no RUNPATH of its own: it finds libtcl9.0/libtcl9tk9.0 through
# the interpreter's $ORIGIN/../lib. Point PyInstaller's dependency resolver there,
# or the frozen wizard is built without Tcl/Tk and cannot open a window.
LD_LIBRARY_PATH="$SOURCE_ROOT/python-3.13-linux/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
"$PYTHON" -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name Omega-Linux \
  --paths "$SOURCE_ROOT" \
  --hidden-import runtime.cloud_providers \
  --hidden-import scripts.lmstudio_updater \
  --hidden-import scripts.omega_launcher_art \
  --hidden-import cryptography.hazmat.primitives.ciphers.aead \
  --exclude-module torch \
  --exclude-module torchaudio \
  --exclude-module torchvision \
  --exclude-module transformers \
  --exclude-module tokenizers \
  --exclude-module accelerate \
  --exclude-module safetensors \
  --exclude-module huggingface_hub \
  --exclude-module scipy \
  --exclude-module sklearn \
  --exclude-module pandas \
  --exclude-module numpy \
  --exclude-module numba \
  --exclude-module llvmlite \
  --exclude-module spacy \
  --exclude-module thinc \
  --exclude-module librosa \
  --exclude-module soundfile \
  --exclude-module cv2 \
  --exclude-module onnx \
  --exclude-module onnxruntime \
  --exclude-module sympy \
  --exclude-module matplotlib \
  --exclude-module pyarrow \
  --exclude-module fastapi \
  --exclude-module uvicorn \
  --exclude-module starlette \
  --exclude-module playwright \
  --exclude-module lxml \
  --exclude-module reportlab \
  --exclude-module xhtml2pdf \
  --exclude-module mypy \
  --exclude-module bandit \
  --distpath "$BUILD_DIR/dist" \
  --workpath "$BUILD_DIR/work" \
  --specpath "$BUILD_DIR" \
  scripts/omega_launcher.py

install -m 0755 "$BUILD_DIR/dist/Omega-Linux" "$SOURCE_ROOT/Omega-Linux"
SSD_ROOT="$(dirname "$SOURCE_ROOT")"
if [[ -f "$SSD_ROOT/Omega.exe" ]]; then
  install -m 0755 "$SOURCE_ROOT/Omega-Linux" "$SSD_ROOT/Omega-Linux"
fi
echo "built $SOURCE_ROOT/Omega-Linux"
