#!/usr/bin/env python3
"""ig2claude — turn Instagram links into Claude Code memory, gap-aware.

Workflow:
  1. Share IG reels/posts/carousels to yourself, copy the links.
  2. Paste them into <mem-dir>/inbox.txt (default ./ig-memory/inbox.txt), one per line.
     Or skip the paste: --from-dm reads a DM thread (live, via --cookies), and
     --from-export reads an official Instagram data download.
  3. Run this script. Each new URL is fetched and turned into a structured
     markdown note (caption + transcript + carousel OCR when available),
     written into Claude Code memory and registered in an index.

Gap philosophy:
  Instagram fights scrapers and hides content, so any single post may come
  through degraded. Rather than fail silently, every shortfall is recorded as a
  GapNotice: printed live during the run (prefixed GAP), written into the note's
  "## Gaps" section, and flagged in the index. You always know exactly what the
  pipeline could not capture and why.

Only public posts are supported without cookies. Provide --cookies to reach
posts that require login. The tool never handles your password.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MEM_DIR = Path("ig-memory")  # project-relative; override with --mem-dir
INDEX_NAME = "index.md"
INBOX_NAME = "inbox.txt"
PROCESSED_NAME = ".processed.json"
DM_STATE_NAME = ".dm-state.json"

URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/\S+", re.IGNORECASE)
SHORTCODE_RE = re.compile(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")

# Invoke yt-dlp as a module through the running interpreter so it works even when
# pip's console-script bin is not on PATH (non-interactive shells, cron, etc.).
YTDLP = [sys.executable, "-m", "yt_dlp"]
# gallery-dl extracts Instagram photo/carousel images, which yt-dlp cannot.
GALLERY_DL = [sys.executable, "-m", "gallery_dl"]


# --------------------------------------------------------------------------- #
# Gap notification
# --------------------------------------------------------------------------- #
@dataclass
class GapNotice:
    """One shortfall encountered while processing a post."""
    kind: str      # short machine label, e.g. "no_transcript"
    detail: str    # human sentence explaining the gap and any remedy

    def line(self) -> str:
        return f"{self.kind}: {self.detail}"


class GapLog:
    """Collects gaps for one post and prints them live as they occur."""

    def __init__(self, code: str, stream=sys.stderr):
        self.code = code
        self.stream = stream
        self.gaps: list[GapNotice] = []

    def add(self, kind: str, detail: str) -> None:
        gap = GapNotice(kind, detail)
        self.gaps.append(gap)
        print(f"GAP   {self.code}  {gap.line()}", file=self.stream, flush=True)

    def __bool__(self) -> bool:
        return bool(self.gaps)


# --------------------------------------------------------------------------- #
# Inbox / ids / state
# --------------------------------------------------------------------------- #
def read_inbox(inbox: Path) -> list[tuple[str, str]]:
    """Parse the inbox into (url, directive) pairs.

    A line may carry a per-post directive after a '|' telling Claude how to use
    that post later, e.g.:
        https://www.instagram.com/reel/ABC/ | recipe; use when I ask what to cook
    The directive is optional; pipes never appear in Instagram URLs.
    """
    if not inbox.exists():
        return []
    pairs, seen = [], set()
    for raw in inbox.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        directive = ""
        if "|" in line:
            line, directive = line.split("|", 1)
            line, directive = line.strip(), directive.strip()
        match = URL_RE.search(line)
        if not match:
            continue
        url = match.group(0).rstrip(".,)")
        if url not in seen:
            seen.add(url)
            pairs.append((url, directive))
    return pairs


def prune_inbox(inbox: Path, done_urls: set) -> int:
    """Drop inbox lines whose Instagram URL has been processed into the library
    (saved as a note, with or without gaps). Comments, blanks, and links that
    failed to save (so they can be retried next run) are preserved verbatim.
    Returns how many lines were removed.
    """
    if not inbox.exists():
        return 0
    kept, removed = [], 0
    for raw in inbox.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            url_part = line.split("|", 1)[0].strip()
            m = URL_RE.search(url_part)
            if m and m.group(0).rstrip(".,)") in done_urls:
                removed += 1
                continue
        kept.append(raw)
    text = "\n".join(kept)
    if text and not text.endswith("\n"):
        text += "\n"
    inbox.write_text(text, encoding="utf-8")
    return removed


def shortcode(url: str) -> str:
    m = SHORTCODE_RE.search(url)
    if m:
        return m.group(1)
    return re.sub(r"[^A-Za-z0-9]+", "-", url)[-24:].strip("-")


def load_processed(mem_dir: Path) -> dict:
    path = mem_dir / PROCESSED_NAME
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_processed(mem_dir: Path, processed: dict) -> None:
    (mem_dir / PROCESSED_NAME).write_text(
        json.dumps(processed, indent=2, sort_keys=True), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_metadata(url: str, gaps: GapLog, cookies: Path | None) -> dict | None:
    """Pull post metadata via yt-dlp. Records a gap and returns None on failure."""
    cmd = [*YTDLP, "--dump-single-json", "--no-warnings", "--skip-download",
           "--ignore-no-formats-error",  # image/carousel posts have no video; still want their metadata
           "--write-auto-subs", "--sub-langs", "en.*"]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        gaps.add("fetch_timeout",
                 "yt-dlp took over 120s; Instagram may be throttling. Retry later or in smaller batches.")
        return None

    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-1] if result.stderr else "yt-dlp failed"
        low = tail.lower()
        if any(k in low for k in ("login", "rate", "429", "empty", "not available")):
            gaps.add("login_or_rate_limit",
                     "Instagram wants login or is rate-limiting. Pass --cookies cookies.txt, or run fewer links at once.")
        elif "certificate" in low or "unable to download" in low:
            gaps.add("network", "Could not reach Instagram (network/SSL, or post is private).")
        else:
            gaps.add("fetch_failed", f"yt-dlp error: {tail[:160]}")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        gaps.add("bad_metadata", "yt-dlp returned unparseable metadata.")
        return None


# --------------------------------------------------------------------------- #
# Note building (transcript/OCR added in later passes)
# --------------------------------------------------------------------------- #
def extract_caption_transcript(meta: dict, gaps: GapLog) -> tuple[str, str]:
    """Return (caption, transcript). Records a gap when caption is absent."""
    caption = (meta.get("description") or "").strip()
    transcript = ""
    subs = meta.get("automatic_captions") or meta.get("subtitles") or {}
    for lang, tracks in subs.items():
        if not lang.startswith("en"):
            continue
        for track in tracks:
            text = track.get("data")
            if text:
                cleaned = []
                for ln in text.splitlines():
                    ln = ln.strip()
                    if not ln or "-->" in ln or ln.isdigit() or ln == "WEBVTT":
                        continue
                    ln = re.sub(r"<[^>]+>", "", ln)
                    if ln and (not cleaned or cleaned[-1] != ln):
                        cleaned.append(ln)
                if cleaned:
                    transcript = " ".join(cleaned)
                    break
        if transcript:
            break
    if not caption:
        gaps.add("no_caption", "Post had no caption text.")
    return caption, transcript


_WHISPER_MODEL = None  # lazy singleton so the model loads at most once per run


def _get_whisper(gaps: GapLog):
    """Load faster-whisper once. Returns model or None (recording a gap)."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        gaps.add("whisper_missing",
                 "faster-whisper not installed; cannot transcribe audio. Run: pip3 install faster-whisper")
        return None
    try:
        _WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        return _WHISPER_MODEL
    except Exception as exc:  # noqa: BLE001 — model download needs internet on first use
        gaps.add("whisper_load_failed",
                 f"Could not load Whisper model (first use needs internet): {str(exc)[:120]}")
        return None


def _whisper_from_file(media: Path, gaps: GapLog) -> str:
    """Run Whisper on an audio/video file (faster-whisper decodes mp4 audio directly).
    '' on failure (gap logged)."""
    model = _get_whisper(gaps)
    if model is None:
        return ""
    try:
        segments, _ = model.transcribe(str(media), language="en")
        text = " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as exc:  # noqa: BLE001
        gaps.add("transcribe_failed", f"Whisper failed on the audio: {str(exc)[:120]}")
        return ""
    if not text:
        gaps.add("empty_transcript", "Whisper produced no text (likely no speech in the audio).")
    return text


def _frames_ocr_and_save(video: Path, code: str, mem_dir: Path, tmp: Path,
                         gaps: GapLog) -> tuple[str, list[str]]:
    """Sample frames, OCR the burned-in text, and SAVE the informative keyframes into
    mem_dir/assets/<code>/. Returns (on-screen text, [screenshot relpaths])."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        gaps.add("ocr_missing",
                 "pytesseract/Pillow not installed; cannot read on-screen text. Run: pip3 install pytesseract Pillow (and install tesseract).")
        return "", []
    import difflib, shutil
    frames = tmp / "frames"
    frames.mkdir(exist_ok=True)
    fcmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-vf", "fps=1/2", str(frames / "f_%04d.png")]
    try:
        subprocess.run(fcmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        gaps.add("frame_timeout", "Frame extraction exceeded 120s; skipped on-screen text OCR.")
        return "", []
    shots = sorted(frames.glob("*.png"))[:60]  # cap ~2 min of video
    if not shots:
        gaps.add("no_frames", "Could not extract frames from the video for OCR.")
        return "", []

    def clean(raw: str) -> str:
        kept = []
        for ln in re.sub(r"[ \t]+", " ", raw).splitlines():
            ln = ln.strip()
            toks = ln.split()
            if not toks:
                continue
            if sum(1 for t in toks if len(t) <= 2) * 2 > len(toks):
                continue  # majority tiny tokens = OCR gibberish
            kept.append(ln)
        return "\n".join(kept)

    blocks, frame_of = [], []
    for shot in shots:
        try:
            text = clean(pytesseract.image_to_string(Image.open(shot)))
        except Exception:  # noqa: BLE001
            continue
        if len(text) < 8:
            continue
        norm = " ".join(text.lower().split())
        dup = False
        for i, kept in enumerate(blocks):
            if difflib.SequenceMatcher(None, norm, " ".join(kept.lower().split())).ratio() > 0.8:
                if len(text) > len(kept):
                    blocks[i], frame_of[i] = text, shot
                dup = True
                break
        if not dup:
            blocks.append(text)
            frame_of.append(shot)

    screenshots = []
    if blocks:
        adir = mem_dir / "assets" / code
        adir.mkdir(parents=True, exist_ok=True)
        for n, shot in enumerate(frame_of[:8], 1):  # keep up to 8 informative keyframes
            dest = adir / f"frame-{n:02d}.png"
            try:
                shutil.copyfile(shot, dest)
                screenshots.append(f"assets/{code}/frame-{n:02d}.png")
            except OSError:
                pass
    onscreen = "\n\n".join(blocks)
    if not onscreen:
        gaps.add("no_onscreen_text",
                 "Video frames had no readable on-screen text (purely visual or animated text).")
    return onscreen, screenshots


def capture_video(url: str, code: str, mem_dir: Path, gaps: GapLog, cookies: Path | None,
                  want_transcript: bool, want_frames: bool) -> tuple[str, str, list[str]]:
    """Download the clip ONCE, then optionally Whisper-transcribe it and sample
    informative keyframes (OCR + saved screenshots). One download serves both.
    Returns (transcript, on-screen text, [screenshot relpaths])."""
    import tempfile, shutil
    transcript, onscreen, screenshots = "", "", []
    have_ff = shutil.which("ffmpeg") is not None
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        out = tmp / f"{code}.mp4"
        cmd = [*YTDLP, "--no-warnings", "-f", "mp4/best", "-o", str(out)]
        if cookies:
            cmd += ["--cookies", str(cookies)]
        cmd.append(url)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            gaps.add("video_timeout", "Video download exceeded 180s; skipped video capture.")
            return transcript, onscreen, screenshots
        produced = list(tmp.glob(f"{code}.*"))
        if r.returncode != 0 or not produced:
            gaps.add("video_download_failed",
                     "Could not download the video (post may be login-gated or image-only).")
            return transcript, onscreen, screenshots
        video = produced[0]

        if want_transcript:
            transcript = _whisper_from_file(video, gaps)
        if want_frames:
            if have_ff:
                onscreen, screenshots = _frames_ocr_and_save(video, code, mem_dir, tmp, gaps)
            else:
                gaps.add("ffmpeg_missing",
                         "ffmpeg not installed; cannot sample video frames. Install ffmpeg.")
    return transcript, onscreen, screenshots


def ocr_images(url: str, code: str, gaps: GapLog, cookies: Path | None) -> str:
    """Download post images and OCR text baked into them. '' on failure (gap logged)."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        gaps.add("ocr_missing",
                 "pytesseract/Pillow not installed; cannot read text in images. Run: pip3 install pytesseract Pillow (and install tesseract).")
        return ""

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def found_images():
            return [p for p in Path(tmp).rglob("*")
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]

        # First try yt-dlp's thumbnail: cheap, and it grabs a video reel's poster
        # frame without downloading the whole clip.
        cmd = [*YTDLP, "--no-warnings", "--write-thumbnail", "--skip-download",
               "-o", str(Path(tmp) / f"{code}.%(ext)s")]
        if cookies:
            cmd += ["--cookies", str(cookies)]
        cmd.append(url)
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            gaps.add("image_timeout", "Image download exceeded 120s; skipped OCR.")
            return ""

        # Image/carousel posts expose no thumbnail to yt-dlp; gallery-dl pulls their
        # actual images (same cookies). A missing gallery-dl just returns nonzero here.
        if not found_images():
            gcmd = [*GALLERY_DL, "--quiet", "-D", str(tmp)]
            if cookies:
                gcmd += ["--cookies", str(cookies)]
            gcmd.append(url)
            try:
                subprocess.run(gcmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                gaps.add("image_timeout", "gallery-dl image download exceeded 120s; skipped OCR.")
                return ""

        images = found_images()
        if not images:
            gaps.add("no_images", "No images retrieved to OCR (post may be video-only or login-gated).")
            return ""

        blocks = []
        for i, img in enumerate(sorted(images), 1):
            try:
                text = pytesseract.image_to_string(Image.open(img)).strip()
            except Exception as exc:  # noqa: BLE001
                gaps.add("ocr_failed", f"tesseract failed on an image: {str(exc)[:100]}")
                continue
            # collapse whitespace, drop near-empty noise
            text = re.sub(r"[ \t]+", " ", text)
            text = "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())
            if len(text) >= 8:  # ignore stray single chars / logos
                label = f"**Slide {i}:**" if len(images) > 1 else ""
                blocks.append(f"{label}\n{text}".strip())

        if not blocks:
            gaps.add("no_image_text", "Images had no readable text (purely visual, or OCR found nothing).")
            return ""
        return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Follow the references the post gives us (GitHub repos, links) to pull real info
# --------------------------------------------------------------------------- #
_GH_URL_RE = re.compile(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", re.IGNORECASE)
_BARE_REPO_RE = re.compile(r"\b([A-Za-z0-9][\w.\-]*/[A-Za-z0-9][\w.\-]+)\b")
_REPO_CUE_RE = re.compile(r"github|repo|marketplace|plugin|open[- ]?source", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")


def _http_get(url: str, headers: dict, timeout: int = 15, limit: int = 60000) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit).decode("utf-8", "replace")


def _clean_repo(s: str) -> str:
    s = s.rstrip(").,;:/")
    return s[:-4] if s.endswith(".git") else s


def _gh_repo_info(repo: str) -> dict | None:
    try:
        body = _http_get(f"https://api.github.com/repos/{repo}",
                         {"User-Agent": "ig2claude", "Accept": "application/vnd.github+json"})
        d = json.loads(body)
    except Exception:  # noqa: BLE001 — network/parse/404 all mean "skip this candidate"
        return None
    if not d.get("full_name"):
        return None
    return {
        "full_name": d["full_name"],
        "html_url": d.get("html_url", f"https://github.com/{repo}"),
        "description": (d.get("description") or "").strip(),
        "stars": d.get("stargazers_count", 0),
        "language": d.get("language") or "",
        "homepage": (d.get("homepage") or "").strip(),
    }


def _url_meta(url: str) -> dict | None:
    try:
        html = _http_get(url, {"User-Agent": "Mozilla/5.0 (compatible; ig2claude/1.0)"})
    except Exception:  # noqa: BLE001
        return None
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", t.group(1)).strip()[:120] if t else ""
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
                  html, re.IGNORECASE | re.DOTALL)
    desc = re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""
    if not title and not desc:
        return None
    return {"url": url, "title": title or url, "desc": desc}


def follow_references(text: str, gaps: GapLog) -> str:
    """Extract GitHub repos and links the post references, fetch what they are, and
    return a markdown '## References (followed)' block. '' when nothing resolves."""
    if not text.strip():
        return ""
    repos = {_clean_repo(m) for m in _GH_URL_RE.findall(text)}
    for line in text.splitlines():
        if not _REPO_CUE_RE.search(line):
            continue
        for cand in _BARE_REPO_RE.findall(line):
            cand = _clean_repo(cand)
            if "github.com" in cand.lower():
                continue
            owner, _, rp = cand.partition("/")
            if (len(owner) >= 2 and len(rp) >= 2
                    and re.search(r"[A-Za-z]", owner) and re.search(r"[A-Za-z]", rp)):
                repos.add(cand)
    gh = [g for g in (_gh_repo_info(r) for r in list(repos)[:8]) if g]

    urls, seen = [], set()
    for u in _URL_RE.findall(text):
        u = u.rstrip(").,;:'\"")
        low = u.lower()
        if "instagram.com" in low or "github.com" in low or u in seen:
            continue
        seen.add(u)
        urls.append(u)
    links = [l for l in (_url_meta(u) for u in urls[:5]) if l]

    if not gh and not links:
        return ""
    out = ["## References (followed)", ""]
    if gh:
        out.append("**GitHub repos:**")
        for g in gh:
            meta_bits = ", ".join(x for x in [
                f"{g['stars']:,} stars" if g["stars"] else "",
                g["language"],
            ] if x)
            head = f"- [{g['full_name']}]({g['html_url']})"
            if meta_bits:
                head += f": {meta_bits}"
            if g["description"]:
                head += f". {g['description']}"
            if g["homepage"]:
                head += f" (site: {g['homepage']})"
            out.append(head)
        out.append("")
    if links:
        out.append("**Links:**")
        for l in links:
            line = f"- [{l['title']}]({l['url']})"
            if l["desc"]:
                line += f": {l['desc']}"
            out.append(line)
        out.append("")
    return "\n".join(out).strip()


def build_note(url: str, code: str, meta: dict, caption: str,
               transcript: str, ocr_text: str, gaps: GapLog,
               directive: str = "", onscreen: str = "",
               screenshots: list[str] | None = None, references: str = "") -> str:
    title = meta.get("title") or caption.split("\n")[0][:80] or code
    uploader = meta.get("uploader") or meta.get("channel") or "unknown"
    upload_date = meta.get("upload_date")
    if upload_date and len(upload_date) == 8:
        posted = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        posted = "unknown"
    saved = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"# {title.strip()}",
        "",
        f"- **Source:** [{url}]({url})",
        f"- **Author:** @{uploader}",
        f"- **Posted:** {posted}",
        f"- **Saved:** {saved}",
        "",
    ]
    if directive:
        lines += ["## How to use", "",
                  "_Instruction from the user for how to apply this post:_", "",
                  directive, ""]
    lines += [
        "## Caption",
        "",
        caption if caption else "_(none)_",
        "",
    ]
    if transcript:
        lines += ["## Transcript", "", transcript, ""]
    if onscreen:
        lines += ["## On-screen text (video)", "", onscreen, ""]
    if screenshots:
        lines += ["## Screenshots", "",
                  "_Informative keyframes captured from the video:_", ""]
        lines += [f"![frame {i}]({p})" for i, p in enumerate(screenshots, 1)]
        lines += [""]
    if ocr_text:
        lines += ["## Text in images (OCR)", "", ocr_text, ""]
    if references:
        lines += [references, ""]
    if gaps:
        lines += ["## Gaps", "",
                  "_The pipeline could not fully capture this post:_", ""]
        lines += [f"- **{g.kind}** — {g.detail}" for g in gaps.gaps]
        lines += [""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def update_index(mem_dir: Path, processed: dict) -> None:
    entries = sorted(processed.values(), key=lambda e: e.get("saved", ""), reverse=True)
    lines = [
        "# Instagram memory index",
        "",
        "Informative Instagram posts saved for Claude Code to reference.",
        "A warning marker means the pipeline captured that post only partially (see its Gaps section).",
        "",
    ]
    for e in entries:
        warn = " [partial]" if e.get("gaps") else ""
        use = f" (use: {e['directive']})" if e.get("directive") else ""
        lines.append(f"- [{e['title']}](./{e['file']}){warn} — @{e['author']} ({e['saved']}){use}")
    lines.append("")
    (mem_dir / INDEX_NAME).write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# DM thread -> post / reel / carousel URLs
# --------------------------------------------------------------------------- #
# Live mode pages Instagram's web DM endpoints with the same Netscape cookies
# file --cookies already uses. Export mode reads an official "Download your
# information" JSON dump instead, which is the safer way to do the first big
# backfill. Neither mode stores a password.

DM_INBOX_URL = "https://www.instagram.com/api/v1/direct_v2/inbox/"
DM_THREAD_URL = "https://www.instagram.com/api/v1/direct_v2/threads/{thread_id}/"
DM_INBOX_REFERER = "https://www.instagram.com/direct/inbox/"
IG_WEB_APP_ID = "936619743392459"
IG_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Small pages and a multi-second jittered gap, matching the README guidance to
# go slowly and back off when Instagram throttles. Backoff waits are longer.
DM_PAGE_SIZE = 20
DM_PAGE_DELAY_MIN = 4.0
DM_PAGE_DELAY_MAX = 8.0
DM_BACKOFF_START = 45.0
DM_BACKOFF_MAX = 300.0
DM_BACKOFF_ATTEMPTS = 4

# reel_share / story_share are story replies, not saved posts. Their media
# shortcodes are not /p/ or /reel/ permalinks, so they are not turned into URLs.
# A post link typed in the message text is still collected.
_STORY_ITEM_TYPES = {"reel_share", "story_share", "xma_story_share"}
_THREAD_ID_RE = re.compile(r"^\d{8,}$")
_THREAD_URL_RE = re.compile(r"instagram\.com/direct/t/(\d+)", re.IGNORECASE)
_MESSAGE_FILE_RE = re.compile(r"message_(\d+)\.json$", re.IGNORECASE)


class DmError(Exception):
    """DM collection failed in a way a re-run might not fix by itself."""


class DmRateLimit(DmError):
    """Instagram rate-limited the session. Earlier pages are saved."""


class DmLoginRequired(DmError):
    """Cookies are missing, expired, or Instagram asked for a checkpoint."""


class DmNotFound(DmError):
    """The requested thread was not in the inbox or the export."""


def dm_log(message: str) -> None:
    """Progress for a DM pull. Stdout stays a clean URL list in --dry-run."""
    print(f"dm    {message}", file=sys.stderr, flush=True)


def post_code(url: str) -> str | None:
    """Shortcode for a post/reel/tv URL, or None if it is not one."""
    match = SHORTCODE_RE.search(url or "")
    return match.group(1) if match else None


def canonical_post_url(url: str) -> str | None:
    """Normalize a post, reel, or IGTV permalink. None for stories and other links.

    Query strings, trailing punctuation, and /reels/ (plural) are folded away so
    the same media shared twice becomes one URL. Opaque /share/ links that hide
    the shortcode are dropped; resolving them would mean extra requests.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    cleaned = url.strip().rstrip(".,);]>\"'")
    match = re.search(
        r"instagram\.com/(reel|reels|p|tv)/([A-Za-z0-9_-]+)",
        cleaned,
        re.IGNORECASE,
    )
    if not match:
        return None
    kind = match.group(1).lower()
    if kind == "reels":
        kind = "reel"
    return f"https://www.instagram.com/{kind}/{match.group(2)}/"


def urls_in_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    found = []
    for match in URL_RE.finditer(text):
        url = canonical_post_url(match.group(0))
        if url:
            found.append(url)
    return found


def _unique_urls(urls: list[str], skip: set[str] | None = None) -> list[str]:
    """Dedupe by shortcode, preserving first-seen (newest-first) order."""
    seen = set(skip or ())
    out = []
    for url in urls:
        code = post_code(url)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(url)
    return out


def _kind_for_media(media: dict) -> str:
    product = str(media.get("product_type") or "").lower()
    if product == "clips":
        return "reel"
    if product in ("igtv", "tv"):
        return "tv"
    return "p"


def url_from_media(media, depth: int = 0) -> str | None:
    """Build a permalink from a DM media object (code + product_type, or a URL)."""
    if not isinstance(media, dict) or depth > 4:
        return None
    for key in ("share_url", "target_url", "url"):
        if isinstance(media.get(key), str):
            got = canonical_post_url(media[key])
            if got:
                return got
    code = media.get("code") or media.get("shortcode")
    if isinstance(code, str) and code:
        return f"https://www.instagram.com/{_kind_for_media(media)}/{code}/"
    for key in ("clip", "video", "media"):
        nested = media.get(key)
        if isinstance(nested, dict):
            got = url_from_media(nested, depth + 1)
            if got:
                return got
    return None


def _urls_from_xma(blob) -> list[str]:
    if isinstance(blob, dict):
        entries = [blob]
    elif isinstance(blob, list):
        entries = [entry for entry in blob if isinstance(entry, dict)]
    else:
        return []
    found = []
    for entry in entries:
        for key in ("target_url", "url", "link"):
            if isinstance(entry.get(key), str):
                got = canonical_post_url(entry[key])
                if got:
                    found.append(got)
        got = url_from_media(entry)
        if got:
            found.append(got)
    return found


def extract_urls_from_dm_item(item: dict) -> list[str]:
    """Post/reel/carousel URLs from one web DM item (shared media or text)."""
    if not isinstance(item, dict):
        return []
    found: list[str] = []
    if isinstance(item.get("text"), str):
        found.extend(urls_in_text(item["text"]))

    link = item.get("link")
    if isinstance(link, dict):
        if isinstance(link.get("text"), str):
            found.extend(urls_in_text(link["text"]))
        context = link.get("link_context") if isinstance(link.get("link_context"), dict) else {}
        for candidate in (link.get("url"), context.get("link_url")):
            if isinstance(candidate, str):
                got = canonical_post_url(candidate)
                if got:
                    found.append(got)

    item_type = str(item.get("item_type") or "")
    if item_type not in _STORY_ITEM_TYPES:
        for key in ("media_share", "clip", "felix_share", "igtv_share"):
            if isinstance(item.get(key), dict):
                got = url_from_media(item[key])
                if got:
                    found.append(got)
        for key in ("xma_media_share", "generic_xma", "xma_share", "xma_link"):
            found.extend(_urls_from_xma(item.get(key)))
    return _unique_urls(found)


def extract_urls_from_dm_payload(payload: dict) -> list[str]:
    """URLs from a direct_v2 thread response, newest item first."""
    page = parse_thread_page(payload)
    found: list[str] = []
    for item in page["items"]:
        found.extend(extract_urls_from_dm_item(item))
    return _unique_urls(found)


def parse_thread_page(payload: dict) -> dict:
    """Normalize a thread response to items, oldest_cursor, has_older, newest_ts."""
    if not isinstance(payload, dict):
        raise DmError("Unexpected DM response (not a JSON object).")
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else payload
    if not isinstance(thread, dict):
        raise DmError("DM response had no thread.")
    raw_items = thread.get("items") or []
    items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    cursor = thread.get("oldest_cursor") or thread.get("next_cursor") or None
    if cursor is not None:
        cursor = str(cursor)
    has_older = bool(thread.get("has_older")) and bool(cursor)
    timestamps = [
        int(item["timestamp"])
        for item in items
        if isinstance(item.get("timestamp"), (int, float))
    ]
    return {
        "items": items,
        "oldest_cursor": cursor,
        "has_older": has_older,
        "newest_ts": max(timestamps) if timestamps else None,
    }


def parse_inbox_page(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("inbox"), dict):
        raise DmError("DM inbox response had no inbox object.")
    inbox = payload["inbox"]
    raw = inbox.get("threads") or []
    threads = [thread for thread in raw if isinstance(thread, dict)] if isinstance(raw, list) else []
    cursor = inbox.get("oldest_cursor") or inbox.get("next_cursor")
    if cursor is not None:
        cursor = str(cursor)
    return {
        "threads": threads,
        "oldest_cursor": cursor,
        "has_older": bool(inbox.get("has_older")) and bool(cursor),
    }


def fix_export_text(value: str) -> str:
    """Undo the Download-your-information latin-1 mojibake on a string.

    Instagram writes UTF-8 bytes, then those bytes are decoded as latin-1 and
    stored in the JSON. ASCII (including URLs) is unchanged. Text that is
    already proper Unicode is left alone when the round-trip is not valid UTF-8.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def _fix_export_obj(obj):
    if isinstance(obj, str):
        return fix_export_text(obj)
    if isinstance(obj, list):
        return [_fix_export_obj(item) for item in obj]
    if isinstance(obj, dict):
        fixed = {}
        for key, val in obj.items():
            fixed_key = fix_export_text(key) if isinstance(key, str) else key
            fixed[fixed_key] = _fix_export_obj(val)
        return fixed
    return obj


def decode_export_json(raw: bytes):
    """Parse one export JSON file and fix mojibake in every string."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return _fix_export_obj(json.loads(text))


def extract_urls_from_export_message(msg: dict) -> list[str]:
    """URLs from one export message: share.link and any permalinks in the text."""
    if not isinstance(msg, dict):
        return []
    found: list[str] = []
    if isinstance(msg.get("content"), str):
        found.extend(urls_in_text(fix_export_text(msg["content"])))
    share = msg.get("share")
    if isinstance(share, dict):
        link = share.get("link")
        if isinstance(link, str):
            got = canonical_post_url(fix_export_text(link))
            if got:
                found.append(got)
        if isinstance(share.get("share_text"), str):
            found.extend(urls_in_text(fix_export_text(share["share_text"])))
    return _unique_urls(found)


def captured_shortcodes(mem_dir: Path) -> set[str]:
    """Shortcodes already written into the memory folder (index or note file)."""
    codes: set[str] = set()
    if not mem_dir.exists():
        return codes
    codes.update(load_processed(mem_dir).keys())
    for path in mem_dir.glob("*.md"):
        if path.name != INDEX_NAME:
            codes.add(path.stem)
    return codes


def inbox_shortcodes(inbox: Path) -> set[str]:
    codes: set[str] = set()
    for url, _directive in read_inbox(inbox):
        canonical = canonical_post_url(url)
        code = post_code(canonical or url)
        if code:
            codes.add(code)
    return codes


def dedupe_post_urls(urls: list[str], mem_dir: Path, extra_codes: set[str] | None = None) -> list[str]:
    """Drop URLs whose shortcode is already captured, or repeated in `urls`."""
    skip = captured_shortcodes(mem_dir)
    if extra_codes:
        skip |= set(extra_codes)
    return _unique_urls(urls, skip)


def enqueue_urls(inbox: Path, urls: list[str]) -> int:
    """Append new post URLs to the inbox. Existing lines (comments included) stay."""
    if not urls:
        return 0
    existing = inbox.read_text(encoding="utf-8").splitlines() if inbox.exists() else []
    have = inbox_shortcodes(inbox) if inbox.exists() else set()
    fresh = []
    for url in urls:
        code = post_code(url)
        if not code or code in have:
            continue
        have.add(code)
        fresh.append(url)
    if not fresh:
        return 0
    text = "\n".join([*existing, *fresh])
    if text:
        text += "\n"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(text, encoding="utf-8")
    return len(fresh)


def dm_state_path(mem_dir: Path) -> Path:
    return mem_dir / DM_STATE_NAME


def load_dm_state(mem_dir: Path) -> dict | None:
    path = dm_state_path(mem_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def save_dm_state(mem_dir: Path, state: dict) -> None:
    mem_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dm_state_path(mem_dir).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _blank_dm_state(thread_key: str, thread_id: str) -> dict:
    return {
        "thread_key": thread_key,
        "thread_id": thread_id,
        "cursor": None,
        "backfill_done": False,
        "newest_ts": None,
    }


def _urls_from_items(items: list[dict], skip: set[str]) -> list[str]:
    found: list[str] = []
    for item in items:
        found.extend(extract_urls_from_dm_item(item))
    return _unique_urls(found, skip)


def collect_thread_urls(
    thread_key: str,
    mem_dir: Path,
    fetch_page,
    *,
    resolve_thread_id=None,
    limit: int | None = None,
    persist: bool = True,
    sleep=None,
    rng: random.Random | None = None,
    extra_skip: set[str] | None = None,
    on_commit=None,
) -> list[str]:
    """Page a thread newest-first and return post URLs not already captured.

    `fetch_page(thread_id, cursor)` returns one direct_v2 thread JSON object.
    `cursor` is None for the newest page. When `persist` is set, a cursor is
    written to `<mem-dir>/.dm-state.json` after each fully consumed page so a
    long backfill can stop and continue. A page that is only partly taken
    because of `limit` does not advance the cursor.

    After the backfill reaches the oldest message, later runs only walk new
    messages at the head (newer than the saved timestamp) until they overlap
    what was already synced.

    Dry-runs pass persist=False: the saved cursor is still honored, but not moved.
    `on_commit` receives each committed batch (used to append the inbox before
    the cursor moves, so an interrupt cannot skip links).
    """
    if sleep is None:
        sleep = time.sleep
    if rng is None:
        rng = random.Random()

    skip = captured_shortcodes(mem_dir)
    if extra_skip:
        skip |= set(extra_skip)

    state = load_dm_state(mem_dir) or {}
    if state.get("thread_key") != thread_key or not state.get("thread_id"):
        if resolve_thread_id is None:
            raise DmError("No thread id for this DM pull.")
        state = _blank_dm_state(thread_key, str(resolve_thread_id()))
        if persist:
            save_dm_state(mem_dir, state)
    thread_id = str(state["thread_id"])
    collected: list[str] = []

    def commit(urls: list[str]) -> None:
        if not urls:
            return
        collected.extend(urls)
        for url in urls:
            code = post_code(url)
            if code:
                skip.add(code)
        if persist and on_commit:
            on_commit(list(urls))

    def take(urls: list[str]) -> tuple[list[str], bool]:
        """(urls that fit in the limit, whether every url on the page was taken)."""
        if limit is None:
            return urls, True
        room = limit - len(collected)
        if len(urls) <= room:
            return urls, True
        return urls[: max(room, 0)], False

    def pause() -> None:
        sleep(rng.uniform(DM_PAGE_DELAY_MIN, DM_PAGE_DELAY_MAX))

    if state.get("backfill_done"):
        _collect_new_since(
            state, thread_id, fetch_page, commit, take, pause, collected, skip,
            mem_dir, persist, limit, thread_key,
        )
        return collected

    cursor = state.get("cursor") or None
    first = True
    page_no = 0
    while True:
        if limit is not None and len(collected) >= limit:
            return collected
        if not first:
            pause()
        first = False
        page_no += 1
        page = parse_thread_page(fetch_page(thread_id, cursor))
        if cursor is None and state.get("newest_ts") is None and page["newest_ts"] is not None:
            state["newest_ts"] = page["newest_ts"]

        chosen, consumed_all = take(_urls_from_items(page["items"], skip))
        commit(chosen)
        dm_log(f"{thread_key}  page {page_no}  +{len(chosen)} new ({len(collected)} this run)")
        if not consumed_all:
            if persist:
                save_dm_state(mem_dir, state)
            dm_log(f"{thread_key}  limit {limit} reached; resume cursor kept")
            return collected

        if page["has_older"]:
            next_cursor = page["oldest_cursor"]
            if not next_cursor or next_cursor == cursor:
                raise DmError(
                    "Instagram did not advance the thread cursor, so paging stopped. "
                    "Re-run later. Links from finished pages stay in the inbox."
                )
            state["cursor"] = next_cursor
            cursor = next_cursor
            if persist:
                save_dm_state(mem_dir, state)
            if limit is not None and len(collected) >= limit:
                return collected
            continue

        state["cursor"] = None
        state["backfill_done"] = True
        if persist:
            save_dm_state(mem_dir, state)
        dm_log(f"{thread_key}  reached the oldest message")
        return collected


def _collect_new_since(
    state, thread_id, fetch_page, commit, take, pause, collected, skip,
    mem_dir, persist, limit, thread_key,
) -> None:
    """After a finished backfill, collect only messages newer than newest_ts."""
    cursor = None
    head_ts = None
    first = True
    page_no = 0
    known_ts = state.get("newest_ts")
    while True:
        if limit is not None and len(collected) >= limit:
            return
        if not first:
            pause()
        first = False
        page_no += 1
        page = parse_thread_page(fetch_page(thread_id, cursor))
        if cursor is None and page["newest_ts"] is not None:
            head_ts = page["newest_ts"]

        fresh = []
        overlapped = False
        for item in page["items"]:
            ts = item.get("timestamp")
            if known_ts is not None and isinstance(ts, (int, float)) and int(ts) <= int(known_ts):
                overlapped = True
                break
            fresh.append(item)

        chosen, consumed_all = take(_urls_from_items(fresh, skip))
        commit(chosen)
        dm_log(f"{thread_key}  new page {page_no}  +{len(chosen)} ({len(collected)} this run)")
        if not consumed_all:
            dm_log(f"{thread_key}  limit {limit} reached; head cursor not moved")
            return

        if overlapped or not page["has_older"]:
            if persist and head_ts is not None:
                previous = state.get("newest_ts")
                state["newest_ts"] = int(head_ts) if previous is None else max(int(previous), int(head_ts))
                state["cursor"] = None
                state["backfill_done"] = True
                save_dm_state(mem_dir, state)
            return

        next_cursor = page["oldest_cursor"]
        if not next_cursor or next_cursor == cursor:
            raise DmError(
                "Instagram did not advance the thread cursor while checking new messages."
            )
        cursor = next_cursor


def normalize_thread_id(value: str) -> str | None:
    """Return a numeric thread id, or None when `value` looks like a username."""
    text = (value or "").strip()
    match = _THREAD_URL_RE.search(text)
    if match:
        return match.group(1)
    if _THREAD_ID_RE.match(text):
        return text
    return None


def thread_matches_user(thread: dict, username: str) -> bool:
    want = username.lstrip("@").strip().casefold()
    if not want or not isinstance(thread, dict):
        return False
    for user in thread.get("users") or []:
        if isinstance(user, dict) and str(user.get("username") or "").lstrip("@").casefold() == want:
            return True
    title = str(thread.get("thread_title") or "").lstrip("@").strip().casefold()
    return title == want


def classify_dm_response(status: int, payload) -> str:
    """Return 'ok', 'rate_limit', 'login', or 'error'."""
    if status == 429 or status in (500, 502, 503, 504):
        return "rate_limit"
    if status in (401, 403):
        return "login"
    if not isinstance(payload, dict):
        return "error"
    if payload.get("status") == "ok" or (
        payload.get("status") is None and ("thread" in payload or "inbox" in payload)
    ):
        return "ok"
    blob = " ".join(
        str(payload.get(key) or "") for key in ("message", "error_type", "feedback_title")
    ).lower()
    if "login_required" in blob or "checkpoint" in blob or "consent_required" in blob:
        return "login"
    if payload.get("spam") is True or any(
        token in blob for token in ("please wait", "rate limit", "rate_limit", "feedback_required", "try again")
    ):
        return "rate_limit"
    return "error"


def _response_detail(payload, status: int) -> str:
    """Short reason safe to print. Never includes cookies or headers."""
    if isinstance(payload, dict):
        msg = payload.get("message") or payload.get("error_type") or ""
        msg = re.sub(r"\s+", " ", str(msg)).strip()
        if msg:
            return msg[:180]
    return f"HTTP {status}"


def build_dm_headers(cookies: dict, referer: str) -> dict:
    claim = cookies.get("ig_www_claim") or cookies.get("www_claim") or "0"
    headers = {
        "User-Agent": IG_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "X-IG-App-ID": IG_WEB_APP_ID,
        "X-Requested-With": "XMLHttpRequest",
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": claim,
    }
    token = cookies.get("csrftoken")
    if token:
        headers["X-CSRFToken"] = token
    headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)
    return headers


def http_get_json(url: str, headers: dict, timeout: int = 30):
    """GET a URL. Returns (status, payload). Payload is a dict when the body is JSON."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise DmError(f"Could not reach Instagram ({exc.reason}).") from exc
    text = raw.decode("utf-8", "replace") if raw else ""
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {"message": text[:200]}
    return status, payload


def get_json(url, *, cookies, referer, http_get, sleep, rng, max_attempts=DM_BACKOFF_ATTEMPTS):
    """GET JSON, backing off on rate limits. Raises DmLoginRequired or DmRateLimit."""
    headers = build_dm_headers(cookies, referer)
    delay = DM_BACKOFF_START
    detail = "HTTP error"
    for attempt in range(1, max_attempts + 1):
        status, payload = http_get(url, headers)
        kind = classify_dm_response(status, payload)
        if kind == "ok":
            return payload
        detail = _response_detail(payload, status)
        if kind == "login":
            raise DmLoginRequired(
                "Instagram rejected the session (login or checkpoint). "
                "Re-export cookies.txt from the browser while logged in. "
                f"({detail})"
            )
        if kind == "rate_limit":
            if attempt == max_attempts:
                break
            wait = min(delay * rng.uniform(0.8, 1.2), DM_BACKOFF_MAX)
            dm_log(f"rate limit ({detail}); waiting {wait:.0f}s (attempt {attempt}/{max_attempts})")
            sleep(wait)
            delay = min(delay * 2, DM_BACKOFF_MAX)
            continue
        raise DmError(f"Instagram DM request failed ({status}): {detail}")
    raise DmRateLimit(
        f"Instagram rate limit persisted after {max_attempts} attempts ({detail}). "
        "Progress up to the previous page is saved. Re-run the same command later to continue."
    )


def load_netscape_cookies(path: Path) -> dict[str, str]:
    """Parse a Netscape cookies.txt. Only instagram.com cookies are kept.

    Values are returned as stored. This never reads a password; a cookies file
    has no password field.
    """
    cookies: dict[str, str] = {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain = parts[0].lstrip(".").lower()
        if domain != "instagram.com" and not domain.endswith(".instagram.com"):
            continue
        name = parts[5]
        value = "\t".join(parts[6:])
        if name:
            cookies[name] = value
    return cookies


def require_ig_session(cookies: dict) -> None:
    if not cookies.get("sessionid"):
        raise DmLoginRequired(
            "cookies file has no Instagram sessionid. "
            "Export Netscape cookies while logged in to instagram.com."
        )
    if not cookies.get("csrftoken"):
        dm_log("cookies file has no csrftoken; Instagram may reject the request")


def build_inbox_url(cursor: str | None) -> str:
    params = {
        "persistentBadging": "true",
        "folder": "",
        "limit": str(DM_PAGE_SIZE),
        "thread_message_limit": "1",
    }
    if cursor:
        params["cursor"] = cursor
    return DM_INBOX_URL + "?" + urllib.parse.urlencode(params)


def build_thread_url(thread_id: str, cursor: str | None) -> str:
    params = {
        "visual_message_return_type": "unseen",
        "direction": "older",
        "limit": str(DM_PAGE_SIZE),
    }
    if cursor:
        params["cursor"] = cursor
    quoted = urllib.parse.quote(str(thread_id), safe="")
    return DM_THREAD_URL.format(thread_id=quoted) + "?" + urllib.parse.urlencode(params)


class InstagramWeb:
    """Minimal client for the web inbox and thread endpoints."""

    def __init__(self, cookies: dict, *, sleep=None, rng=None, http_get=None):
        self.cookies = cookies
        self.sleep = sleep or time.sleep
        self.rng = rng or random.Random()
        self.http_get = http_get or http_get_json

    def get_json(self, url: str, referer: str) -> dict:
        return get_json(
            url,
            cookies=self.cookies,
            referer=referer,
            http_get=self.http_get,
            sleep=self.sleep,
            rng=self.rng,
        )

    def resolve_thread_id(self, thread_key: str) -> str:
        direct = normalize_thread_id(thread_key)
        if direct:
            return direct
        cursor = None
        seen: set[str] = set()
        page_no = 0
        while True:
            if cursor:
                self.sleep(self.rng.uniform(DM_PAGE_DELAY_MIN, DM_PAGE_DELAY_MAX))
            payload = self.get_json(build_inbox_url(cursor), referer=DM_INBOX_REFERER)
            page = parse_inbox_page(payload)
            page_no += 1
            for thread in page["threads"]:
                if thread_matches_user(thread, thread_key):
                    thread_id = str(thread.get("thread_id") or thread.get("thread_v2_id") or "")
                    if thread_id:
                        dm_log(f"thread {thread_key} is {thread_id}")
                        return thread_id
            next_cursor = page["oldest_cursor"]
            if not page["has_older"] or not next_cursor or next_cursor in seen:
                break
            seen.add(next_cursor)
            cursor = next_cursor
            dm_log(f"inbox page {page_no}, still looking for {thread_key}")
        raise DmNotFound(
            f"No DM thread found for {thread_key}. "
            "Pass that account's username, or the numeric id from instagram.com/direct/t/<id>/."
        )

    def fetch_thread_page(self, thread_id: str, cursor: str | None) -> dict:
        referer = f"https://www.instagram.com/direct/t/{thread_id}/"
        return self.get_json(build_thread_url(thread_id, cursor), referer=referer)


def gather_live_urls(thread_key, cookies_path, mem_dir, inbox, limit, persist, on_commit=None) -> list[str]:
    """Page the live DM thread. When persist is set, append URLs to the inbox as we go.

    `on_commit` runs after each batch is appended, including when a later page
    raises, so the caller can still capture links queued before the failure.
    """
    cookies = load_netscape_cookies(cookies_path)
    require_ig_session(cookies)
    client = InstagramWeb(cookies)
    extra = inbox_shortcodes(inbox)

    def commit(urls: list[str]) -> None:
        enqueue_urls(inbox, urls)
        if on_commit:
            on_commit(urls)

    return collect_thread_urls(
        thread_key,
        mem_dir,
        client.fetch_thread_page,
        resolve_thread_id=lambda: client.resolve_thread_id(thread_key),
        limit=limit,
        persist=persist,
        extra_skip=extra,
        on_commit=commit if persist else None,
    )


def _message_file_index(name: str) -> int:
    match = _MESSAGE_FILE_RE.search(name.replace("\\", "/").split("/")[-1])
    return int(match.group(1)) if match else 10**9


def _score_export_thread(folder_name: str, meta: dict | None, key: str) -> int:
    """Higher is a better match. 0 means this folder is not the requested thread."""
    want = key.lstrip("@").strip().casefold()
    folder = (folder_name or "").casefold()
    if not want:
        return 0
    if folder == want:
        return 100
    if folder.startswith(want + "_"):
        return 90
    if folder.endswith("_" + want):
        return 85
    if isinstance(meta, dict):
        if str(meta.get("title") or "").casefold() == want:
            return 80
        for part in meta.get("participants") or []:
            if isinstance(part, dict) and str(part.get("name") or "").casefold() == want:
                return 70
    return 0


def _select_scored_thread(scored: list[tuple[int, str]], thread_key: str, labels: list[str]) -> str:
    if not scored:
        preview = ", ".join(labels[:20]) or "(none)"
        raise DmNotFound(
            f"No thread in the export matched {thread_key}. "
            f"Folders seen: {preview}. "
            "Pass the other account's username or the thread folder name."
        )
    scored = sorted(scored, key=lambda pair: (-pair[0], pair[1]))
    top = scored[0][0]
    best = [label for score, label in scored if score == top]
    if len(best) > 1:
        preview = ", ".join(best[:20])
        raise DmError(
            f"Several threads matched {thread_key}: {preview}. "
            "Pass the thread folder name, or point --from-export at that folder."
        )
    return best[0]


def _thread_dirs_under(root: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for message in root.rglob("message_*.json"):
        parent = message.parent
        if parent in seen:
            continue
        seen.add(parent)
        found.append(parent)
    inbox_dirs = [path for path in found if "inbox" in path.parts]
    return inbox_dirs or found


def _messages_from_decoded(files_data: list[dict]) -> list[dict]:
    """files_data is already in message_1, message_2, ... order (newest file first)."""
    messages: list[dict] = []
    for data in files_data:
        raw = data.get("messages") if isinstance(data, dict) else None
        if isinstance(raw, list):
            messages.extend(msg for msg in raw if isinstance(msg, dict))
    return messages


def _load_export_from_dir(root: Path, thread_key: str, *, force_this_dir: bool) -> list[dict]:
    if force_this_dir:
        files = sorted(root.glob("message_*.json"), key=lambda path: _message_file_index(path.name))
        if not files:
            raise DmNotFound(f"No message_*.json files in {root}.")
        decoded = [decode_export_json(path.read_bytes()) for path in files]
        dm_log(f"export thread folder {root.name} ({len(files)} file(s))")
        return _messages_from_decoded([d for d in decoded if isinstance(d, dict)])

    dirs = _thread_dirs_under(root)
    if not dirs:
        raise DmNotFound(f"No messages/inbox threads under {root}.")

    def meta_for(path: Path) -> dict:
        files = sorted(path.glob("message_*.json"), key=lambda item: _message_file_index(item.name))
        if not files:
            return {}
        data = decode_export_json(files[0].read_bytes())
        return data if isinstance(data, dict) else {}

    scored = []
    for path in dirs:
        score = _score_export_thread(path.name, meta_for(path), thread_key)
        if score:
            scored.append((score, str(path)))
    winner = Path(_select_scored_thread(scored, thread_key, [path.name for path in dirs]))
    files = sorted(winner.glob("message_*.json"), key=lambda path: _message_file_index(path.name))
    dm_log(f"export thread {winner.name} ({len(files)} file(s))")
    decoded = [decode_export_json(path.read_bytes()) for path in files]
    return _messages_from_decoded([d for d in decoded if isinstance(d, dict)])


def _load_export_from_zip(path: Path, thread_key: str) -> list[dict]:
    with zipfile.ZipFile(path) as zf:
        names = [
            name for name in zf.namelist()
            if not name.endswith("/") and _MESSAGE_FILE_RE.search(name.replace("\\", "/").split("/")[-1])
        ]
        groups: dict[str, list[str]] = {}
        for name in names:
            folder = name.rsplit("/", 1)[0] if "/" in name else ""
            groups.setdefault(folder, []).append(name)
        inbox_groups = {
            folder: files for folder, files in groups.items()
            if "inbox" in folder.replace("\\", "/").split("/")
        }
        use = inbox_groups or groups
        if not use:
            raise DmNotFound(f"No messages/inbox threads inside {path.name}.")

        def folder_label(folder: str) -> str:
            if not folder:
                return path.stem
            return folder.replace("\\", "/").rstrip("/").split("/")[-1]

        def read_meta(folder: str) -> dict:
            files = sorted(use[folder], key=lambda name: _message_file_index(name))
            data = decode_export_json(zf.read(files[0]))
            return data if isinstance(data, dict) else {}

        scored = []
        for folder in use:
            score = _score_export_thread(folder_label(folder), read_meta(folder), thread_key)
            if score:
                scored.append((score, folder))
        winner = _select_scored_thread(scored, thread_key, [folder_label(folder) for folder in use])
        files = sorted(use[winner], key=lambda name: _message_file_index(name))
        dm_log(f"export thread {folder_label(winner)} ({len(files)} file(s) in zip)")
        decoded = []
        for name in files:
            data = decode_export_json(zf.read(name))
            if isinstance(data, dict):
                decoded.append(data)
        return _messages_from_decoded(decoded)


def load_export_thread_messages(path: Path, thread_key: str) -> list[dict]:
    """Messages from an unzipped download or a .zip, newest file first.

    Within each message_N.json the export lists messages newest first, and
    message_1.json is the newest file, so the returned list is newest first.
    """
    path = path.expanduser()
    if not path.exists():
        raise DmNotFound(f"export path not found: {path}")
    if path.is_file() and path.suffix.lower() == ".zip":
        return _load_export_from_zip(path, thread_key)
    if path.is_file() and path.name.lower().startswith("message_") and path.suffix.lower() == ".json":
        return _load_export_from_dir(path.parent, thread_key, force_this_dir=True)
    if path.is_dir() and any(path.glob("message_*.json")):
        return _load_export_from_dir(path, thread_key, force_this_dir=True)
    if path.is_dir():
        return _load_export_from_dir(path, thread_key, force_this_dir=False)
    raise DmError(f"export path is not a folder, zip, or message JSON: {path}")


def collect_export_urls(
    path: Path,
    thread_key: str,
    mem_dir: Path,
    *,
    limit: int | None = None,
    extra_skip: set[str] | None = None,
) -> list[str]:
    """Newest-first post URLs from an Instagram data download, deduped."""
    messages = load_export_thread_messages(path, thread_key)
    skip = captured_shortcodes(mem_dir)
    if extra_skip:
        skip |= set(extra_skip)
    found: list[str] = []
    seen = set(skip)
    for msg in messages:
        for url in extract_urls_from_export_message(msg):
            code = post_code(url)
            if not code or code in seen:
                continue
            seen.add(code)
            found.append(url)
            if limit is not None and len(found) >= limit:
                return found
    return found


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def process_url(url: str, mem_dir: Path, cookies: Path | None,
                transcribe: bool = True, ocr: bool = True,
                directive: str = "", follow: bool = True) -> tuple[dict | None, GapLog]:
    """Process one URL. Returns (index_entry_or_None, gaplog).

    `directive` is the optional per-post instruction from the inbox telling Claude
    how to use this note; it is stored verbatim in the note and the index.
    """
    code = shortcode(url)
    gaps = GapLog(code)

    meta = fetch_metadata(url, gaps, cookies)
    if meta is None:
        return None, gaps

    caption, transcript = extract_caption_transcript(meta, gaps)
    ocr_text, onscreen, screenshots = "", "", []

    # Image/carousel posts have no audio or video track; only reels do. Skip the
    # video path for them so we don't emit misleading "no audio / no transcript" gaps.
    # (yt-dlp still lists a requested_downloads entry for image posts, so that field
    # can't be trusted; real media shows up as playable formats or a duration.)
    has_video = bool(meta.get("formats") or meta.get("duration"))

    if has_video:
        # One download serves both: Whisper transcript (only if IG gave none) plus
        # informative keyframes (OCR'd for on-screen text and saved as screenshots).
        need_tx = transcribe and not transcript
        whisper_text, onscreen, screenshots = capture_video(
            url, code, mem_dir, gaps, cookies, want_transcript=need_tx, want_frames=ocr)
        if not transcript and whisper_text:
            transcript = whisper_text
        if onscreen:
            # Content was captured on-screen; a missing speech transcript is then
            # expected, not a shortfall, so drop the now-moot empty-transcript gap.
            gaps.gaps = [g for g in gaps.gaps if g.kind != "empty_transcript"]
    elif ocr:
        # Photo/carousel posts: read any text baked into the images (gallery-dl).
        ocr_text = ocr_images(url, code, gaps, cookies)

    # Only a video post owes us a spoken transcript, and only if we also got no
    # on-screen text or image text from it; image posts legitimately have none.
    if has_video and not transcript and not ocr_text and not onscreen:
        gaps.add("no_transcript",
                 "No spoken-word transcript available (IG captions absent and audio transcription did not yield text).")

    # Follow the references the post gives us (GitHub repos, links) to pull real info.
    references = ""
    if follow:
        combined = "\n".join(t for t in (caption, transcript, ocr_text, onscreen) if t)
        references = follow_references(combined, gaps)

    note = build_note(url, code, meta, caption, transcript, ocr_text, gaps,
                      directive, onscreen, screenshots, references)
    filename = f"{code}.md"
    (mem_dir / filename).write_text(note, encoding="utf-8")

    title = note.split("\n", 1)[0].lstrip("# ").strip()
    entry = {
        "url": url, "file": filename, "title": title,
        "author": meta.get("uploader") or "unknown",
        "saved": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "gaps": [g.kind for g in gaps.gaps],
        "directive": directive,
    }
    return entry, gaps


def doctor(transcribe: bool, ocr: bool) -> None:
    """Report which capabilities are available. Warns (does not fail) on gaps."""
    import shutil

    def have(cmd): return shutil.which(cmd) is not None

    def have_mod(mod):
        try:
            __import__(mod); return True
        except ImportError:
            return False

    print("Preflight check:")
    yt = have("yt-dlp") or have_mod("yt_dlp")  # launcher on PATH, or importable module
    print(f"  [{'ok' if yt else 'XX'}] yt-dlp        — fetching posts {'' if yt else '(REQUIRED; pip3 install yt-dlp)'}")
    print("  [ok] dm thread     : shared post links via --from-dm (cookies) or --from-export")
    if transcribe:
        ff = have("ffmpeg"); fw = have_mod("faster_whisper")
        ok = ff and fw
        note = "" if ok else "(transcription disabled; " + \
            ", ".join(x for x in [("" if ff else "install ffmpeg"), ("" if fw else "pip3 install faster-whisper")] if x) + ")"
        print(f"  [{'ok' if ok else '--'}] whisper       — audio transcription fallback {note}")
    if ocr:
        ts = have("tesseract"); pt = have_mod("pytesseract"); pil = have_mod("PIL"); gd = have_mod("gallery_dl")
        ok = ts and pt and pil and gd
        note = "" if ok else "(OCR degraded; " + \
            ", ".join(x for x in [("" if ts else "install tesseract"),
                                  ("" if (pt and pil) else "pip3 install pytesseract Pillow"),
                                  ("" if gd else "pip3 install gallery-dl (fetches Instagram images)")] if x) + ")"
        print(f"  [{'ok' if ok else '--'}] tesseract OCR — text inside carousel images {note}")
    if not yt:
        print("  yt-dlp is required; nothing will process without it.", file=sys.stderr)
    print()


def process(mem_dir: Path, inbox: Path, cookies: Path | None, transcribe: bool = True,
            ocr: bool = True, keep_inbox: bool = False, follow: bool = True) -> int:
    mem_dir.mkdir(parents=True, exist_ok=True)
    doctor(transcribe, ocr)
    entries = read_inbox(inbox)
    if not entries:
        print(f"No URLs found in {inbox}")
        return 0

    processed = load_processed(mem_dir)
    new_count, gap_posts = 0, 0
    for url, directive in entries:
        code = shortcode(url)
        if code in processed:
            print(f"skip  {code}  (already saved)")
            continue
        entry, gaps = process_url(url, mem_dir, cookies, transcribe, ocr, directive, follow)
        if entry is None:
            print(f"FAIL  {code}  (see GAP lines above)")
            continue
        processed[code] = entry
        new_count += 1
        if gaps:
            gap_posts += 1
        flag = " [partial]" if gaps else ""
        print(f"ok    {code}{flag}  {entry['title'][:50]}")

    save_processed(mem_dir, processed)
    update_index(mem_dir, processed)

    # Remove every link already in the library (saved, gaps or not) from the inbox,
    # so it is never reprocessed or duplicated; only links that failed to save stay.
    if not keep_inbox:
        done = {e["url"] for e in processed.values()}
        pruned = prune_inbox(inbox, done)
        if pruned:
            print(f"pruned {pruned} processed link(s) from {inbox}")

    print(f"\n{new_count} new ({gap_posts} with gaps), {len(processed)} total -> {mem_dir}")
    index_path = (mem_dir / INDEX_NAME).resolve()
    print(f"Wire into Claude Code: ~/.claude/INSTAGRAM.md should contain:\n  @{index_path}")
    return new_count


def _validate_dm_args(args) -> None:
    using_dm = bool(args.from_dm or args.from_export)
    if args.dm_limit is not None and args.dm_limit < 1:
        print("--dm-limit must be a positive integer.", file=sys.stderr)
        sys.exit(2)
    if args.from_export and not args.from_dm:
        print("--from-export needs --from-dm to name the thread (username or folder).", file=sys.stderr)
        sys.exit(2)
    if args.dry_run and not using_dm:
        print("--dry-run is for --from-dm / --from-export.", file=sys.stderr)
        sys.exit(2)
    if args.dm_limit is not None and not using_dm:
        print("--dm-limit is for --from-dm / --from-export.", file=sys.stderr)
        sys.exit(2)
    if args.urls and using_dm:
        print("Pass URLs on the command line or use --from-dm, not both.", file=sys.stderr)
        sys.exit(2)
    if args.from_dm and not args.from_export and not args.cookies:
        print("--from-dm needs --cookies cookies.txt (Netscape format).", file=sys.stderr)
        sys.exit(2)
    if args.from_export and not args.from_export.exists():
        print(f"export path not found: {args.from_export}", file=sys.stderr)
        sys.exit(2)


def _run_dm_source(args, mem_dir: Path, inbox: Path) -> None:
    """Collect DM URLs into the inbox, then run the normal capture flow.

    --dry-run prints the URLs and does not write the inbox, the resume cursor,
    or any notes. Export mode does not use the network.
    """
    limit = args.dm_limit
    committed = {"n": 0}

    def note_commit(count: int) -> None:
        committed["n"] += count

    try:
        if args.from_export:
            urls = collect_export_urls(
                args.from_export,
                args.from_dm,
                mem_dir,
                limit=limit,
                extra_skip=inbox_shortcodes(inbox),
            )
            if not args.dry_run:
                note_commit(enqueue_urls(inbox, urls))
        else:
            urls = gather_live_urls(
                args.from_dm,
                args.cookies,
                mem_dir,
                inbox,
                limit,
                persist=not args.dry_run,
                on_commit=(lambda batch: note_commit(len(batch))) if not args.dry_run else None,
            )
    except DmError as exc:
        print(str(exc), file=sys.stderr)
        if committed["n"] and not args.dry_run:
            print("Capturing links queued before the DM pull stopped.", file=sys.stderr)
            process(
                mem_dir, inbox, args.cookies,
                transcribe=not args.no_transcribe, ocr=not args.no_ocr,
                keep_inbox=args.keep_inbox, follow=not args.no_follow,
            )
        sys.exit(1 if isinstance(exc, DmRateLimit) else 2)

    if args.dry_run:
        for url in urls:
            print(url)
        print(f"{len(urls)} url(s); dry-run, nothing captured", file=sys.stderr)
        return

    if urls:
        dm_log(f"queued {len(urls)} new link(s)")
    if not read_inbox(inbox):
        print("No new post links to capture.")
        return
    process(
        mem_dir, inbox, args.cookies,
        transcribe=not args.no_transcribe, ocr=not args.no_ocr,
        keep_inbox=args.keep_inbox, follow=not args.no_follow,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn Instagram links into Claude Code memory (gap-aware).")
    parser.add_argument("--mem-dir", type=Path, default=DEFAULT_MEM_DIR,
                        help=f"memory directory (default: {DEFAULT_MEM_DIR})")
    parser.add_argument("--inbox", type=Path, default=None,
                        help="inbox file of URLs (default: <mem-dir>/inbox.txt)")
    parser.add_argument("--cookies", type=Path, default=None,
                        help="Netscape cookies.txt for login-required posts")
    parser.add_argument("--from-dm", default=None, metavar="THREAD",
                        help="pull shared post/reel/carousel links from this DM thread "
                             "(other account's username, numeric thread id, or "
                             "instagram.com/direct/t/<id>/ URL)")
    parser.add_argument("--from-export", type=Path, default=None, metavar="PATH",
                        help="read that thread from an Instagram data download "
                             "(unzipped folder or .zip); use with --from-dm")
    parser.add_argument("--dm-limit", type=int, default=None, metavar="N",
                        help="take at most N new links from the DM thread this run (newest first)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --from-dm/--from-export, print the URLs and exit "
                             "(no capture, inbox unchanged, resume cursor unchanged)")
    parser.add_argument("--no-transcribe", action="store_true",
                        help="skip Whisper audio transcription fallback (faster)")
    parser.add_argument("--no-ocr", action="store_true",
                        help="skip OCR of text in carousel/images (faster)")
    parser.add_argument("--doctor", action="store_true",
                        help="check dependencies/capabilities and exit")
    parser.add_argument("--keep-inbox", action="store_true",
                        help="don't remove processed links from the inbox after the run")
    parser.add_argument("--no-follow", action="store_true",
                        help="skip following GitHub repos / links found in the post (no network enrichment)")
    parser.add_argument("--use", default="", metavar="DIRECTIVE",
                        help="directive for how Claude should use the URL(s) passed on the command "
                             "line (same as the inbox '| directive' syntax; applied to each URL)")
    parser.add_argument("urls", nargs="*", help="URLs to process directly (bypasses inbox)")
    args = parser.parse_args()

    if args.doctor:
        doctor(transcribe=not args.no_transcribe, ocr=not args.no_ocr)
        return

    mem_dir = args.mem_dir
    inbox = args.inbox or (mem_dir / INBOX_NAME)
    if args.cookies and not args.cookies.exists():
        print(f"cookies file not found: {args.cookies}", file=sys.stderr)
        sys.exit(2)

    _validate_dm_args(args)

    if args.from_dm or args.from_export:
        _run_dm_source(args, mem_dir, inbox)
        return

    if args.urls:
        mem_dir.mkdir(parents=True, exist_ok=True)
        suffix = f" | {args.use}" if args.use else ""
        inbox.write_text("\n".join(u + suffix for u in args.urls) + "\n", encoding="utf-8")

    process(mem_dir, inbox, args.cookies, transcribe=not args.no_transcribe,
            ocr=not args.no_ocr, keep_inbox=args.keep_inbox, follow=not args.no_follow)


if __name__ == "__main__":
    main()
