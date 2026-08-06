#!/usr/bin/env python3
"""vid2learn SessionStart hook — flag topics with undistilled captures.

Wire into a consuming project's .claude/settings.json so a new Claude Code session
opens already knowing there are captures waiting to be distilled:

  {
    "hooks": {
      "SessionStart": [
        { "hooks": [
            { "type": "command",
              "command": "python3 /ABS/PATH/hooks/vid2learn-pending.py /ABS/PATH/learnings" }
        ] }
      ]
    }
  }

The base dir (holding topics/) is argv[1], defaulting to ./learnings. The hook
never blocks: it prints a short context line only when something is pending and
always exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def pending_counts(base: Path) -> dict[str, int]:
    topics = base / "topics"
    out: dict[str, int] = {}
    if not topics.exists():
        return out
    for topic_dir in sorted(p for p in topics.iterdir() if p.is_dir()):
        state_path = topic_dir / ".state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        n = sum(1 for e in state.values() if isinstance(e, dict) and not e.get("distilled"))
        if n:
            out[topic_dir.name] = n
    return out


def main() -> None:
    # Consume (and ignore) any hook payload on stdin so we never block the pipe.
    try:
        sys.stdin.read()
    except Exception:  # noqa: BLE001
        pass

    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("learnings")
    pending = pending_counts(base)
    if not pending:
        sys.exit(0)

    report = ", ".join(f"{t} ({n})" for t, n in sorted(pending.items()))
    context = (
        f"vid2learn has undistilled captures pending: {report}. "
        f"Offer to run the distill-learnings skill on {base}/topics to fold them "
        f"into each topic's learnings.md."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
