# Source this from ~/.bashrc so an SSH login lands in the repo.
#   echo '. /home/KalshiBot/scripts/pi-shell.sh' >> ~/.bashrc
#
# Do not execute it (`./pi-shell.sh`) — cd would not stick. Use `.` or `source`.

# Interactive shells only (Debian .bashrc already returns early; keep this anyway).
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

export TZ=America/New_York

KALSHIBOT_ROOT="${KALSHIBOT_ROOT:-/home/KalshiBot}"

# Login terminals start in $HOME. Do not yank the cwd if you already `cd` elsewhere.
if [ -d "$KALSHIBOT_ROOT" ]; then
  case "$PWD" in
    "$HOME"|"$HOME/"|"")
      cd "$KALSHIBOT_ROOT" || return 0
      ;;
  esac
fi

if [ -f "$KALSHIBOT_ROOT/.venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  . "$KALSHIBOT_ROOT/.venv/bin/activate"
fi
