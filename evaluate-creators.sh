#!/usr/bin/env bash
# Evaluate the last N days of Instagram posts for a list of creators.
# Each creator's posts are captured into learnings/staging/<handle>/, ready for the
# sort-captures skill to file into topics, then distill-learnings.
#
# Usage:  ./evaluate-creators.sh [days] [creators-file]
# Default: 30 days, ./creators.txt. Uses ./cookies.txt automatically if present.
set -euo pipefail
cd "$(dirname "$0")"

DAYS="${1:-30}"
FILE="${2:-creators.txt}"
COOKIES=()
[ -f cookies.txt ] && COOKIES=(--cookies cookies.txt)
[ -f "$FILE" ] || { echo "no creators file: $FILE"; exit 1; }

while IFS= read -r line; do
  handle="$(printf '%s' "$line" | sed 's/#.*//' | tr -d '[:space:]' | sed 's/^@//')"
  [ -z "$handle" ] && continue
  echo "==================== @$handle (last ${DAYS}d) ===================="
  python3 vid2learn.py --ig-user "$handle" --days "$DAYS" "${COOKIES[@]}" || true
  echo
done < "$FILE"

echo "Done. If any posts staged, run the sort-captures skill to file them into topics,"
echo "then distill-learnings to fold them into each topic's learnings.md."
