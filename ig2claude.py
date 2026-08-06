#!/usr/bin/env python3
"""ig2claude — turn Instagram links into Claude Code memory, gap-aware.

Workflow:
  1. Share IG reels/posts/carousels to yourself, copy the links.
  2. Paste them into <mem-dir>/inbox.txt (default ./ig-memory/inbox.txt), one per line.
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
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MEM_DIR = Path("ig-memory")  # project-relative; override with --mem-dir
INDEX_NAME = "index.md"
INBOX_NAME = "inbox.txt"
PROCESSED_NAME = ".processed.json"

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Turn Instagram links into Claude Code memory (gap-aware).")
    parser.add_argument("--mem-dir", type=Path, default=DEFAULT_MEM_DIR,
                        help=f"memory directory (default: {DEFAULT_MEM_DIR})")
    parser.add_argument("--inbox", type=Path, default=None,
                        help="inbox file of URLs (default: <mem-dir>/inbox.txt)")
    parser.add_argument("--cookies", type=Path, default=None,
                        help="Netscape cookies.txt for login-required posts")
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

    if args.urls:
        mem_dir.mkdir(parents=True, exist_ok=True)
        suffix = f" | {args.use}" if args.use else ""
        inbox.write_text("\n".join(u + suffix for u in args.urls) + "\n", encoding="utf-8")

    process(mem_dir, inbox, args.cookies, transcribe=not args.no_transcribe,
            ocr=not args.no_ocr, keep_inbox=args.keep_inbox, follow=not args.no_follow)


if __name__ == "__main__":
    main()
