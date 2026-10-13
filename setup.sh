#!/usr/bin/env bash
# Set up usbvault on this computer (macOS, Ubuntu or EndeavourOS):
#   1. create or update the 'usbvault' conda environment from environment.yml
#   2. install the daemon (launchd on macOS, systemd on Ubuntu/EndeavourOS) with that env's Python
#
#   ./setup.sh             on the Mac, the Ubuntu PC and the EndeavourOS laptop
#   ./setup.sh --dry-run   show the service file instead of installing it
#
# The daemon does nothing until you run 'python usbvault.py first-run' once.
set -euo pipefail

ENV_NAME=usbvault
EXPECTED="$HOME/Documents/code/back-to-the-future-unix-base"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

daemon_args=("$@")

if [[ "$HERE" != "$EXPECTED" ]]; then
  echo "! This repo is at $HERE"
  echo "  (expected $EXPECTED). The daemon will run the script from where it is now;"
  echo "  if you move the folder later, run ./setup.sh again."
fi

# 'conda' is usually a shell function that only exists in interactive shells,
# so look for the real executable.
find_conda() {
  local c
  for c in "${CONDA_EXE:-}" "$(command -v conda 2>/dev/null || true)" \
           "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda" "$HOME/anaconda3/bin/conda" \
           "$HOME/mambaforge/bin/conda" "/opt/homebrew/Caskroom/miniconda/base/bin/conda" \
           "/opt/homebrew/Caskroom/miniforge/base/bin/conda" "/usr/local/Caskroom/miniconda/base/bin/conda" \
           "/opt/miniconda3/bin/conda" "/opt/anaconda/bin/conda" "/opt/conda/bin/conda"; do
    if [[ -n "$c" && -x "$c" && -f "$c" ]]; then echo "$c"; return 0; fi
  done
  return 1
}

CONDA="$(find_conda)" || {
  echo "error: conda not found. Install Miniconda or Miniforge, or set CONDA_EXE=/path/to/conda" >&2
  exit 1
}
echo "Using conda: $CONDA"

if "$CONDA" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  echo "Updating conda environment '$ENV_NAME'..."
  "$CONDA" env update -n "$ENV_NAME" -f environment.yml --prune
else
  echo "Creating conda environment '$ENV_NAME'..."
  "$CONDA" env create -f environment.yml
fi

PY="$("$CONDA" run -n "$ENV_NAME" python -c 'import sys; print(sys.executable)')"
echo "Environment Python: $PY"
"$PY" --version

"$PY" usbvault.py install-daemon --python "$PY" ${daemon_args[@]+"${daemon_args[@]}"}

echo
echo "Next (once per computer, with the VAULT USB plugged in):"
echo "  conda activate $ENV_NAME && cd $HERE"
echo "  python usbvault.py first-run --dry-run     # see everything it would do"
echo "  python usbvault.py first-run               # do it; the daemon takes over afterwards"
