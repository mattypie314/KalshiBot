#!/bin/sh
# Install Termius scoreboard wrappers into ~/.local/bin for the Pi.
# Run from the KalshiBot15 checkout after pulling this branch / main:
#   /home/KalshiBot15/scripts/install-pi-scoreboards.sh
set -e
if command -v readlink >/dev/null 2>&1; then
  here=$(readlink -f "$0" 2>/dev/null || printf '%s\n' "$0")
else
  here="$0"
fi
scripts_dir=$(CDPATH= cd -- "$(dirname "$here")" && pwd)
root=$(CDPATH= cd -- "$scripts_dir/.." && pwd)
if [ ! -f "$root/kb15" ]; then
  echo "install-pi-scoreboards: cannot find kb15 next to $scripts_dir" >&2
  exit 1
fi
chmod +x "$scripts_dir/kbscore" "$scripts_dir/kbscore-live" "$root/kb15"
mkdir -p "$HOME/.local/bin"
ln -sfn "$scripts_dir/kbscore" "$HOME/.local/bin/kbscore"
ln -sfn "$scripts_dir/kbscore" "$HOME/.local/bin/score"
ln -sfn "$scripts_dir/kbscore-live" "$HOME/.local/bin/kbscore-live"
ln -sfn "$scripts_dir/kbscore-live" "$HOME/.local/bin/livescore"
echo "Installed Termius boards:"
echo "  $HOME/.local/bin/kbscore       -> $scripts_dir/kbscore       (paper)"
echo "  $HOME/.local/bin/score         -> $scripts_dir/kbscore"
echo "  $HOME/.local/bin/kbscore-live  -> $scripts_dir/kbscore-live  (live cash)"
echo "  $HOME/.local/bin/livescore     -> $scripts_dir/kbscore-live"
echo "Repo: $root"
echo "Also: ./kb15 score   and   ./kb15 livescore"
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  echo "Note: add ~/.local/bin to PATH if Termius does not find kbscore."
fi
