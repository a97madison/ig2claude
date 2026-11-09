"""DM link collection. Fixtures are synthetic: they are not real Instagram payloads."""

import json
import random
import zipfile
from pathlib import Path

import pytest

import ig2claude

FIX = Path(__file__).parent / "fixtures"

REEL = "https://www.instagram.com/reel/REELCODE1/"
CAROUSEL = "https://www.instagram.com/p/CAROUSEL1/"
NESTED = "https://www.instagram.com/reel/NESTEDCLIP/"
XMA_POST = "https://www.instagram.com/p/XMAPOST99/"
XMA_REEL = "https://www.instagram.com/reel/XMAREEL55/"
TEXT_REEL = "https://www.instagram.com/reel/TEXTREEL7/"
IGTV = "https://www.instagram.com/tv/IGTVCODE1/"
TYPED = "https://www.instagram.com/p/TYPEDPOST1/"
FELIX = "https://www.instagram.com/tv/FELIX999/"

EXPORT_REEL = "https://www.instagram.com/reel/REELxxxx1/"
EXPORT_POST = "https://www.instagram.com/p/POST2222/"


def _page(items, cursor, has_older, thread_id="999001"):
    return {
        "status": "ok",
        "thread": {
            "thread_id": thread_id,
            "items": items,
            "oldest_cursor": cursor,
            "has_older": has_older,
        },
    }


def _text(item_id, body, ts):
    return {"item_id": item_id, "item_type": "text", "timestamp": ts, "text": body}


def _reel(item_id, code, ts):
    return {
        "item_id": item_id,
        "item_type": "media_share",
        "timestamp": ts,
        "media_share": {"code": code, "media_type": 2, "product_type": "clips"},
    }


def test_extract_urls_from_sample_dm_api_json():
    payload = json.loads((FIX / "dm_thread_page.json").read_text(encoding="utf-8"))
    urls = ig2claude.extract_urls_from_dm_payload(payload)
    assert urls == [REEL, CAROUSEL, NESTED, XMA_POST, XMA_REEL, TEXT_REEL, IGTV, TYPED, FELIX]
    assert "STORYCODESHOULDNOTAPPEAR" not in " ".join(urls)
    assert all("example.com" not in url for url in urls)


def test_canonical_post_url_folds_variants_and_drops_non_posts():
    assert ig2claude.canonical_post_url("https://www.instagram.com/reels/ABCDE12345/?igsh=1") == (
        "https://www.instagram.com/reel/ABCDE12345/"
    )
    assert ig2claude.canonical_post_url("https://instagram.com/share/OPAQUE99") is None
    assert ig2claude.canonical_post_url("https://www.instagram.com/stories/someone/123/") is None


def test_export_json_mojibake_and_urls():
    raw = (FIX / "export_message_1.json").read_bytes()
    data = ig2claude.decode_export_json(raw)
    assert data["participants"][1]["name"] == "José Learning"
    assert data["title"] == "José Learning"
    # Already-correct text is left alone.
    assert ig2claude.fix_export_text("José Learning") == "José Learning"
    assert ig2claude.fix_export_text("plain ascii") == "plain ascii"

    urls = []
    for msg in data["messages"]:
        urls.extend(ig2claude.extract_urls_from_export_message(msg))
    urls = ig2claude._unique_urls(urls)
    assert urls == [EXPORT_REEL, EXPORT_POST]


def test_export_directory_order_dedupe_and_zip(tmp_path):
    root = tmp_path / "download" / "messages" / "inbox"
    thread = root / "jose_learning_9"
    other = root / "someone_else_1"
    thread.mkdir(parents=True)
    other.mkdir()
    message_1 = (FIX / "export_message_1.json").read_bytes()
    (thread / "message_1.json").write_bytes(message_1)
    older = {
        "participants": [{"name": "Main Account"}, {"name": "José Learning"}],
        "title": "José Learning",
        "messages": [
            {
                "sender_name": "Main Account",
                "timestamp_ms": 1600000000000,
                "content": "older carousel https://www.instagram.com/p/SLIDE9999/",
            }
        ],
    }
    (thread / "message_2.json").write_text(json.dumps(older), encoding="utf-8")
    (other / "message_1.json").write_text(
        json.dumps({
            "participants": [{"name": "Other"}],
            "title": "Other",
            "messages": [{
                "content": "https://www.instagram.com/p/NOTTHISONE/",
            }],
        }),
        encoding="utf-8",
    )

    mem = tmp_path / "mem"
    mem.mkdir()
    # POST2222 is already captured, both as a note and in the index.
    (mem / "POST2222.md").write_text("# already\n", encoding="utf-8")
    (mem / ig2claude.PROCESSED_NAME).write_text(
        json.dumps({"POST2222": {"url": EXPORT_POST, "file": "POST2222.md"}}),
        encoding="utf-8",
    )

    urls = ig2claude.collect_export_urls(tmp_path / "download", "José Learning", mem)
    assert urls == [EXPORT_REEL, "https://www.instagram.com/p/SLIDE9999/"]

    # Same bytes from a zip, limited to the newest new URL.
    zpath = tmp_path / "export.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(thread / "message_1.json", "messages/inbox/jose_learning_9/message_1.json")
        zf.write(thread / "message_2.json", "messages/inbox/jose_learning_9/message_2.json")
        zf.write(other / "message_1.json", "messages/inbox/someone_else_1/message_1.json")
    limited = ig2claude.collect_export_urls(zpath, "jose_learning_9", mem, limit=1)
    assert limited == [EXPORT_REEL]


def test_export_ambiguous_thread_errors(tmp_path):
    root = tmp_path / "messages" / "inbox"
    for name in ("learningacct_1", "learningacct_2"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "message_1.json").write_text(
            json.dumps({
                "participants": [{"name": "Learning"}],
                "messages": [{"content": "https://www.instagram.com/p/AAAA1111/"}],
            }),
            encoding="utf-8",
        )
    with pytest.raises(ig2claude.DmError, match="Several threads"):
        ig2claude.load_export_thread_messages(tmp_path, "learningacct")


def test_dedupe_against_notes_preserves_newest_first(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / ig2claude.PROCESSED_NAME).write_text(
        json.dumps({"REELCODE1": {"url": REEL, "file": "REELCODE1.md"}}),
        encoding="utf-8",
    )
    # Note on disk with no index entry is still treated as captured.
    (mem / "CAROUSEL1.md").write_text("# slide\n", encoding="utf-8")
    urls = [REEL, CAROUSEL, TEXT_REEL, TEXT_REEL, NESTED]
    assert ig2claude.dedupe_post_urls(urls, mem) == [TEXT_REEL, NESTED]


def test_resume_cursor_and_limit_does_not_skip_the_rest_of_a_page(tmp_path):
    """A limit stops inside a page without moving the cursor.

    The next run re-reads that page, skips what is now captured, and continues.
    A fully consumed page advances the cursor so the next run does not re-fetch it.
    """
    mem = tmp_path / "mem"
    mem.mkdir()
    pages = {
        None: _page(
            [
                _reel("1", "AAA11111", 300),
                _reel("2", "BBB22222", 200),
                _reel("3", "CCC33333", 100),
            ],
            cursor="CUR1",
            has_older=True,
        ),
        "CUR1": _page(
            [_text("4", "https://www.instagram.com/p/DDD44444/", 50)],
            cursor=None,
            has_older=False,
        ),
    }
    calls = []

    def fetch(thread_id, cursor):
        calls.append(cursor)
        return pages[cursor]

    sleeps = []
    urls = ig2claude.collect_thread_urls(
        "learningacct",
        mem,
        fetch,
        resolve_thread_id=lambda: "999001",
        limit=1,
        persist=True,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert urls == ["https://www.instagram.com/reel/AAA11111/"]
    state = json.loads((mem / ig2claude.DM_STATE_NAME).read_text(encoding="utf-8"))
    assert state["cursor"] is None  # page was not fully taken
    assert state["backfill_done"] is False
    assert state["thread_id"] == "999001"
    assert calls == [None]
    assert sleeps == []  # no second page, so no delay

    # AAA is now captured. The same page still has BBB and CCC; limit 1 takes BBB only.
    (mem / "AAA11111.md").write_text("# a\n", encoding="utf-8")
    calls.clear()
    urls = ig2claude.collect_thread_urls(
        "learningacct",
        mem,
        fetch,
        resolve_thread_id=lambda: (_ for _ in ()).throw(AssertionError("should reuse state")),
        limit=1,
        persist=True,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert urls == ["https://www.instagram.com/reel/BBB22222/"]
    assert calls == [None]

    (mem / "BBB22222.md").write_text("# b\n", encoding="utf-8")
    calls.clear()
    sleeps.clear()
    urls = ig2claude.collect_thread_urls(
        "learningacct",
        mem,
        fetch,
        resolve_thread_id=lambda: (_ for _ in ()).throw(AssertionError("should reuse state")),
        limit=None,
        persist=True,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    # CCC finishes the head page (cursor advances), then DDD finishes the backfill.
    assert urls == [
        "https://www.instagram.com/reel/CCC33333/",
        "https://www.instagram.com/p/DDD44444/",
    ]
    assert calls == [None, "CUR1"]
    assert len(sleeps) == 1
    assert ig2claude.DM_PAGE_DELAY_MIN <= sleeps[0] <= ig2claude.DM_PAGE_DELAY_MAX
    state = json.loads((mem / ig2claude.DM_STATE_NAME).read_text(encoding="utf-8"))
    assert state["backfill_done"] is True
    assert state["cursor"] is None
    assert state["newest_ts"] == 300


def test_incremental_after_backfill_stops_at_overlap(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    ig2claude.save_dm_state(mem, {
        "thread_key": "learningacct",
        "thread_id": "999001",
        "cursor": None,
        "backfill_done": True,
        "newest_ts": 1000,
    })
    calls = []

    def fetch(thread_id, cursor):
        calls.append(cursor)
        assert cursor is None  # do not walk the old backfill cursor
        return _page(
            [
                _reel("new", "NEW11111", 3000),
                _reel("mid", "MID22222", 2000),
                _reel("old", "OLD33333", 1000),
                _reel("older", "OLDER444", 500),
            ],
            cursor="SHOULD_NOT_FOLLOW",
            has_older=True,
        )

    urls = ig2claude.collect_thread_urls(
        "learningacct",
        mem,
        fetch,
        resolve_thread_id=lambda: (_ for _ in ()).throw(AssertionError("no resolve")),
        persist=True,
        sleep=lambda _s: (_ for _ in ()).throw(AssertionError("no extra page")),
        rng=random.Random(0),
    )
    assert urls == [
        "https://www.instagram.com/reel/NEW11111/",
        "https://www.instagram.com/reel/MID22222/",
    ]
    assert calls == [None]
    state = json.loads((mem / ig2claude.DM_STATE_NAME).read_text(encoding="utf-8"))
    assert state["newest_ts"] == 3000
    assert state["backfill_done"] is True


def test_switching_thread_resets_the_cursor(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    ig2claude.save_dm_state(mem, {
        "thread_key": "otheracct",
        "thread_id": "111",
        "cursor": "OLD",
        "backfill_done": False,
        "newest_ts": 5,
    })
    seen = []

    def fetch(thread_id, cursor):
        seen.append((thread_id, cursor))
        return _page([], cursor=None, has_older=False, thread_id=thread_id)

    ig2claude.collect_thread_urls(
        "learningacct",
        mem,
        fetch,
        resolve_thread_id=lambda: "999001",
        persist=True,
        sleep=lambda _s: None,
        rng=random.Random(0),
    )
    assert seen == [("999001", None)]
    state = json.loads((mem / ig2claude.DM_STATE_NAME).read_text(encoding="utf-8"))
    assert state["thread_key"] == "learningacct"
    assert state["thread_id"] == "999001"


def test_netscape_cookies_and_headers_have_no_password(tmp_path):
    text = "\n".join([
        "# Netscape HTTP Cookie File",
        "#HttpOnly_.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tSESSION%3AVALUE",
        ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRFVALUE",
        ".example.com\tTRUE\t/\tTRUE\t0\tsessionid\tNOT-IG",
    ])
    path = tmp_path / "cookies.txt"
    path.write_text(text, encoding="utf-8")
    cookies = ig2claude.load_netscape_cookies(path)
    assert cookies == {"sessionid": "SESSION%3AVALUE", "csrftoken": "CSRFVALUE"}
    headers = ig2claude.build_dm_headers(cookies, ig2claude.DM_INBOX_REFERER)
    assert "SESSION%3AVALUE" in headers["Cookie"]
    assert "password" not in headers["Cookie"].lower()
    assert headers["X-CSRFToken"] == "CSRFVALUE"
    assert headers["X-IG-App-ID"] == ig2claude.IG_WEB_APP_ID


def test_get_json_backs_off_then_succeeds_and_login_does_not():
    sleeps = []
    calls = {"n": 0}

    def http_get(url, headers, timeout=30):
        assert "sessionid=SECRETSESSION" in headers["Cookie"]
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {"message": "Please wait a few minutes before you try again.", "status": "fail"}
        return 200, {"status": "ok", "thread": {"items": [], "has_older": False}}

    payload = ig2claude.get_json(
        "https://www.instagram.com/api/v1/direct_v2/threads/1/",
        cookies={"sessionid": "SECRETSESSION", "csrftoken": "T"},
        referer="https://www.instagram.com/direct/inbox/",
        http_get=http_get,
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    assert payload["status"] == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1
    assert ig2claude.DM_BACKOFF_START * 0.8 <= sleeps[0] <= ig2claude.DM_BACKOFF_START * 1.2

    def login_get(url, headers, timeout=30):
        return 200, {"message": "login_required", "status": "fail"}

    login_sleeps = []
    with pytest.raises(ig2claude.DmLoginRequired) as raised:
        ig2claude.get_json(
            "https://www.instagram.com/api/v1/direct_v2/threads/1/",
            cookies={"sessionid": "SECRETSESSION"},
            referer="https://www.instagram.com/direct/inbox/",
            http_get=login_get,
            sleep=login_sleeps.append,
            rng=random.Random(0),
        )
    assert login_sleeps == []
    assert "SECRETSESSION" not in str(raised.value)


def test_resolve_username_pages_inbox_and_numeric_id_skips_network():
    calls = []

    def http_get(url, headers, timeout=30):
        calls.append(url)
        assert "direct_v2/inbox/" in url
        assert "thread_message_limit=1" in url
        if "cursor=CURBOX" in url:
            return 200, {
                "status": "ok",
                "inbox": {
                    "threads": [{
                        "thread_id": "777001",
                        "users": [{"username": "learningacct"}],
                    }],
                    "has_older": False,
                },
            }
        return 200, {
            "status": "ok",
            "inbox": {
                "threads": [{
                    "thread_id": "111001",
                    "users": [{"username": "someoneelse"}],
                }],
                "has_older": True,
                "oldest_cursor": "CURBOX",
            },
        }

    web = ig2claude.InstagramWeb(
        {"sessionid": "S", "csrftoken": "C"},
        sleep=lambda _s: None,
        rng=random.Random(0),
        http_get=http_get,
    )
    assert web.resolve_thread_id("@learningacct") == "777001"
    assert len(calls) == 2
    calls.clear()
    direct = "111111111111111111111111111111111111111"
    assert web.resolve_thread_id(f"https://www.instagram.com/direct/t/{direct}/") == direct
    assert calls == []


def test_thread_url_uses_older_direction_and_cursor():
    url = ig2claude.build_thread_url("555", "CUR 1")
    assert url.startswith("https://www.instagram.com/api/v1/direct_v2/threads/555/?")
    assert "direction=older" in url
    assert "visual_message_return_type=unseen" in url
    assert "limit=20" in url
    assert "cursor=CUR+1" in url or "cursor=CUR%201" in url


def _netscape(path: Path) -> Path:
    path.write_text(
        ".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tSECRETSESSION\n"
        ".instagram.com\tTRUE\t/\tTRUE\t0\tcsrftoken\tCSRF\n",
        encoding="utf-8",
    )
    return path


def test_cli_export_dry_run_lists_urls_without_writing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "export" / "messages" / "inbox" / "learningacct_9"
    root.mkdir(parents=True)
    (root / "message_1.json").write_bytes((FIX / "export_message_1.json").read_bytes())
    mem = tmp_path / "mem"
    monkeypatch.setattr("sys.argv", [
        "ig2claude.py",
        "--from-export", str(tmp_path / "export"),
        "--from-dm", "José Learning",
        "--dry-run",
        "--mem-dir", str(mem),
        "--dm-limit", "1",
    ])
    ig2claude.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [EXPORT_REEL]
    assert not mem.exists() or not any(mem.iterdir())


def test_cli_export_enqueues_and_captures(tmp_path, monkeypatch, capsys):
    root = tmp_path / "export" / "messages" / "inbox" / "learningacct_9"
    root.mkdir(parents=True)
    (root / "message_1.json").write_bytes((FIX / "export_message_1.json").read_bytes())
    mem = tmp_path / "mem"
    inbox = mem / "inbox.txt"
    inbox.parent.mkdir(parents=True)
    inbox.write_text("# keep me\n", encoding="utf-8")
    seen = {}

    def fake_process(mem_dir, inbox_path, cookies, transcribe=True, ocr=True, keep_inbox=False, follow=True):
        seen["text"] = inbox_path.read_text(encoding="utf-8")
        seen["cookies"] = cookies
        return 0

    monkeypatch.setattr(ig2claude, "process", fake_process)
    monkeypatch.setattr("sys.argv", [
        "ig2claude.py",
        "--from-export", str(tmp_path / "export"),
        "--from-dm", "José Learning",
        "--mem-dir", str(mem),
    ])
    ig2claude.main()
    assert "# keep me" in seen["text"]
    assert EXPORT_REEL in seen["text"]
    assert EXPORT_POST in seen["text"]
    assert "stories/" not in seen["text"]
    assert not (mem / ig2claude.DM_STATE_NAME).exists()


def test_cli_live_dry_run_and_rate_limit_resume(tmp_path, monkeypatch, capsys):
    cookies = _netscape(tmp_path / "cookies.txt")
    mem = tmp_path / "mem"
    calls = []

    def http_get(url, headers, timeout=30):
        assert "SECRETSESSION" in headers["Cookie"]
        assert "password" not in headers["Cookie"].lower()
        calls.append(url)
        if "direct_v2/inbox/" in url:
            return 200, {
                "status": "ok",
                "inbox": {
                    "threads": [{"thread_id": "555", "users": [{"username": "learningacct"}]}],
                    "has_older": False,
                },
            }
        if "direct_v2/threads/555/" in url and "cursor=" not in url:
            page = json.loads((FIX / "dm_thread_page.json").read_text(encoding="utf-8"))
            return 200, page
        if "direct_v2/threads/555/" in url and "cursor=" in url:
            return 429, {"message": "Please wait a few minutes before you try again.", "status": "fail"}
        raise AssertionError(url)

    monkeypatch.setattr(ig2claude, "http_get_json", http_get)
    monkeypatch.setattr(ig2claude.time, "sleep", lambda _s: None)

    monkeypatch.setattr("sys.argv", [
        "ig2claude.py",
        "--from-dm", "learningacct",
        "--cookies", str(cookies),
        "--dry-run",
        "--dm-limit", "2",
        "--mem-dir", str(mem),
    ])
    ig2claude.main()
    err = capsys.readouterr()
    assert err.out.splitlines() == [REEL, CAROUSEL]
    assert "SECRETSESSION" not in err.err
    assert not (mem / ig2claude.DM_STATE_NAME).exists()

    # A real run pages the fixture (has_older, so it asks for the next cursor),
    # hits the rate limit, keeps the first page's links, and still captures them.
    processed = {}

    def fake_process(mem_dir, inbox_path, cookies_path, transcribe=True, ocr=True, keep_inbox=False, follow=True):
        processed["inbox"] = inbox_path.read_text(encoding="utf-8")
        return 1

    monkeypatch.setattr(ig2claude, "process", fake_process)
    monkeypatch.setattr("sys.argv", [
        "ig2claude.py",
        "--from-dm", "learningacct",
        "--cookies", str(cookies),
        "--mem-dir", str(mem),
    ])
    with pytest.raises(SystemExit) as raised:
        ig2claude.main()
    assert raised.value.code == 1
    state = json.loads((mem / ig2claude.DM_STATE_NAME).read_text(encoding="utf-8"))
    assert state["thread_id"] == "555"
    assert state["cursor"] == "CURSOR_PAGE_2"
    assert state["backfill_done"] is False
    assert REEL in processed["inbox"]
    assert FELIX in processed["inbox"]
    assert "STORYCODESHOULDNOTAPPEAR" not in processed["inbox"]
    err = capsys.readouterr()
    assert "SECRETSESSION" not in err.err
    assert "rate limit" in err.err.lower() or "Re-run" in err.err


def test_cli_rejects_export_without_thread(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "ig2claude.py",
        "--from-export", str(tmp_path),
        "--mem-dir", str(tmp_path / "mem"),
    ])
    with pytest.raises(SystemExit) as raised:
        ig2claude.main()
    assert raised.value.code == 2
    assert "--from-dm" in capsys.readouterr().err


def test_doctor_mentions_dm_thread(capsys):
    ig2claude.doctor(transcribe=False, ocr=False)
    out = capsys.readouterr().out
    assert "dm thread" in out
    assert "--from-dm" in out
    assert "--from-export" in out
