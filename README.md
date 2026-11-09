# ig2claude — Instagram links → Claude Code memory (gap-aware)

Turns Instagram reels/posts/carousels you save into structured markdown notes
Claude Code can reference. The script and its data live in your project
directory; only one import line touches Claude Code's `~/.claude` config.

It captures four things per post when available: the **caption**, a **transcript**
of spoken audio (Instagram's own captions, or local Whisper transcription as a
fallback), and **text inside carousel images** via OCR. Whenever it can't capture
something, it tells you — live during the run, and in the note itself.

## vid2learn — videos → topic learnings

`vid2learn.py` generalizes the same capture engine from "Instagram links → flat
notes" to "any video (local files or any yt-dlp URL) → per-topic **learnings**". A
folder under `learnings/topics/<topic>/` is a topic. Each video becomes a raw capture
note, and its substance is distilled into one living, deduplicated, source-cited
`learnings.md` per topic.

Two phases: **capture** (Python) and **distill** (an agent step, the
`distill-learnings` skill, so it takes judgment you can steer).

    # capture a queued URL or a dropped file into a topic
    python3 vid2learn.py --topic sales-technique "https://youtube.com/watch?v=..."
    #   ...or drop a .mp4 into learnings/topics/sales-technique/ and run:
    python3 vid2learn.py

    # pull an Instagram creator's last 30 days into staging (then sort-captures)
    python3 vid2learn.py --ig-user theobjectionlab --days 30 --cookies cookies.txt

Capture prints a `pending distill:` report; a SessionStart hook
(`hooks/vid2learn-pending.py <base>`) surfaces the same to any new Claude session.

Three agent steps ship as skills (installed to `~/.claude/skills/`, copies in
`skills/`):

- **distill-learnings** — fold undistilled captures into each topic's `learnings.md`.
- **sort-captures** — file `--ig-user` staged captures into the topic their content fits.
- **apply-learnings** — edit a target project's files with the learnings relevant to it.

See `docs/2026-07-25-vid2learn-design.md` for the full design.

---

## Setup (once)

    ./install.sh          # installs yt-dlp, faster-whisper, pytesseract, Pillow
                          # and tells you how to get ffmpeg + tesseract

Then wire the knowledge file into Claude Code:

    cp INSTAGRAM.md ~/.claude/INSTAGRAM.md

Add one line to `~/.claude/CLAUDE.md`:

    @~/.claude/INSTAGRAM.md

After your first run the tool prints the exact index path — paste it into
`~/.claude/INSTAGRAM.md`, replacing the placeholder.

## Daily use

1. In Instagram: share the post to yourself → copy link (or long-press → copy link).
2. From your project directory, paste links into `ig-memory/inbox.txt`, one per line.
3. Run:

       python3 ig2claude.py

Each new link becomes a note under `ig-memory/`; the index updates; re-runs skip
anything already saved. To pull links out of a DM thread instead of pasting
them, see "Pulling links from a DM thread" below.

### Telling Claude how to use a post

Add a directive after a link with `|` to say how that post should be used. It is
stored in the note (a **How to use** section) and shown in the index, so Claude
applies the post the way you intended:

       https://www.instagram.com/reel/XXXX/ | recipe; use when I ask what to cook
       https://www.instagram.com/p/YYYY/ | gift idea for mom; remind me before her birthday

The directive is optional; links without one work exactly as before.

Pass links directly instead of using the inbox:

       python3 ig2claude.py "https://www.instagram.com/reel/XXXX/"

## Gap notifications

The pipeline never fails silently. Any shortfall on a post is:

- **printed live** during the run, prefixed `GAP` (e.g. `GAP  ABC  no_transcript: ...`),
- written into that note's **## Gaps** section,
- flagged `[partial]` next to the post in the index.

At the start of every run a **preflight check** reports which capabilities are
available. Run it on its own any time:

       python3 ig2claude.py --doctor

## Flags

    --cookies cookies.txt   reach posts that require login (export via a browser
                            extension; the tool never sees your password)
    --from-dm THREAD        pull shared post/reel/carousel links from a DM thread.
                            THREAD is the other account's username, the numeric
                            thread id, or an instagram.com/direct/t/<id>/ URL
    --from-export PATH      read that thread from an Instagram "Download your
                            information" folder or .zip (use with --from-dm).
                            Offline. Best path for the first big backfill
    --dm-limit N            take at most N new links from the thread this run
                            (newest first). Re-run to continue
    --dry-run               with --from-dm or --from-export, print the URLs and
                            exit. Does not capture, does not change the inbox,
                            and does not move the resume cursor
    --no-transcribe         skip Whisper audio transcription (faster)
    --no-ocr                skip OCR of carousel images (faster)
    --mem-dir PATH          store notes somewhere other than ./ig-memory
    --doctor                check dependencies and exit

## Pulling links from a DM thread

Save learning posts by sending them in a DM between your main account and a
second account. The script collects post, reel, and carousel links from that
thread (shared media and plain-text permalinks) and runs them through the same
capture flow as the inbox, including gap reporting. Links already saved in the
memory folder are skipped. Newest shares come first.

Opaque `instagram.com/share/...` links that hide the shortcode are skipped.
Story replies are not posts, so they are skipped too, unless the message text
itself contains a post or reel URL.

### Live session (`--from-dm`)

Uses the Netscape cookies file from `--cookies` to page Instagram's web DM
endpoints. It does not ask for or store your password. Pages are small, with
about 4 to 8 seconds of jitter between them. On HTTP 429 or a rate-limit
response it waits longer (starting around 45 seconds) and then stops, leaving
a cursor in `<mem-dir>/.dm-state.json`. Re-run the same command to continue.
`--dm-limit` caps how many new links this run will take.

    python3 ig2claude.py --from-dm learningacct --cookies cookies.txt
    python3 ig2claude.py --from-dm learningacct --cookies cookies.txt --dm-limit 30
    python3 ig2claude.py --from-dm learningacct --cookies cookies.txt --dry-run

`--dry-run` still talks to Instagram when you use `--from-dm` without
`--from-export`, but it only prints URLs.

### Export (`--from-export`)

Offline, and the right way to do the first long backfill. In Instagram, use
Download your information (JSON). Point `--from-export` at the unzipped folder
or the `.zip`, and `--from-dm` at the other account's username or the thread
folder name (`messages/inbox/<thread>/message_*.json`). Export text is stored
with a latin-1 / UTF-8 mixup; the script fixes that before it reads names and
links.

    python3 ig2claude.py --from-export ~/Downloads/instagram-export --from-dm learningacct --dry-run
    python3 ig2claude.py --from-export ~/Downloads/instagram-export.zip --from-dm learningacct

## Handling rate limits / login

Instagram throttles heavy scraping and increasingly requires login even for
public posts. If you see `login_or_rate_limit` gaps: run fewer links at a time,
wait, and/or supply `--cookies`. To make a cookies file, use a browser extension
that exports cookies in Netscape format while logged in to Instagram.

`--from-dm` already waits between inbox pages and backs off when Instagram
rate-limits the thread request. If a run stops early, re-run it: the cursor
file keeps the place, and links queued before the stop stay in the inbox.

## Scope / limits

- Transcription and OCR run **locally**; nothing about your posts leaves your machine.
- Whisper's model downloads once on first use (needs internet that one time).
- The memory folder is created relative to where you run the script — run it from
  the project root, or pin a location with `--mem-dir`.
