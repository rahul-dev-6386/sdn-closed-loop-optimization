#!/usr/bin/env bash
# =============================================================================
# Install Mininet into the project venv.
#
# Mininet is not on PyPI, and its setup.py installs ONLY the `mn` script -- the
# `mnexec` helper that mininet invokes to create namespaces/datapaths is only
# installed by `make install`. This script does both, and pins `mnexec` to the
# venv interpreter (its stock shebang is /usr/bin/env python, which would pick
# up the system interpreter instead of our pinned CPython 3.9).
# =============================================================================
set -euo pipefail

VENV=${VENV:-.venv}
PINNED_TAG=${MININET_TAG:-2.3.1b4}
SRC_DIR="$VENV/src/mininet"
REPO=https://github.com/mininet/mininet.git

PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: $VENV not found. Create the venv first (see README)." >&2
  exit 1
fi

mkdir -p "$VENV/src"

if [[ ! -d "$SRC_DIR/.git" ]]; then
  echo "==> cloning mininet $PINNED_TAG"
  rm -rf "$SRC_DIR"
  git clone --depth 1 --branch "$PINNED_TAG" "$REPO" "$SRC_DIR"
else
  echo "==> mininet source already present at $SRC_DIR"
fi

echo "==> installing mininet into the venv"
"$PY" -m pip install --force-reinstall --no-deps "$SRC_DIR"

# In mininet 2.3.x `mnexec` is a small C program (mnexec.c) that mininet invokes
# to create network/mount namespaces. Only `make` builds it, and only
# `make install` copies it to BINDIR -- so build the `mnexec` target directly
# (the default target runs a pyflakes `codecheck` we do not need).
echo "==> building mnexec"
if [[ ! -x "$SRC_DIR/bin/mnexec" ]]; then
  ( cd "$SRC_DIR" && make mnexec )
  # `make mnexec` drops the binary in the source root; normalise its location.
  [[ -x "$SRC_DIR/mnexec" ]] && mv "$SRC_DIR/mnexec" "$SRC_DIR/bin/mnexec"
fi

echo "==> linking mnexec into $VENV/bin"
ln -sf "$SRC_DIR/bin/mnexec" "$VENV/bin/mnexec"

# `mn` itself is normally run under sudo, and sudo resets PATH to secure_path.
# mininet/net.py:678 does a `pathCheck('mnexec', ...)` against the *root* PATH,
# so mnexec must also live in a directory that root's PATH includes. This is
# exactly what `make install` does (BINDIR=/usr/local/bin).
echo "==> installing mnexec system-wide to /usr/local/bin (for sudo/root PATH)"
sudo install -D -m 0755 "$SRC_DIR/bin/mnexec" /usr/local/bin/mnexec

echo "==> verifying"
"$VENV/bin/mn" --version
"$VENV/bin/mnexec" -h 2>&1 | head -3 || echo "   (mnexec -h unsupported, that is fine)"
echo "OK: mn and mnexec installed into $VENV/bin"
echo "REMINDER: make sure $VENV/bin is on PATH so mininet can find mnexec"