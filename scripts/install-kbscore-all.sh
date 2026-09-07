#!/bin/sh
# Install the combined Termius scoreboard into ~/.local/bin.
# Does NOT replace kbscore / kbscore-live / livescore / score.
#
#   /home/KalshiBot/scripts/install-kbscore-all.sh
#   # or from the 15m checkout:
#   /home/KalshiBot15/scripts/install-kbscore-all.sh
set -e
if command -v readlink >/dev/null 2>&1; then
  here=$(readlink -f "$0" 2>/dev/null || printf '%s\n' "$0")
else
  here="$0"
fi
scripts_dir=$(CDPATH= cd -- "$(dirname "$here")" && pwd)
root=$(CDPATH= cd -- "$scripts_dir/.." && pwd)
if [ ! -f "$scripts_dir/kbscore-all" ]; then
  echo "install-kbscore-all: missing $scripts_dir/kbscore-all" >&2
  exit 1
fi
chmod +x "$scripts_dir/kbscore-all" "$scripts_dir/install-kbscore-all.sh"
mkdir -p "$HOME/.local/bin"
ln -sfn "$scripts_dir/kbscore-all" "$HOME/.local/bin/kbscore-all"
ln -sfn "$scripts_dir/kbscore-all" "$HOME/.local/bin/scoreall"
echo "Installed combined LIVE board (15m + hourly):"
echo "  $HOME/.local/bin/kbscore-all  -> $scripts_dir/kbscore-all"
echo "  $HOME/.local/bin/scoreall     -> $scripts_dir/kbscore-all"
echo "Repo: $root"
echo "Also: python -m src.scoreboard_all"
echo "Left alone: kbscore / kbscore-live / livescore / score"
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  echo "Note: add ~/.local/bin to PATH, or alias: alias scoreall='kbscore-all'"
fi
