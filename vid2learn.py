#!/usr/bin/env python3
"""vid2learn — turn videos into topic learnings for Claude Code.

Generalizes ig2claude from "Instagram links -> flat notes" to "any video ->
per-topic captures + a living, distilled learnings.md per topic".

Model:
  <base>/topics/<topic>/
    inbox.txt            URLs queued for this topic (any yt-dlp platform)
    *.mp4 / *.mov ...    local videos dropped straight in
    captures/<id>.md     raw per-video note (transcript / OCR / on-screen / refs)
    captures/assets/...  saved keyframes referenced by the note
    captures/_sources/   local videos moved here after capture
    learnings.md         distilled synthesis (written by the distill-learnings skill)
    .state.json          per-capture record incl. the `distilled` flag

Two phases:
  1. CAPTURE (this script): fetch/transcribe/OCR each new video into captures/,
     record it in .state.json with distilled=false, then report what is pending.
  2. DISTILL (the distill-learnings skill, an agent step): fold undistilled
     captures into learnings.md and flip their flags to distilled=true.

The capture primitives (metadata, whisper, frame-OCR, image-OCR, reference
following, note building) are reused unchanged from ig2claude.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse ig2claude's capture layer. Add this file's dir to the path so the import
# works regardless of the caller's cwd (cron, hooks, other projects).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ig2claude as ig  # noqa: E402

DEFAULT_BASE = Path("learnings")
STATE_NAME = ".state.json"
INBOX_NAME = "inbox.txt"
LEARNINGS_NAME = "learnings.md"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
ANY_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    """Filesystem-safe id fragment."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60]
    return s or "item"


def file_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_state(topic_dir: Path) -> dict:
    p = topic_dir / STATE_NAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(topic_dir: Path, state: dict) -> None:
    (topic_dir / STATE_NAME).write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )


def read_inbox(inbox: Path) -> list[tuple[str, str]]:
    """Parse an inbox into (url, directive) pairs. Generic over any http(s) URL
    (ig2claude's own reader only matches instagram.com)."""
    if not inbox.exists():
        return []
    pairs, seen = [], set()
    for raw in inbox.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        directive = ""
        if "|" in line:
            line, directive = (s.strip() for s in line.split("|", 1))
        m = ANY_URL_RE.search(line)
        if not m:
            continue
        url = m.group(0).rstrip(".,)")
        if url not in seen:
            seen.add(url)
            pairs.append((url, directive))
    return pairs


def prune_inbox(inbox: Path, done_urls: set) -> int:
    """Drop inbox lines whose URL has been captured. Comments, blanks and links
    that failed to capture (retry next run) are kept verbatim."""
    if not inbox.exists():
        return 0
    kept, removed = [], 0
    for raw in inbox.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            m = ANY_URL_RE.search(line.split("|", 1)[0])
            if m and m.group(0).rstrip(".,)") in done_urls:
                removed += 1
                continue
        kept.append(raw)
    text = "\n".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    inbox.write_text(text, encoding="utf-8")
    return removed


# --------------------------------------------------------------------------- #
# Capture: one URL
# --------------------------------------------------------------------------- #
def capture_url(url: str, topic_dir: Path, cookies: Path | None,
                transcribe: bool, ocr: bool, follow: bool,
                directive: str) -> tuple[dict | None, ig.GapLog, str]:
    """Capture one URL into <topic>/captures/. Returns (entry_or_None, gaps, id)."""
    captures = topic_dir / "captures"
    captures.mkdir(parents=True, exist_ok=True)

    prelim = slug(url.rstrip("/").rsplit("/", 1)[-1] or url)
    gaps = ig.GapLog(prelim)
    meta = ig.fetch_metadata(url, gaps, cookies)
    if meta is None:
        return None, gaps, prelim

    code = slug(str(meta.get("id") or prelim))
    gaps.code = code  # relabel live GAP prints with the real id

    caption, transcript = ig.extract_caption_transcript(meta, gaps)
    ocr_text, onscreen, screenshots = "", "", []
    has_video = bool(meta.get("formats") or meta.get("duration"))

    if has_video:
        need_tx = transcribe and not transcript
        whisper_text, onscreen, screenshots = ig.capture_video(
            url, code, captures, gaps, cookies,
            want_transcript=need_tx, want_frames=ocr)
        if not transcript and whisper_text:
            transcript = whisper_text
        if onscreen:
            gaps.gaps = [g for g in gaps.gaps if g.kind != "empty_transcript"]
    elif ocr:
        ocr_text = ig.ocr_images(url, code, gaps, cookies)

    if has_video and not transcript and not ocr_text and not onscreen:
        gaps.add("no_transcript",
                 "No spoken-word transcript available (captions absent and audio "
                 "transcription did not yield text).")

    references = ""
    if follow:
        combined = "\n".join(t for t in (caption, transcript, ocr_text, onscreen) if t)
        references = ig.follow_references(combined, gaps)

    note = ig.build_note(url, code, meta, caption, transcript, ocr_text, gaps,
                         directive, onscreen, screenshots, references)
    (captures / f"{code}.md").write_text(note, encoding="utf-8")
    title = note.split("\n", 1)[0].lstrip("# ").strip()
    entry = {
        "source": url, "file": f"{code}.md", "title": title,
        "author": meta.get("uploader") or meta.get("channel") or "unknown",
        "gaps": [g.kind for g in gaps.gaps],
    }
    return entry, gaps, code


# --------------------------------------------------------------------------- #
# Capture: one local file
# --------------------------------------------------------------------------- #
def capture_file(path: Path, topic_dir: Path, transcribe: bool, ocr: bool,
                 follow: bool, directive: str) -> tuple[dict | None, ig.GapLog, str]:
    """Capture one local video into <topic>/captures/, then move the source into
    captures/_sources/. Returns (entry_or_None, gaps, id)."""
    captures = topic_dir / "captures"
    (captures / "_sources").mkdir(parents=True, exist_ok=True)
    code = slug(path.stem)
    gaps = ig.GapLog(code)

    transcript, onscreen, screenshots = "", "", []
    if transcribe:
        transcript = ig._whisper_from_file(path, gaps)
    if ocr:
        if shutil.which("ffmpeg"):
            with tempfile.TemporaryDirectory() as tmpname:
                onscreen, screenshots = ig._frames_ocr_and_save(
                    path, code, captures, Path(tmpname), gaps)
        else:
            gaps.add("ffmpeg_missing",
                     "ffmpeg not installed; cannot sample video frames. Install ffmpeg.")
    if onscreen:
        gaps.gaps = [g for g in gaps.gaps if g.kind != "empty_transcript"]
    if not transcript and not onscreen:
        gaps.add("no_content",
                 "No transcript or on-screen text captured from the file "
                 "(no speech and no readable frames).")

    references = ""
    if follow:
        combined = "\n".join(t for t in (transcript, onscreen) if t)
        references = ig.follow_references(combined, gaps)

    meta = {"title": path.stem, "uploader": "local", "upload_date": None}
    note = ig.build_note(f"file://{path.name}", code, meta, "", transcript, "",
                         gaps, directive, onscreen, screenshots, references)
    (captures / f"{code}.md").write_text(note, encoding="utf-8")

    dest = captures / "_sources" / path.name
    try:
        shutil.move(str(path), str(dest))
    except OSError:
        pass

    title = note.split("\n", 1)[0].lstrip("# ").strip()
    entry = {
        "source": path.name, "file": f"{code}.md", "title": title,
        "author": "local", "gaps": [g.kind for g in gaps.gaps],
    }
    return entry, gaps, code


# --------------------------------------------------------------------------- #
# Instagram user intake: pull a creator's recent posts into staging
# --------------------------------------------------------------------------- #
def _parse_date(d) -> datetime | None:
    """Best-effort parse of a gallery-dl date field. None if unknown."""
    if isinstance(d, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(d[:19], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# gallery-dl -j emits a list of [tag, ...] messages. Tags we care about:
_GDL_QUEUE = 6    # a child URL to recurse into (a user profile queues /posts/, etc.)
_GDL_ERROR = -1   # an error object: [-1, {"error", "message"}]


def _gdl_json(url: str, cookies: Path | None, cap: int, gaps: ig.GapLog) -> list | None:
    """Run gallery-dl -j on one URL. None (with a gap) on failure."""
    cmd = [*ig.GALLERY_DL, "-j", "--range", f"1-{cap}"]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        gaps.add("ig_user_timeout", "gallery-dl user listing exceeded 180s; try a smaller window.")
        return None
    if r.returncode != 0:
        tail = (r.stderr.strip().splitlines() or ["gallery-dl failed"])[-1]
        low = tail.lower()
        if any(k in low for k in ("login", "rate", "429", "auth", "challenge")):
            gaps.add("ig_user_login", "Instagram wants login / is rate-limiting. Pass --cookies.")
        else:
            gaps.add("ig_user_failed", f"gallery-dl error: {tail[:150]}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        gaps.add("ig_user_badjson", "gallery-dl returned an unparseable listing.")
        return None


def _enum_gallerydl(username: str, days: int, cookies: Path | None,
                    gaps: ig.GapLog, cap: int) -> list[str]:
    """Fallback enumerator via gallery-dl.

    gallery-dl's instagram *user* extractor yields a Queue redirect to the profile's
    /posts/ (and reels, tagged, ...) sub-extractors rather than the posts directly, so
    we follow queue entries (bounded depth) and read post metadata off the media
    entries. Dedupes by shortcode; keeps posts within the date window (posts with an
    unparseable date are kept). Instagram frequently breaks this extractor; the error
    is surfaced as a gap and this returns [].
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    seen, posts, visited = set(), [], set()
    queue = [(f"https://www.instagram.com/{username}/", 0)]

    while queue:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        data = _gdl_json(url, cookies, cap, gaps)
        if not data:
            continue
        for item in data:
            if isinstance(item, dict):          # defensive: bare metadata dict
                item = [None, None, item]
            if not isinstance(item, list) or not item:
                continue
            tag = item[0]
            if tag == _GDL_ERROR:
                msg = item[1].get("message") if len(item) > 1 and isinstance(item[1], dict) else "unknown"
                gaps.add("ig_user_error", f"gallery-dl could not list @{username}: {msg}")
                continue
            if tag == _GDL_QUEUE and depth < 2 and len(item) > 1 and isinstance(item[1], str):
                queue.append((item[1], depth + 1))
                continue
            meta = item[-1] if isinstance(item[-1], dict) else None
            if not meta:
                continue
            sc = meta.get("post_shortcode") or meta.get("shortcode") or meta.get("code")
            if not sc or sc in seen:
                continue
            dt = _parse_date(meta.get("post_date") or meta.get("date"))
            if dt is not None and dt < cutoff:
                continue  # older than the window
            seen.add(sc)
            posts.append(f"https://www.instagram.com/p/{sc}/")
    return posts


def _load_cookiejar_into(session, cookies: Path) -> None:
    """Load a Netscape cookies.txt into a requests session (for instaloader)."""
    import http.cookiejar
    cj = http.cookiejar.MozillaCookieJar(str(cookies))
    cj.load(ignore_discard=True, ignore_expires=True)
    for c in cj:
        session.cookies.set(c.name, c.value, domain=c.domain)


def _enum_instaloader(username: str, days: int, cookies: Path | None,
                      gaps: ig.GapLog, cap: int) -> list[str] | None:
    """Primary enumerator via instaloader (purpose-built for IG profiles).

    `Profile.get_posts()` yields posts newest-first with `.date_utc`, so a date window
    is a bounded scan. Returns the in-window post URLs, or None to signal "instaloader
    unavailable/failed, try the fallback" (so an empty list means a real empty window,
    not a failure). Instagram rate-limits this too; a partial result is returned rather
    than discarded.
    """
    try:
        import instaloader
    except ImportError:
        return None  # not installed -> silently fall back to gallery-dl
    loader = instaloader.Instaloader(
        quiet=True, download_pictures=False, download_videos=False,
        download_comments=False, save_metadata=False, compress_json=False)
    if cookies:
        try:
            _load_cookiejar_into(loader.context._session, cookies)
        except Exception:  # noqa: BLE001 — bad cookie file: try anonymous, may still work
            pass
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    posts: list[str] = []
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        for i, post in enumerate(profile.get_posts()):
            if i >= cap:
                break
            when = post.date_utc.replace(tzinfo=timezone.utc)
            if when < cutoff:
                continue  # pinned posts can sort out of order, so scan the cap, don't break
            posts.append(f"https://www.instagram.com/p/{post.shortcode}/")
        return posts
    except Exception as exc:  # noqa: BLE001
        gaps.add("ig_instaloader_failed", f"instaloader could not list @{username}: {str(exc)[:150]}")
        return posts if posts else None  # keep a partial result, else let gallery-dl try


def enumerate_ig_user(username: str, days: int, cookies: Path | None,
                      gaps: ig.GapLog, cap: int = 60) -> list[str]:
    """List an IG creator's post URLs from the last `days` days.

    Tries instaloader first (gallery-dl's profile extractor is routinely broken by IG);
    falls back to gallery-dl only when instaloader is unavailable or errors with nothing
    to show. Both paths are best-effort under Instagram rate limiting.
    """
    username = username.lstrip("@")
    posts = _enum_instaloader(username, days, cookies, gaps, cap)
    if posts is None:
        posts = _enum_gallerydl(username, days, cookies, gaps, cap)
    if not posts:
        gaps.add("ig_user_empty",
                 f"No posts found for @{username} in the last {days} days (or listing was blocked).")
    return posts


def process_ig_user(base: Path, username: str, days: int, cookies: Path | None,
                    transcribe: bool, ocr: bool, follow: bool) -> None:
    """Pull a creator's last `days` days of posts and capture them into staging.

    Staged captures live under <base>/staging/<username>/ (NOT a topic, so they are
    not auto-distilled). Each is flagged sorted=false; the sort-captures skill then
    files each into the topic its content fits.
    """
    ig.doctor(transcribe, ocr)
    username = username.lstrip("@")
    gaps = ig.GapLog(f"@{username}")
    urls = enumerate_ig_user(username, days, cookies, gaps)
    if not urls:
        print(f"no posts to capture for @{username} (see GAP lines above)")
        return

    print(f"found {len(urls)} post(s) for @{username} in the last {days} days")
    staging = base / "staging" / username
    state = load_state(staging)
    done = {e["source"] for e in state.values()}
    new_count = 0
    for post_url in urls:
        if post_url in done:
            print(f"skip  {username}  {post_url[:48]} (already staged)")
            continue
        entry, g, code = capture_url(post_url, staging, cookies, transcribe, ocr, follow, "")
        if entry is None:
            print(f"FAIL  {username}  {post_url[:48]} (see GAP lines above)")
            continue
        entry.update(captured=now_iso(), distilled=False, sorted=False, directive="")
        state[code] = entry
        done.add(post_url)
        new_count += 1
        print(f"ok    staging/{username}/{code}{' [partial]' if g else ''}  {entry['title'][:40]}")

    save_state(staging, state)
    unsorted = sum(1 for e in state.values() if not e.get("sorted"))
    print(f"\n{new_count} post(s) staged from @{username} -> {staging}")
    if unsorted:
        print(f"{unsorted} unsorted. Run the sort-captures skill to file them into the right "
              f"topics under {base}/topics, then distill-learnings.")


# --------------------------------------------------------------------------- #
# Topic + base orchestration
# --------------------------------------------------------------------------- #
def process_topic(topic_dir: Path, cookies: Path | None, transcribe: bool,
                  ocr: bool, follow: bool, keep_inbox: bool) -> int:
    """Capture every new URL and loose video in one topic. Returns new-count."""
    state = load_state(topic_dir)
    done_urls = {e["source"] for e in state.values()}
    known_hashes = {e["hash"] for e in state.values() if e.get("hash")}
    new_count = 0

    # 1. URLs queued in inbox.txt
    inbox = topic_dir / INBOX_NAME
    for url, directive in read_inbox(inbox):
        if url in done_urls:
            print(f"skip  {topic_dir.name}  {url[:48]} (already captured)")
            continue
        entry, gaps, code = capture_url(url, topic_dir, cookies, transcribe, ocr,
                                        follow, directive)
        if entry is None:
            print(f"FAIL  {topic_dir.name}  {url[:48]} (see GAP lines above)")
            continue
        entry.update(captured=now_iso(), distilled=False, directive=directive)
        state[code] = entry
        done_urls.add(url)
        new_count += 1
        print(f"ok    {topic_dir.name}/{code}{' [partial]' if gaps else ''}  {entry['title'][:44]}")

    if not keep_inbox:
        prune_inbox(inbox, done_urls)

    # 2. Loose local video files dropped into the topic folder
    for f in sorted(topic_dir.iterdir()):
        if not (f.is_file() and f.suffix.lower() in VIDEO_EXTS):
            continue
        h = file_hash(f)
        if h in known_hashes:
            # Same content already captured (possibly under another name): retire
            # the duplicate into _sources so it stops showing up on every scan.
            (topic_dir / "captures" / "_sources").mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(f), str(topic_dir / "captures" / "_sources" / f.name))
            except OSError:
                pass
            print(f"skip  {topic_dir.name}  {f.name} (duplicate content)")
            continue
        code = slug(f.stem)
        if code in state:
            print(f"skip  {topic_dir.name}  {f.name} (id already captured)")
            continue
        entry, gaps, code = capture_file(f, topic_dir, transcribe, ocr, follow, "")
        entry.update(captured=now_iso(), distilled=False, directive="", hash=h)
        state[code] = entry
        known_hashes.add(h)
        new_count += 1
        print(f"ok    {topic_dir.name}/{code}{' [partial]' if gaps else ''}  {entry['title'][:44]}")

    save_state(topic_dir, state)
    return new_count


def process_base(base: Path, cookies: Path | None, transcribe: bool, ocr: bool,
                 follow: bool, keep_inbox: bool) -> None:
    ig.doctor(transcribe, ocr)
    topics_dir = base / "topics"
    if not topics_dir.exists():
        print(f"No topics found. Create {topics_dir}/<topic>/ and add videos or an inbox.txt.")
        return

    total_new, pending = 0, {}
    for topic_dir in sorted(p for p in topics_dir.iterdir() if p.is_dir()):
        total_new += process_topic(topic_dir, cookies, transcribe, ocr, follow, keep_inbox)
        state = load_state(topic_dir)
        undistilled = sum(1 for e in state.values() if not e.get("distilled"))
        if undistilled:
            pending[topic_dir.name] = undistilled

    print(f"\n{total_new} new capture(s) -> {topics_dir}")
    if pending:
        report = ", ".join(f"{t} ({n})" for t, n in sorted(pending.items()))
        print(f"pending distill: {report}")
        print("Run the distill-learnings skill to fold these into each topic's learnings.md.")
    else:
        print("All captures distilled. Nothing pending.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Turn videos (files or URLs) into per-topic Claude Code learnings.")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE,
                        help=f"base directory holding topics/ (default: {DEFAULT_BASE})")
    parser.add_argument("--topic", metavar="NAME",
                        help="queue the given URLs into topics/<NAME>/inbox.txt, then process")
    parser.add_argument("--ig-user", metavar="USERNAME",
                        help="pull an Instagram creator's recent posts into staging (then sort-captures)")
    parser.add_argument("--days", type=int, default=30,
                        help="with --ig-user, how many days back to pull (default: 30)")
    parser.add_argument("--cookies", type=Path, default=None,
                        help="Netscape cookies.txt for login-required posts")
    parser.add_argument("--no-transcribe", action="store_true",
                        help="skip Whisper audio transcription (faster)")
    parser.add_argument("--no-ocr", action="store_true",
                        help="skip frame/image OCR (faster)")
    parser.add_argument("--no-follow", action="store_true",
                        help="skip following GitHub repos / links found in the video")
    parser.add_argument("--keep-inbox", action="store_true",
                        help="don't remove captured links from a topic's inbox")
    parser.add_argument("--doctor", action="store_true",
                        help="check dependencies/capabilities and exit")
    parser.add_argument("urls", nargs="*",
                        help="URLs to queue into --topic before processing")
    args = parser.parse_args()

    if args.doctor:
        ig.doctor(transcribe=not args.no_transcribe, ocr=not args.no_ocr)
        return

    if args.cookies and not args.cookies.exists():
        print(f"cookies file not found: {args.cookies}", file=sys.stderr)
        sys.exit(2)

    if args.ig_user:
        process_ig_user(args.base, args.ig_user, args.days, args.cookies,
                        transcribe=not args.no_transcribe, ocr=not args.no_ocr,
                        follow=not args.no_follow)
        return

    if args.urls:
        if not args.topic:
            print("URLs given without --topic; specify --topic NAME to file them.",
                  file=sys.stderr)
            sys.exit(2)
        topic_dir = args.base / "topics" / args.topic
        topic_dir.mkdir(parents=True, exist_ok=True)
        inbox = topic_dir / INBOX_NAME
        existing = inbox.read_text(encoding="utf-8") if inbox.exists() else ""
        with inbox.open("a", encoding="utf-8") as fp:
            if existing and not existing.endswith("\n"):
                fp.write("\n")
            for u in args.urls:
                fp.write(u + "\n")

    process_base(args.base, args.cookies, transcribe=not args.no_transcribe,
                 ocr=not args.no_ocr, follow=not args.no_follow,
                 keep_inbox=args.keep_inbox)


if __name__ == "__main__":
    main()
