#!/bin/sh
# Install Termius scoreboard wrappers into ~/.local/bin for the Pi.
# Safe to run from either checkout after git pull:
#   /home/KalshiBot15/scripts/install-pi-scoreboards.sh
#   /home/KalshiBot/scripts/install-pi-scoreboards.sh
#
# Combined boards read both:
#   KALSHIBOT15_ROOT=/home/KalshiBot15   (15m journals / .env)
#   KALSHIBOT_ROOT=/home/KalshiBot       (hourly journals / .env)
set -e
if command -v readlink >/dev/null 2>&1; then
  here=$(readlink -f "$0" 2>/dev/null || printf '%s\n' "$0")
else
  here="$0"
fi
scripts_dir=$(CDPATH= cd -- "$(dirname "$here")" && pwd)
root=$(CDPATH= cd -- "$scripts_dir/.." && pwd)
if [ ! -f "$root/src/scoreboard.py" ]; then
  echo "install-pi-scoreboards: cannot find src/scoreboard.py next to $scripts_dir" >&2
  exit 1
fi

chmod +x \
  "$scripts_dir/kb-scoreboard" \
  "$scripts_dir/kbscore" \
  "$scripts_dir/kbscore-live" \
  "$scripts_dir/kbscore-hourly" \
  "$scripts_dir/kbscore-hourly-live" \
  "$scripts_dir/scoreall" \
  "$scripts_dir/livescore-all" \
  "$root/kb" \
  "$root/kb15"

mkdir -p "$HOME/.local/bin"

# Drop leftover *files* from older one-off copies, then symlink.
rm -f \
  "$HOME/.local/bin/kbscore" \
  "$HOME/.local/bin/score" \
  "$HOME/.local/bin/kbscore-live" \
  "$HOME/.local/bin/livescore" \
  "$HOME/.local/bin/kbscore-hourly" \
  "$HOME/.local/bin/score-hourly" \
  "$HOME/.local/bin/hscore" \
  "$HOME/.local/bin/kbscore-hourly-live" \
  "$HOME/.local/bin/livescore-hourly" \
  "$HOME/.local/bin/hlivescore" \
  "$HOME/.local/bin/scoreall" \
  "$HOME/.local/bin/score-all" \
  "$HOME/.local/bin/livescore-all"

ln -sfn "$scripts_dir/kbscore" "$HOME/.local/bin/kbscore"
ln -sfn "$scripts_dir/kbscore" "$HOME/.local/bin/score"
ln -sfn "$scripts_dir/kbscore-live" "$HOME/.local/bin/kbscore-live"
ln -sfn "$scripts_dir/kbscore-live" "$HOME/.local/bin/livescore"
ln -sfn "$scripts_dir/kbscore-hourly" "$HOME/.local/bin/kbscore-hourly"
ln -sfn "$scripts_dir/kbscore-hourly" "$HOME/.local/bin/score-hourly"
ln -sfn "$scripts_dir/kbscore-hourly" "$HOME/.local/bin/hscore"
ln -sfn "$scripts_dir/kbscore-hourly-live" "$HOME/.local/bin/kbscore-hourly-live"
ln -sfn "$scripts_dir/kbscore-hourly-live" "$HOME/.local/bin/livescore-hourly"
ln -sfn "$scripts_dir/kbscore-hourly-live" "$HOME/.local/bin/hlivescore"
ln -sfn "$scripts_dir/scoreall" "$HOME/.local/bin/scoreall"
ln -sfn "$scripts_dir/scoreall" "$HOME/.local/bin/score-all"
ln -sfn "$scripts_dir/livescore-all" "$HOME/.local/bin/livescore-all"

echo "Installed Termius boards (paper and live never mix):"
echo "  score / kbscore                 15m paper"
echo "  livescore / kbscore-live        15m live cash"
echo "  score-hourly / hscore           hourly paper"
echo "  livescore-hourly / hlivescore   hourly live cash"
echo "  scoreall                        combined paper (15m + hourly)"
echo "  livescore-all                   combined live (15m + hourly)"
echo "Repo: $root"
echo "15m artifacts:    \${KALSHIBOT15_ROOT:-/home/KalshiBot15}"
echo "hourly artifacts: \${KALSHIBOT_ROOT:-/home/KalshiBot}"
echo "Also: ./kb15 score|livescore   and   ./kb score|livescore"
echo "Docs: docs/scoreboards.md"
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$HOME/.local/bin"; then
  echo "Note: add ~/.local/bin to PATH if Termius does not find score / livescore."
fi
