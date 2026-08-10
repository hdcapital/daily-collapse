"""Scan your data lake for context on a ticker.

Two pluggable sources, both optional:
  * S3    — set DATALAKE_S3_BUCKET (and optionally DATALAKE_S3_PREFIX) plus AWS creds.
  * Local — a folder in the repo (config: datalake.local_dir), handy for testing
            and for dropping in broker notes / watchlist memos.

Matching is deliberately simple: a file is relevant if the ticker code appears
in its key/filename or its text. Snippets are truncated and handed to the AI.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}


def _in_name(ticker: str, name: str) -> str | None:
    """Filename match on a token boundary.

    A bare substring test makes short codes match constantly — "AI" is inside
    "email-notes.md", "CBA" inside "acbax.txt".
    """
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", name, re.I)


def _snippet_around(text: str, ticker: str, max_chars: int) -> str:
    m = re.search(rf"\b{re.escape(ticker)}\b", text, re.I)
    if not m:
        return text[:max_chars]
    start = max(0, m.start() - max_chars // 2)
    return text[start : start + max_chars]


def scan_local(ticker: str, root: str, max_files: int, max_chars: int) -> list[dict]:
    hits: list[dict] = []
    base = Path(root)
    if not root or not base.exists():
        return hits
    for p in sorted(base.rglob("*")):
        if len(hits) >= max_files:
            break
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if _in_name(ticker, p.name) or re.search(rf"\b{re.escape(ticker)}\b", text, re.I):
            hits.append({"source": f"local:{p}", "text": _snippet_around(text, ticker, max_chars)})
    return hits


MAX_KEYS_INDEXED = 100_000  # guard against an unbounded bucket listing

# The bucket is listed once per process and reused for every ticker. Listing it
# per ticker meant a full pass over the lake for each flagged stock — with 30
# stocks that was 30 identical passes, and it dominated the run time.
_listing_cache: dict[tuple[str, str, int], list[tuple[str, int]]] = {}
_client_cache: list = []


def reset_s3_cache() -> None:
    """Drop the cached listing and client (used by tests)."""
    _listing_cache.clear()
    _client_cache.clear()


def _s3_client():
    if not _client_cache:
        import boto3

        _client_cache.append(boto3.client("s3"))
    return _client_cache[0]


def _bucket_listing(s3, bucket: str, prefix: str, max_age_days: int = 0) -> list[tuple[str, int]]:
    """(key, size) for every object under prefix — fetched once, then cached.

    With `max_age_days` set, objects last modified before the cutoff are
    discarded as the listing streams past. S3 has no server-side date filter
    (ListObjectsV2 narrows by key prefix only), so every object is still walked;
    what this saves is the memory and the per-ticker matching, not the API time.
    Narrow `DATALAKE_S3_PREFIX` if the walk itself needs to get shorter.

    A failure is cached too, so a permissions problem is reported once rather
    than once per ticker.
    """
    cache_key = (bucket, prefix, max_age_days)
    if cache_key in _listing_cache:
        return _listing_cache[cache_key]

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days > 0 else None
    keys: list[tuple[str, int]] = []
    seen = 0
    capped = False
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                seen += 1
                if cutoff is not None:
                    modified = obj.get("LastModified")
                    # Keep anything undated rather than silently dropping it.
                    if modified is not None and modified < cutoff:
                        continue
                keys.append((obj["Key"], obj["Size"]))
            if len(keys) >= MAX_KEYS_INDEXED:
                capped = True
                break
        if capped:
            log.warning(
                "Data lake listing capped at %d objects — set DATALAKE_S3_PREFIX to narrow the scan",
                MAX_KEYS_INDEXED,
            )
        if cutoff is not None:
            log.info(
                "Data lake: kept %d of %d object(s) modified in the last %d day(s) from s3://%s/%s",
                len(keys),
                seen,
                max_age_days,
                bucket,
                prefix,
            )
        else:
            log.info("Data lake: indexed %d object(s) from s3://%s/%s", len(keys), bucket, prefix)
    except Exception as e:  # noqa: BLE001
        log.warning("S3 data-lake listing failed — S3 context skipped for this run: %s", e)
        keys = []

    _listing_cache[cache_key] = keys
    return keys


def scan_s3(ticker: str, max_files: int, max_chars: int, max_age_days: int = 0) -> list[dict]:
    bucket = os.environ.get("DATALAKE_S3_BUCKET")
    if not bucket or max_files <= 0:
        return []
    prefix = os.environ.get("DATALAKE_S3_PREFIX", "")
    hits: list[dict] = []
    try:
        s3 = _s3_client()
    except Exception as e:  # noqa: BLE001
        log.warning("S3 data-lake client unavailable: %s", e)
        return []

    for key, size in _bucket_listing(s3, bucket, prefix, max_age_days):
        if len(hits) >= max_files:
            break
        if Path(key).suffix.lower() not in TEXT_EXT or size > 2_000_000:
            continue
        if not _in_name(ticker, key):
            continue  # cheap pass: key match only, to avoid downloading the lake
        try:
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read(200_000)
        except Exception as e:  # noqa: BLE001
            log.warning("S3 data-lake read failed for %s: %s", key, e)
            continue
        text = body.decode(errors="ignore")
        hits.append({"source": f"s3://{bucket}/{key}", "text": _snippet_around(text, ticker, max_chars)})
    return hits


def gather_context(ticker: str, cfg: dict) -> list[dict]:
    dl = cfg.get("datalake", {})
    if not dl.get("enabled", False):
        return []
    max_files = int(dl.get("max_files_per_ticker", 5))
    max_chars = int(dl.get("max_chars_per_file", 4000))
    # Age applies to S3 only. The local folder is checked out fresh on every CI
    # run, so its file times say when the checkout happened, not when the note
    # was written — filtering on them would be meaningless.
    max_age_days = int(dl.get("max_age_days", 0) or 0)
    hits = scan_s3(ticker, max_files, max_chars, max_age_days)
    hits += scan_local(ticker, dl.get("local_dir", ""), max(0, max_files - len(hits)), max_chars)
    return hits
