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
        self.objects = objects
        self.fail_listing = fail_listing
        self.ages = ages or {}
        self.list_calls = 0
        self.downloaded: list[str] = []

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
                outer.list_calls += 1
                if outer.fail_listing:
                    raise RuntimeError("AccessDenied: s3:ListBucket")
                prefix = kwargs.get("Prefix") or ""
                contents = []
                for k, v in outer.objects.items():
                    if not k.startswith(prefix):
                        continue
                    entry = {"Key": k, "Size": len(v)}
                    modified = outer._modified(k)
                    if modified is not None:
                        entry["LastModified"] = modified
                    contents.append(entry)
                # Two pages, to exercise pagination.
                yield {"Contents": contents[:1]}
                yield {"Contents": contents[1:]}

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


def test_bucket_is_listed_once_across_many_tickers(monkeypatch):
    """The whole point of the fix: one listing per run, not one per stock."""
    fake = install_s3(monkeypatch, OBJECTS)
    for ticker in ("XYZ", "ABC", "DEF", "GHI", "JKL"):
        datalake.scan_s3(ticker, 5, 4000)
    assert fake.list_calls == 1


def test_only_matching_objects_are_downloaded(monkeypatch):
    fake = install_s3(monkeypatch, OBJECTS)
    hits = datalake.scan_s3("XYZ", 5, 4000)
    assert sorted(fake.downloaded) == ["notes/XYZ-drilling.txt", "notes/XYZ-placement.md"]
    assert len(hits) == 2
    assert hits[0]["source"].startswith("s3://my-lake/")
    assert "25% discount" in hits[0]["text"]


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
    fake = install_s3(monkeypatch, OBJECTS, fail_listing=True)
    with caplog.at_level("WARNING"):
        for ticker in ("XYZ", "ABC", "DEF"):
            assert datalake.scan_s3(ticker, 5, 4000) == []
    assert fake.list_calls == 1
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
    assert len(datalake.scan_s3("XYZ", 5, 4000, max_age_days=0)) == 2
    assert fake.list_calls == 2


def test_age_filtered_listing_still_cached_across_tickers(monkeypatch):
    fake = install_s3(monkeypatch, AGED, ages={"notes/XYZ-lastmonth.md": 30})
    for ticker in ("XYZ", "ABC", "DEF"):
        datalake.scan_s3(ticker, 5, 4000, max_age_days=7)
    assert fake.list_calls == 1


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
