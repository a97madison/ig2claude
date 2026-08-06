---
name: sort-captures
description: Use when vid2learn has staged captures to file into topics (e.g. after `vid2learn.py --ig-user <name>` stages a creator's posts). Classifies each staged capture into the topic its content fits, moves it into that topic, then hands off to distill-learnings.
---

# Sort Captures

`vid2learn --ig-user` captures a creator's recent posts into
`<base>/staging/<username>/` without assigning a topic. Your job: read each staged
capture and file it into the topic its **content** belongs to, creating a new topic
only when nothing fits. Then the normal distill step folds it in.

## Inputs

A vid2learn base directory:
- `<base>/staging/<username>/captures/<id>.md` — staged capture notes (+ `assets/<id>/`)
- `<base>/staging/<username>/.state.json` — entries flagged `"sorted": false`
- `<base>/topics/<topic>/` — existing topics (read their `learnings.md` / names to
  understand what each is about)

## Steps

For each staged capture with `"sorted": false`:

1. **Read the capture** in full (caption / transcript / on-screen text / OCR).
2. **Survey existing topics.** List `<base>/topics/*/` and skim each `learnings.md`
   (and the topic folder name) so you know what buckets exist.
3. **Pick the best topic by content:**
   - If it clearly fits an existing topic, use that.
   - If it spans two, pick the dominant one (a capture is filed once).
   - If nothing fits, create a new topic with a short kebab-case name that describes
     the *subject*, not the creator (e.g. `cold-outreach`, not `theobjectionlab`).
   - If a capture is pure promo / no substance, file it into a `misc` topic or note
     it as skippable in your report. Do not force a bad fit.
4. **Move it (never copy):**
   - `git mv`-style move `captures/<id>.md` and its `assets/<id>/` folder from
     staging into `<base>/topics/<chosen>/captures/`.
   - Add an entry for `<id>` to the topic's `.state.json` with `"distilled": false`,
     carrying over `source`, `title`, `author`, `captured`, `gaps` from staging.
   - Remove the entry from the staging `.state.json` (or set `"sorted": true` if you
     prefer to keep an audit trail — but do not leave it both unsorted and moved).
5. **Report** a table: each capture id -> chosen topic (or "new: <topic>" / "skipped"),
   with a one-line reason. Then tell the user to run **distill-learnings** on the
   affected topics (or offer to do it now).

## Rules

- Classify by the capture's actual content, not the creator's username.
- Keep note and assets together; a moved note whose images are left behind is broken.
- Prefer reusing an existing topic over spawning near-duplicates
  (`sales` vs `sales-technique` vs `selling` should be one topic).
- Do not distill here. Sorting only files captures; distill-learnings synthesizes.
- Voice for any new topic names / notes follows the user's global CLAUDE.md §0.
