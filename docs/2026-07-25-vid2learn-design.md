# vid2learn — videos into topic learnings

**Date:** 2026-07-25
**Status:** design approved, implementing

## Problem

`ig2claude.py` turns Instagram links into per-post capture notes, flat into one
`ig-memory/` folder. It does one source (Instagram URLs) and stops at raw capture.
We want to generalize: ingest **any** video (local files or any-platform URLs),
organize by **topic** (a folder is a topic), and go past capture to **distilled
learnings** — one living, deduplicated document per topic.

## Core decisions (all settled in brainstorming)

- **Input:** both local video files and URLs (any platform yt-dlp supports).
- **Topic = folder.** A video's topic is decided entirely by which topic folder it
  lands in. Files are dropped straight into the folder; URLs go in that folder's
  `inbox.txt`. No global inbox, no per-link topic tags.
- **Output (Model A):** each video keeps its raw capture note **and** its substance
  is folded into a single `learnings.md` per topic that accumulates across videos.
- **Distiller (Model B):** a Claude Code agent, not code. Capture is automated
  Python; synthesis is an agent step you can steer mid-run. Lives as the
  `distill-learnings` skill, not a frozen prompt in the script.
- **Merge style (Model A):** `learnings.md` is a living, deduplicated synthesis. A
  new video reinforcing an existing point refines that bullet and adds its citation;
  new points are added; contradictions are flagged inline. Raw notes are never
  edited. Every learning cites its source video(s).
- **Proactive distilling (Model C):** both a capture-time offer and a SessionStart
  hook backstop, so a topic never silently accumulates undistilled captures.

## Directory layout

```
<base>/                         # e.g. learnings/
  topics/
    <topic-name>/
      inbox.txt                 # URLs queued for this topic (optional)
      *.mp4 / *.mov / *.webm    # local videos dropped straight in
      captures/
        <id>.md                 # raw per-video note (transcript/OCR/on-screen/refs)
        assets/<id>/*.png       # saved keyframes, referenced by the note
        _sources/               # local videos moved here after capture (dedup by hash)
      learnings.md              # living distilled synthesis (agent-written)
      .state.json               # per-capture record incl. distilled flag
```

The existing `ig-memory/` is untouched legacy; `ig2claude.py` keeps working as-is.

## Components

### 1. Capture (Python: `vid2learn.py`)

Reuses `ig2claude.py` primitives by import (no fork of the capture logic):
`GapLog`, `fetch_metadata`, `extract_caption_transcript`, `capture_video`,
`_whisper_from_file`, `_frames_ocr_and_save`, `ocr_images`, `follow_references`,
`build_note`.

New/adapted in `vid2learn.py`:

- **Source id.** URLs: yt-dlp's `meta["id"]` (falls back to a slug of the URL).
  Files: the filename stem, sanitized. Ids namespace the capture note, assets, and
  state key.
- **URL capture.** Any `http(s)` URL (not just Instagram). `fetch_metadata` +
  `capture_video` already work for any yt-dlp-supported site; the Instagram-specific
  `URL_RE`/`shortcode` are replaced with generic ones.
- **Local-file capture.** New: skip `fetch_metadata`; run `_whisper_from_file` +
  `_frames_ocr_and_save` directly on the file, synthesize a minimal `meta`
  (title = filename, author = `local`, no date), build the note the same way. Move
  the file into `captures/_sources/` and track it by content hash so a re-scan does
  not reprocess it.
- **Topic scan.** For each `topics/*/` folder: process every URL in `inbox.txt` plus
  every loose video file. Per-topic `captures/` and `.state.json`. Inbox pruning and
  URL dedup carry over from `ig2claude`, scoped per topic.
- **State.** `.state.json` per topic replaces the flat `.processed.json`:
  `{ "<id>": { "source": "...", "title": "...", "captured": "<iso>",
  "gaps": [...], "distilled": false } }`. Undistilled = filter `distilled == false`.
- **Pending report.** After the run, print undistilled counts per topic, e.g.
  `pending distill: sales-technique (3), ai-tooling (1)`.

### 2. Distill (`distill-learnings` skill)

Agent step, invoked at capture time or from the SessionStart offer. For a given
topic:

1. Read `.state.json`; collect captures with `distilled == false`.
2. Read those raw notes in `captures/` and the current `learnings.md`.
3. Rewrite `learnings.md` as a merged, deduplicated synthesis:
   - reinforce/refine an existing bullet (add the new `[id]` citation) rather than
     duplicate;
   - add genuinely new points;
   - flag contradictions inline (`> conflicts with [id]`);
   - every learning cites its source video id(s), linking to `captures/<id>.md`.
4. Never edit raw notes.
5. Flip the merged captures to `distilled: true` in `.state.json`.

The skill is steerable ("pull the sales angles", "tie this to Glide") — that
judgment is the reason distilling is an agent step, not code.

### 3. Proactive triggers

- **Capture-time:** `vid2learn.py` prints the pending-distill report; when run inside
  a Claude session the agent offers to distill immediately.
- **SessionStart hook:** `hooks/vid2learn-pending.py` scans `<base>/topics/*/.state.json`
  for any `distilled == false` and, if found, emits a short context line so a new
  session opens with the offer. Wired per-project via `settings.json` (the consuming
  project points the hook at its `<base>`), mirroring how `~/.claude/CLAUDE.md`
  already pulls the IG index.

## Data flow

```
drop file / queue URL in topics/<t>/  ->  vid2learn.py capture
   -> captures/<id>.md (+ assets), .state.json {distilled:false}
   -> "pending distill" report
      -> distill-learnings skill (agent)
         -> learnings.md merged synthesis, .state.json {distilled:true}
```

## Error handling

- Capture inherits `ig2claude`'s gap model: every shortfall is a `GapNotice`,
  printed live, written into the note's `## Gaps`, recorded in `.state.json`. A
  capture with gaps is still saved and still distillable.
- Local file with no audio track: whisper gap logged, frame-OCR still runs; note is
  saved. Not a failure.
- Distill is idempotent by the `distilled` flag: re-running distills only what is
  still `false`. A capture whose id is already in `.state.json` is never recaptured.

## Testing

- **Local-file capture:** generate a short silent test clip with burned-in text
  (ffmpeg `testsrc` + `drawtext`), drop into a topic, run `vid2learn.py`; assert a
  `captures/<id>.md` note, saved keyframe assets, a `.state.json` entry with
  `distilled == false`, and the pending report naming the topic.
- **Distill loop:** run the distill step on that capture; assert `learnings.md`
  exists with a cited bullet and the state entry flips to `distilled == true`.
- **Idempotency:** re-run capture; assert the file is not reprocessed (hash dedup)
  and no duplicate state entry.
- **Hook:** run `vid2learn-pending.py` against the base before and after distill;
  assert it reports the topic when undistilled and stays silent when all distilled.
- **URL path:** reused unchanged from `ig2claude` (already verified in production);
  smoke-checked separately, not part of the offline test set.

## Extensions (2026-07-28)

Added after the core shipped:

### Instagram creator intake (`--ig-user`)

`vid2learn.py --ig-user <name> [--days 30]` pulls a creator's recent posts and
stages them for sorting.

- **Enumerate (instaloader primary):** `_enum_instaloader` uses instaloader's
  `Profile.get_posts()` (newest-first, each with `.date_utc`), loading the Netscape
  `cookies.txt` into its session, and keeps posts within the `--days` window (scans up
  to `cap` posts rather than breaking on the first old one, since pinned posts sort out
  of order). This is the reliable path: gallery-dl's instagram *user* extractor is
  routinely broken by IG (returns "user could not be found" for every account).
- **Fallback (gallery-dl):** `_enum_gallerydl` is used only when instaloader is absent
  or errors with nothing to show. It follows gallery-dl's Queue-redirect message
  protocol (tag 6 -> recurse bounded depth, tag 3 -> post metadata, tag -1 -> surface
  error), dedupes, date-filters. `enumerate_ig_user` orchestrates: instaloader first,
  gallery-dl fallback, one `ig_user_empty` gap if both yield nothing.
- **Stage, don't file:** captured into `<base>/staging/<username>/captures/` with
  state `distilled:false, sorted:false`. Staging lives outside `topics/`, so it is not
  auto-distilled. Topic is decided later, by content, not by creator.
- **Rate limits:** both enumeration and per-post capture are best-effort under IG
  throttling; run small batches. Enumeration (instaloader) and capture (yt-dlp) are
  throttled independently, so enumeration can succeed while capture is rate-limited.

### `sort-captures` skill

Agent step. Reads each staged capture, surveys existing topics, and files each into
the topic its **content** fits (creating a new subject-named topic when nothing does),
moving the note + its `assets/<id>/` together and carrying the state entry into the
topic as `distilled:false`. Then the normal distill flow applies. Classify by content,
not by creator username.

### `apply-learnings` skill

Agent step. Given a target project under `~/Desktop/projects/<name>`, reads the
project (README/CLAUDE.md/notes + structure) and every `topics/*/learnings.md`,
selects only the learnings relevant to that project, and **edits the project's own
files directly** in its voice, preserving each learning's source provenance. Changes
are left **uncommitted** for the user to review (never committed, never pushed).
Relevance is judgment, not keyword match.

### Distribution

The three agent steps are skills installed to `~/.claude/skills/` (global, usable from
any project). Copies live in the repo under `skills/` for documentation and
portability. The capture engine is `vid2learn.py` + reused `ig2claude.py`. The
SessionStart hook is `hooks/vid2learn-pending.py`.

## Out of scope

- Consuming learnings into downstream projects (Glide sales docs, etc.) stays a
  manual handoff.
- Migrating existing `ig-memory/` notes into the topic structure.
- Auto-classifying a video's topic (topic is always the folder you chose).
```

