#!/usr/bin/env bash
# =============================================================================
# Install ryu==4.34 into the project venv.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# ryu 4.34 (the pinned version) will not build against any modern setuptools.
# Its ryu/hooks.py does:
#
#     from setuptools.command import easy_install
#     ...
#     easy_install.get_script_args
#
# `easy_install.get_script_args` was removed from setuptools long ago, so the
# build dies with:
#
#     AttributeError: module 'setuptools.command.easy_install'
#                     has no attribute 'get_script_args'
#
# Two independent fixes are applied here, and both are needed:
#   1. --no-build-isolation, so the build uses OUR pinned setuptools from the
#      venv instead of pip pulling a fresh (incompatible) one into a temp env.
#   2. A one-line patch to ryu/hooks.py making the legacy shim tolerant of the
#      missing symbol. The shim only exists for pbr's old setup_hook path,
#      which ryu does not use, so degrading it to a no-op is behaviour-preserving.
# =============================================================================
set -euo pipefail

VENV=${VENV:-.venv}
RYU_VERSION=${RYU_VERSION:-4.34}
SETUPTOOLS_PIN=${SETUPTOOLS_PIN:-69.5.1}
PY="$VENV/bin/python"
WORK="$VENV/src/ryu"
TARBALL="ryu-$RYU_VERSION.tar.gz"
URL="https://files.pythonhosted.org/packages/source/r/ryu/$TARBALL"

[[ -x "$PY" ]] || { echo "ERROR: $VENV not found." >&2; exit 1; }
mkdir -p "$VENV/src"

echo "==> pinning setuptools==$SETUPTOOLS_PIN in the venv"
"$PY" -m pip install --quiet "setuptools==$SETUPTOOLS_PIN" wheel

if [[ ! -d "$WORK" ]]; then
  echo "==> downloading ryu-$RYU_VERSION sdist"
  curl -fsSL -o "$VENV/src/$TARBALL" "$URL"
  tar -xzf "$VENV/src/$TARBALL" -C "$VENV/src"
  mv "$VENV/src/ryu-$RYU_VERSION" "$WORK"
fi

echo "==> patching ryu/hooks.py for modern setuptools"
"$PY" - "$WORK" <<'PYEOF'
import pathlib
import sys

hooks = pathlib.Path(sys.argv[1]) / "ryu" / "hooks.py"
src = hooks.read_text()
old = "    _main_module()._orig_get_script_args = easy_install.get_script_args"
new = (
    "    # PATCHED: easy_install.get_script_args was removed in setuptools>=80.\n"
    "    # This shim only guards pbr's legacy setup_hook path, which ryu does not\n"
    "    # use, so tolerating the missing symbol preserves behaviour.\n"
    "    _main_module()._orig_get_script_args = getattr(\n"
    "        easy_install, 'get_script_args', lambda *a, **k: [])"
)
if new in src:
    print("    already patched")
else:
    if old not in src:
        print("    WARNING: expected line not found; hooks.py may have changed upstream",
              file=sys.stderr)
    else:
        hooks.write_text(src.replace(old, new))
        print("    patched")
PYEOF

echo "==> installing ryu==$RYU_VERSION (no build isolation)"
"$PY" -m pip install --quiet --force-reinstall --no-deps --no-build-isolation "$WORK"

echo "==> verifying"
"$VENV/bin/ryu-manager" --version
"$PY" -c "import eventlet, ryu; print('eventlet', eventlet.__version__)"