---
name: distill-learnings
description: Use when a vid2learn topic has undistilled captures (the capture run prints "pending distill", or a SessionStart hook flags it). Folds new per-video capture notes into a topic's living learnings.md, deduplicated and cited, then marks the captures distilled.
---

# Distill Learnings

You are the DISTILL phase of `vid2learn`. Capture (the Python script) has already
turned videos into raw notes. Your job: fold the undistilled ones into a topic's
`learnings.md` as a tight, deduplicated synthesis, then flip their state flags.

This is an agent step, not code, because it takes judgment. The user may steer it
("pull the sales angles", "tie this to the Glide receptionist"). Honor that intent.

## Inputs

A vid2learn base directory. Each topic lives at `<base>/topics/<topic>/` with:
- `.state.json` — `{ "<id>": { "source", "title", "captured", "gaps",
  "distilled", ... } }`
- `captures/<id>.md` — the raw per-video notes (caption / transcript / on-screen
  text / OCR / followed references)
- `learnings.md` — the living distilled doc (may not exist yet)

## Steps

Do this per topic that has any capture with `"distilled": false`.

1. **Find the work.** Read `.state.json`; collect ids where `distilled == false`.
   If none, say so and stop for that topic.
2. **Read.** Read each undistilled `captures/<id>.md` in full, and the current
   `learnings.md` if it exists.
3. **Extract, don't transcribe.** From each capture pull the *actionable substance*
   (claims, techniques, data, frameworks), not a summary of the video. Skip filler,
   hooks, and self-promotion. A capture with only a `## Gaps` section and no real
   content yields nothing — that is fine.
4. **Merge into a living doc.** Rewrite `learnings.md` so it is the deduplicated
   state of everything known on this topic:
   - A new point that **reinforces** an existing bullet strengthens/refines that
     bullet and **appends its citation** — do not add a duplicate line. A
     well-supported learning ends up with several citations.
   - Genuinely new points get added under a fitting heading.
   - **Contradictions** stay visible: keep both and flag inline, e.g.
     `> conflicts with [id2]`.
   - Every learning **cites its source(s)** as `[<id>]` linking to the note:
     `([oGmS4kQ](captures/oGmS4kQ.md))`. Multiple sources → multiple links.
   - Keep it tight and skimmable. This doc is what gets handed to a project, so it
     reads like distilled knowledge, not a changelog.
5. **Never edit the raw notes** in `captures/`. Only `learnings.md`.
6. **Mark distilled.** Set `"distilled": true` on every id you folded in (including
   ones that yielded nothing — they are processed). Save `.state.json`.
7. **Report.** Say which topics you distilled, how many captures, and the notable
   new or reinforced learnings.

## learnings.md shape

```markdown
# <topic> — learnings

_Distilled from vid2learn captures. Each point cites its source video(s)._

## <theme>

- <specific, actionable learning>. ([id1](captures/id1.md), [id2](captures/id2.md))
- <another>. ([id3](captures/id3.md))
  > conflicts with [id4] — <what differs>

## <another theme>
...
```

## Rules

- Voice follows the user's global CLAUDE.md §0: plain, specific, confident. No
  em-dashes. Nothing that reads as AI-generated.
- Distilling is idempotent: only ever touch captures still flagged `false`, so a
  re-run never re-folds or duplicates.
- Preserve source traceability above brevity — a claim with no citation is not a
  learning, it is an assertion.
- If a capture's substance is thin or looks wrong, say so in your report rather than
  laundering it into a confident bullet.
