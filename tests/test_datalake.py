"""Data-lake context scan tests."""
from src import datalake


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
