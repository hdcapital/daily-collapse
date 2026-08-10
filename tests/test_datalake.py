"""Data-lake context scan tests."""
import sys
import types

import pytest

from src import datalake


@pytest.fixture(autouse=True)
def _clear_s3_cache():
    datalake.reset_s3_cache()
    yield
    datalake.reset_s3_cache()


class FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n=None):
        return self._data[:n] if n else self._data


class FakeS3:
    """Records how often the bucket is listed and which keys are downloaded.

    `ages` maps a key to how many days old it is (default: modified just now).
    """

    def __init__(self, objects: dict[str, bytes], fail_listing: bool = False, ages: dict | None = None):
        import threading

        self.objects = objects
        self.fail_listing = fail_listing
        self.ages = ages or {}
        self.list_calls = 0
        self.downloaded: list[str] = []
        self._lock = threading.Lock()

    def _modified(self, key):
        from datetime import datetime, timedelta, timezone

        if key in self.ages and self.ages[key] is None:
            return None  # object with no LastModified at all
        days = self.ages.get(key, 0)
        return datetime.now(timezone.utc) - timedelta(days=days)

    def get_paginator(self, name):
        outer = self

        class _P:
            def paginate(self, **kwargs):
                with outer._lock:
                    outer.list_calls += 1
                if outer.fail_listing:
                    raise RuntimeError("AccessDenied: s3:ListBucket")
                prefix = kwargs.get("Prefix") or ""
                delimiter = kwargs.get("Delimiter")
                contents, folders, seen_folders = [], [], set()
                for k in sorted(outer.objects):
                    if not k.startswith(prefix):
                        continue
                    rest = k[len(prefix):]
                    if delimiter and delimiter in rest:
                        # Rolled up into a CommonPrefix, like real S3.
                        folder = prefix + rest.split(delimiter, 1)[0] + delimiter
                        if folder not in seen_folders:
                            seen_folders.add(folder)
                            folders.append(folder)
                        continue
                    entry = {"Key": k, "Size": len(outer.objects[k])}
                    modified = outer._modified(k)
                    if modified is not None:
                        entry["LastModified"] = modified
                    contents.append(entry)
                # Two pages, to exercise pagination.
                mid = (len(contents) + 1) // 2
                yield {
                    "Contents": contents[:mid],
                    "CommonPrefixes": [{"Prefix": f} for f in folders],
                }
                yield {"Contents": contents[mid:]}

        return _P()

    def get_object(self, Bucket, Key):  # noqa: N803 - mirrors boto3's API
        self.downloaded.append(Key)
        return {"Body": FakeBody(self.objects[Key])}


def install_s3(monkeypatch, objects, fail_listing=False, bucket="my-lake", prefix=None, ages=None):
    fake = FakeS3(objects, fail_listing, ages)
    mod = types.ModuleType("boto3")
    mod.client = lambda name: fake
    monkeypatch.setitem(sys.modules, "boto3", mod)
    monkeypatch.setenv("DATALAKE_S3_BUCKET", bucket)
    if prefix is None:
        monkeypatch.delenv("DATALAKE_S3_PREFIX", raising=False)
    else:
        monkeypatch.setenv("DATALAKE_S3_PREFIX", prefix)
    return fake


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_matches_ticker_in_body(tmp_path):
    _write(tmp_path, "notes.md", "XYZ — thin balance sheet, expect a raise.")
    hits = datalake.scan_local("XYZ", str(tmp_path), 5, 4000)
    assert len(hits) == 1
    assert "thin balance sheet" in hits[0]["text"]
    assert hits[0]["source"].startswith("local:")


def test_matches_ticker_in_filename(tmp_path):
    _write(tmp_path, "XYZ-broker-note.txt", "no ticker mentioned in the body at all")
    assert len(datalake.scan_local("XYZ", str(tmp_path), 5, 4000)) == 1


def test_filename_substring_is_not_a_match(tmp_path):
    """'AI' must not match 'email-notes.md' just because the letters appear."""
    _write(tmp_path, "email-notes.md", "nothing relevant here")
    assert datalake.scan_local("AI", str(tmp_path), 5, 4000) == []


def test_word_boundary_in_body(tmp_path):
    _write(tmp_path, "notes.md", "CBAX is a different company entirely")
    assert datalake.scan_local("CBA", str(tmp_path), 5, 4000) == []


def test_binary_and_unknown_extensions_ignored(tmp_path):
    _write(tmp_path, "note.pdf", "XYZ mentioned")
    _write(tmp_path, "note.png", "XYZ mentioned")
    assert datalake.scan_local("XYZ", str(tmp_path), 5, 4000) == []


def test_respects_max_files(tmp_path):
    for i in range(10):
        _write(tmp_path, f"note{i}.md", "XYZ is mentioned")
    assert len(datalake.scan_local("XYZ", str(tmp_path), 3, 4000)) == 3


def test_zero_budget_returns_nothing(tmp_path):
    _write(tmp_path, "note.md", "XYZ is mentioned")
    assert datalake.scan_local("XYZ", str(tmp_path), 0, 4000) == []


def test_missing_directory_is_not_an_error():
    assert datalake.scan_local("XYZ", "/nonexistent/path", 5, 4000) == []
    assert datalake.scan_local("XYZ", "", 5, 4000) == []


def test_snippet_is_centred_on_the_ticker(tmp_path):
    text = "A" * 5000 + " XYZ crashed on the placement " + "B" * 5000
    _write(tmp_path, "big.md", text)
    hits = datalake.scan_local("XYZ", str(tmp_path), 5, 400)
    assert "XYZ crashed on the placement" in hits[0]["text"]
    assert len(hits[0]["text"]) <= 400


def test_subdirectories_are_scanned(tmp_path):
    _write(tmp_path, "sub/deeper/note.md", "XYZ is mentioned")
    assert len(datalake.scan_local("XYZ", str(tmp_path), 5, 4000)) == 1


def test_gather_context_disabled():
    assert datalake.gather_context("XYZ", {"datalake": {"enabled": False}}) == []
    assert datalake.gather_context("XYZ", {}) == []


def test_gather_context_local(tmp_path):
    _write(tmp_path, "note.md", "XYZ is mentioned")
    cfg = {"datalake": {"enabled": True, "local_dir": str(tmp_path), "max_files_per_ticker": 5, "max_chars_per_file": 100}}
    assert len(datalake.gather_context("XYZ", cfg)) == 1


def test_s3_disabled_without_bucket(monkeypatch):
    monkeypatch.delenv("DATALAKE_S3_BUCKET", raising=False)
    assert datalake.scan_s3("XYZ", 5, 4000) == []


# --------------------------------------------------------------- S3 scanning


OBJECTS = {
    "notes/XYZ-placement.md": b"XYZ raised at a 25% discount",
    "notes/ABC-note.md": b"ABC is unrelated",
    "notes/XYZ-drilling.txt": b"XYZ hit 12m at 3.1 g/t",
    "notes/scan.pdf": b"XYZ mentioned but wrong format",
}


def test_bucket_is_walked_once_across_many_tickers(monkeypatch):
    """The whole point of the caching: one walk per run, not one per stock."""
    fake = install_s3(monkeypatch, OBJECTS)
    datalake.scan_s3("XYZ", 5, 4000)
    calls_after_first = fake.list_calls
    assert calls_after_first > 0
    for ticker in ("ABC", "DEF", "GHI", "JKL"):
        datalake.scan_s3(ticker, 5, 4000)
    assert fake.list_calls == calls_after_first


def test_only_matching_objects_are_downloaded(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    hits = datalake.scan_s3("XYZ", 5, 4000)
    assert sorted(fake.downloaded) == ["notes/XYZ-drilling.txt", "notes/XYZ-placement.md"]
    assert len(hits) == 2
    # Listing is lexicographic (as on real S3), so drilling.txt comes first.
    assert hits[0]["source"] == "s3://my-lake/notes/XYZ-drilling.txt"
    assert "3.1 g/t" in hits[0]["text"]
    assert "25% discount" in hits[1]["text"]


def test_unmatched_ticker_downloads_nothing(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    assert datalake.scan_s3("ZZZ", 5, 4000) == []
    assert fake.downloaded == []


def test_non_text_extensions_skipped(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    datalake.scan_s3("XYZ", 5, 4000)
    assert "notes/scan.pdf" not in fake.downloaded


def test_oversized_objects_skipped(monkeypatch):
    fake = install_s3(monkeypatch, {"notes/XYZ-big.md": b"x" * 2_000_001})
    assert datalake.scan_s3("XYZ", 5, 4000) == []
    assert fake.downloaded == []


def test_s3_respects_max_files(monkeypatch):
    objs = {f"notes/XYZ-{i}.md": b"XYZ mentioned" for i in range(10)}
    fake = install_s3(monkeypatch, objs)
    assert len(datalake.scan_s3("XYZ", 3, 4000)) == 3
    assert len(fake.downloaded) == 3


def test_s3_zero_budget_skips_entirely(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    assert datalake.scan_s3("XYZ", 0, 4000) == []
    assert fake.list_calls == 0


def test_listing_failure_is_reported_once(monkeypatch, caplog):
    """A permissions error must not produce one warning per stock."""
    install_s3(monkeypatch, OBJECTS, fail_listing=True)
    with caplog.at_level("WARNING"):
        for ticker in ("XYZ", "ABC", "DEF"):
            assert datalake.scan_s3(ticker, 5, 4000) == []
    assert sum("listing failed" in r.message for r in caplog.records) == 1


def test_prefix_narrows_the_listing(monkeypatch):
    objs = dict(OBJECTS)
    objs["archive/XYZ-old.md"] = b"XYZ ancient history"
    fake = install_s3(monkeypatch, objs, prefix="notes/")
    datalake.scan_s3("XYZ", 5, 4000)
    assert "archive/XYZ-old.md" not in fake.downloaded


def test_download_failure_skips_that_file_only(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    original = fake.get_object

    def flaky(Bucket, Key):  # noqa: N803
        if Key.endswith("placement.md"):
            raise RuntimeError("transient S3 error")
        return original(Bucket=Bucket, Key=Key)

    fake.get_object = flaky
    hits = datalake.scan_s3("XYZ", 5, 4000)
    assert len(hits) == 1
    assert "drilling" in hits[0]["source"]


# ------------------------------------------------------- S3 age filter


AGED = {
    "notes/XYZ-today.md": b"XYZ placement announced today",
    "notes/XYZ-lastmonth.md": b"XYZ old commentary",
}


def test_stale_objects_are_excluded(monkeypatch):
    fake = install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    hits = datalake.scan_s3("XYZ", 5, 4000, max_age_days=7)
    assert [h["source"] for h in hits] == ["s3://my-lake/notes/XYZ-today.md"]
    assert fake.downloaded == ["notes/XYZ-today.md"]


def test_object_on_the_boundary_is_kept(monkeypatch):
    install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 6})
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=7)) == 2


def test_age_filter_off_by_zero(monkeypatch):
    install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=0)) == 2


def test_object_without_a_date_is_kept(monkeypatch):
    """Missing LastModified must not silently drop the note."""
    install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": None})
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=7)) == 2


def test_age_is_part_of_the_cache_key(monkeypatch):
    """Changing the window must re-list rather than reuse a differently-filtered one."""
    fake = install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=7)) == 1
    calls_after_first = fake.list_calls
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=0)) == 2
    assert fake.list_calls == 2 * calls_after_first  # a second, separate walk


def test_age_filtered_listing_still_cached_across_tickers(monkeypatch):
    fake = install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    datalake.scan_s3("XYZ", 5, 4000, max_age_days=7)
    calls_after_first = fake.list_calls
    for ticker in ("ABC", "DEF"):
        datalake.scan_s3(ticker, 5, 4000, max_age_days=7)
    assert fake.list_calls == calls_after_first


# ------------------------------------------------------- parallel bucket walk


FOLDERED = {
    "root-note-XYZ.md": b"XYZ loose note at the bucket root",
    "broker/XYZ-target-cut.md": b"XYZ downgraded",
    "broker/ABC-initiation.md": b"ABC initiated",
    "news/XYZ-halt.txt": b"XYZ trading halt",
    "news/deep/nested/XYZ-old-story.md": b"XYZ archive piece",
    "screens/weekly.csv": b"XYZ,ABC,DEF",
}


def test_foldered_bucket_is_fanned_out(monkeypatch):
    """Multiple top-level folders -> one probe + one walk per folder."""
    fake = install_s3(monkeypatch, FOLDERED)
    hits = datalake.scan_s3("XYZ", 10, 4000)
    # 1 probe + broker/ + news/ + screens/ = 4 listings
    assert fake.list_calls == 4
    # Completeness: root-level, shallow, and deeply nested keys all found.
    assert [h["source"] for h in hits] == [
        "s3://my-lake/broker/XYZ-target-cut.md",
        "s3://my-lake/news/XYZ-halt.txt",
        "s3://my-lake/news/deep/nested/XYZ-old-story.md",
        "s3://my-lake/root-note-XYZ.md",
    ]


def test_parallel_walk_result_is_sorted_and_deterministic(monkeypatch):
    """Thread timing must not change which notes reach the AI."""
    objects = {f"folder{i}/XYZ-note-{i}.md": b"XYZ" for i in range(20)}
    install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("XYZ", 50, 4000)
    sources = [h["source"] for h in hits]
    assert sources == sorted(sources)
    assert len(sources) == 20


def test_parallel_walk_applies_the_age_filter(monkeypatch):
    install_s3(monkeypatch, FOLDERED, ages={"news/XYZ-halt.txt": 30})
    hits = datalake.scan_s3("XYZ", 10, 4000, max_age_days=7)
    assert "s3://my-lake/news/XYZ-halt.txt" not in [h["source"] for h in hits]
    assert len(hits) == 3


def test_cap_respected_in_parallel_mode(monkeypatch, caplog):
    monkeypatch.setattr(datalake, "MAX_KEYS_INDEXED", 3)
    objects = {f"folder{i}/XYZ-{j}.md": b"XYZ" for i in range(4) for j in range(5)}
    install_s3(monkeypatch, objects)
    with caplog.at_level("WARNING"):
        hits = datalake.scan_s3("XYZ", 50, 4000)
    assert len(hits) <= 3
    assert any("capped" in r.message for r in caplog.records)


def test_too_many_folders_falls_back_to_flat_walk(monkeypatch):
    """Past the fan-out bound the walk goes flat — complete, with no duplicates."""
    monkeypatch.setattr(datalake, "MAX_FANOUT_FOLDERS", 2)
    fake = install_s3(monkeypatch, FOLDERED)
    hits = datalake.scan_s3("XYZ", 10, 4000)
    sources = [h["source"] for h in hits]
    assert len(sources) == len(set(sources)) == 4
    assert fake.list_calls == 2  # probe + one flat re-walk


def test_growth_warning_fires_on_a_big_bucket(monkeypatch, caplog):
    monkeypatch.setattr(datalake, "GROWTH_WARN_OBJECTS", 4)
    install_s3(monkeypatch, FOLDERED)
    with caplog.at_level("WARNING"):
        datalake.scan_s3("XYZ", 10, 4000)
    assert any("DATALAKE_S3_PREFIX" in r.message for r in caplog.records)


# ------------------------------------------------- manifest-indexed lake reads


import json as _json
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

MANIFESTS_CFG = {"prefix": "market-data/", "markets": ["asx"]}


def _date(days_ago=0):
    return (_dt.now(_tz.utc).date() - _td(days=days_ago)).isoformat()


def _doc(title, text):
    return _json.dumps({"title": title, "content": {"text": text}}).encode()


def _manifest_line(ticker, key, noise=False):
    return _json.dumps({"ticker": ticker, "key": key, "is_admin_noise": noise})


def _lake(days_ago=0, extra_lines=(), market="asx"):
    """A market-ingestion-shaped lake: hashed doc filenames + a daily manifest."""
    y, m, d = _date(days_ago).split("-")
    doc_key = f"market-data/documents/asx/{y}/{m}/{d}/9f86d081884c7d659a2feaa0c55ad015.json"
    lines = [_manifest_line("ARF", doc_key), *extra_lines]
    return {
        doc_key: _doc("Capital raising announced", "ARF placement at a 25% discount."),
        f"market-data/manifests/{market}/{_date(days_ago)}.jsonl": ("\n".join(lines) + "\n").encode(),
    }


def test_manifest_index_needs_no_bucket_walk(monkeypatch):
    """The headline property: zero ListObjects calls, ever."""
    fake = install_s3(monkeypatch, _lake())
    hits = datalake.scan_s3("ARF", 5, 4000, max_age_days=7, manifests=MANIFESTS_CFG)
    assert fake.list_calls == 0
    assert len(hits) == 1
    assert "Capital raising announced" in hits[0]["text"]
    assert "25% discount" in hits[0]["text"]


def test_manifest_finds_docs_with_hashed_filenames(monkeypatch):
    """Doc keys are MD5 hashes — filename matching can't find them; the manifest can."""
    objects = _lake()
    doc_key = next(k for k in objects if k.endswith(".json") and "documents" in k)
    assert "ARF" not in doc_key  # the very case the old matcher was blind to
    install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("ARF", 5, 4000, manifests=MANIFESTS_CFG)
    assert hits and hits[0]["source"].endswith(doc_key)


def test_manifest_ticker_match_is_exact(monkeypatch):
    y, m, d = _date().split("-")
    other = f"market-data/documents/asx/{y}/{m}/{d}/aaaa.json"
    objects = _lake(extra_lines=[_manifest_line("ARF2", other)])
    objects[other] = _doc("Other company", "ARF2 news")
    install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("ARF", 5, 4000, manifests=MANIFESTS_CFG)
    assert len(hits) == 1
    assert "ARF2" not in hits[0]["text"]


def test_admin_noise_is_skipped(monkeypatch):
    y, m, d = _date().split("-")
    noise = f"market-data/documents/asx/{y}/{m}/{d}/bbbb.json"
    objects = _lake(extra_lines=[_manifest_line("ARF", noise, noise=True)])
    objects[noise] = _doc("Change of registry address", "administrative")
    install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("ARF", 5, 4000, manifests=MANIFESTS_CFG)
    assert len(hits) == 1
    assert "registry" not in hits[0]["text"]


def test_manifest_window_and_weekend_gaps(monkeypatch):
    """Only dates inside the window are read; missing dates are normal."""
    objects = _lake(days_ago=2)  # manifest exists 2 days ago only
    objects.update(_lake(days_ago=30, market="asx"))  # and one far outside the window
    old_manifest = f"market-data/manifests/asx/{_date(30)}.jsonl"
    fake = install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("ARF", 5, 4000, max_age_days=7, manifests=MANIFESTS_CFG)
    assert len(hits) == 1  # today's gap tolerated, 30-day-old manifest never fetched
    assert old_manifest not in fake.downloaded


def test_manifest_max_files_prefers_newest(monkeypatch):
    objects = {}
    for days_ago in (0, 1, 2):
        y, m, d = _date(days_ago).split("-")
        key = f"market-data/documents/asx/{y}/{m}/{d}/doc{days_ago}.json"
        objects[key] = _doc(f"Announcement {days_ago}d ago", "ARF news")
        objects[f"market-data/manifests/asx/{_date(days_ago)}.jsonl"] = (
            _manifest_line("ARF", key) + "\n"
        ).encode()
    install_s3(monkeypatch, objects)
    hits = datalake.scan_s3("ARF", 2, 4000, manifests=MANIFESTS_CFG)
    assert len(hits) == 2
    assert "0d ago" in hits[0]["text"] and "1d ago" in hits[1]["text"]


def test_manifest_index_cached_across_tickers(monkeypatch):
    fake = install_s3(monkeypatch, _lake())
    datalake.scan_s3("ARF", 5, 4000, manifests=MANIFESTS_CFG)
    manifest_gets = sum("manifests/" in k for k in fake.downloaded)
    datalake.scan_s3("BBB", 5, 4000, manifests=MANIFESTS_CFG)
    datalake.scan_s3("CCC", 5, 4000, manifests=MANIFESTS_CFG)
    assert sum("manifests/" in k for k in fake.downloaded) == manifest_gets


def test_unreadable_document_is_skipped(monkeypatch):
    objects = _lake()
    doc_key = next(k for k in objects if "documents" in k)
    del objects[doc_key]  # manifest points at a document that's gone
    install_s3(monkeypatch, objects)
    assert datalake.scan_s3("ARF", 5, 4000, manifests=MANIFESTS_CFG) == []


def test_no_manifests_falls_back_to_the_walk(monkeypatch, caplog):
    fake = install_s3(monkeypatch, OBJECTS)  # a plain notes bucket, no manifests
    with caplog.at_level("INFO"):
        hits = datalake.scan_s3("XYZ", 5, 4000, manifests=MANIFESTS_CFG)
    assert len(hits) == 2  # filename matching still works
    assert fake.list_calls > 0
    assert any("walking the bucket listing instead" in r.message for r in caplog.records)


def test_empty_markets_goes_straight_to_the_walk(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    hits = datalake.scan_s3("XYZ", 5, 4000, manifests={"prefix": "x/", "markets": []})
    assert len(hits) == 2
    assert not any("manifests/" in k for k in fake.downloaded)


def test_gather_context_wires_manifests_through(monkeypatch, tmp_path):
    fake = install_s3(monkeypatch, _lake())
    cfg = {
        "datalake": {
            "enabled": True,
            "local_dir": "",
            "max_files_per_ticker": 5,
            "max_chars_per_file": 500,
            "max_age_days": 7,
            "manifests": {"prefix": "market-data/", "markets": ["asx"]},
        }
    }
    hits = datalake.gather_context("ARF", cfg)
    assert len(hits) == 1 and fake.list_calls == 0


def test_gather_context_passes_the_age_window(monkeypatch, tmp_path):
    fake = install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    cfg = {
        "datalake": {
            "enabled": True,
            "local_dir": "",
            "max_files_per_ticker": 5,
            "max_chars_per_file": 500,
            "max_age_days": 7,
        }
    }
    assert len(datalake.gather_context("XYZ", cfg)) == 1
    assert fake.downloaded == ["notes/XYZ-today.md"]


def test_gather_context_merges_s3_and_local(tmp_path, monkeypatch):
    install_s3(monkeypatch, OBJECTS)
    _write(tmp_path, "local-note.md", "XYZ local memo")
    cfg = {
        "datalake": {
            "enabled": True,
            "local_dir": str(tmp_path),
            "max_files_per_ticker": 5,
            "max_chars_per_file": 500,
        }
    }
    sources = [h["source"] for h in datalake.gather_context("XYZ", cfg)]
    assert any(s.startswith("s3://") for s in sources)
    assert any(s.startswith("local:") for s in sources)
