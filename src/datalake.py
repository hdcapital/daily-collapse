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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


MAX_KEYS_INDEXED = 100_000  # cap on *kept* entries — a memory guard, not a feature
MAX_LIST_WORKERS = 12  # concurrent listings; S3 tolerates thousands/sec, this is nowhere near
MAX_FANOUT_FOLDERS = 512  # beyond this, per-folder requests would outnumber plain pages
GROWTH_WARN_OBJECTS = 250_000  # start telling the operator the walk is getting long

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


class _WalkState:
    """Shared counters for a (possibly parallel) bucket walk."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = 0  # objects walked past
        self.kept = 0  # objects retained after the age filter
        self.stop = False  # kept hit MAX_KEYS_INDEXED — wind down


def _list_prefix(s3, bucket, prefix, cutoff, state, delimiter=None):
    """List one prefix, returning (kept entries, sub-folders seen).

    Without a delimiter this walks the prefix's whole subtree; with "/" it
    returns only direct keys plus the immediate sub-folders.
    """
    kept: list[tuple[str, int]] = []
    folders: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    if delimiter:
        kwargs["Delimiter"] = delimiter
    for page in paginator.paginate(**kwargs):
        for cp in page.get("CommonPrefixes", []):
            folders.append(cp["Prefix"])
        contents = page.get("Contents", [])
        batch = []
        for obj in contents:
            if cutoff is not None:
                modified = obj.get("LastModified")
                # Keep anything undated rather than silently dropping it.
                if modified is not None and modified < cutoff:
                    continue
            batch.append((obj["Key"], obj["Size"]))
        kept.extend(batch)
        with state.lock:
            state.seen += len(contents)
            state.kept += len(batch)
            if state.kept >= MAX_KEYS_INDEXED:
                state.stop = True
            if state.stop:
                break
    return kept, folders


def _walk(s3, bucket, prefix, cutoff):
    """Walk the bucket, fanning out across top-level folders when there are some.

    One delimiter probe finds the folders under `prefix` (and lists any loose
    keys at that level as a side effect). Each folder's subtree is then listed
    in parallel — the same number of S3 requests as a flat walk, spread across
    MAX_LIST_WORKERS threads instead of one. A flat bucket has no folders to
    fan out over, so it degrades to exactly the old sequential walk; ditto a
    pathological layout with more folders than MAX_FANOUT_FOLDERS, where
    per-folder requests would cost more than plain pages.
    """
    state = _WalkState()
    keys, folders = _list_prefix(s3, bucket, prefix, cutoff, state, delimiter="/")
    folders = list(dict.fromkeys(folders))  # paranoid dedupe, order-stable

    if state.stop or not folders:
        mode = "flat, sequential"
    elif len(folders) == 1:
        more, _ = _list_prefix(s3, bucket, folders[0], cutoff, state)
        keys += more
        mode = "single folder, sequential"
    elif len(folders) <= MAX_FANOUT_FOLDERS:
        with ThreadPoolExecutor(max_workers=MAX_LIST_WORKERS) as pool:
            futures = [
                pool.submit(_list_prefix, s3, bucket, folder, cutoff, state)
                for folder in folders
            ]
            for future in futures:
                more, _ = future.result()
                keys += more
        mode = f"parallel over {len(folders)} folders"
    else:
        # Too many folders for fan-out to pay off — re-walk flat from scratch
        # (discarding the probe's partial results so nothing is double-counted).
        state = _WalkState()
        keys, _ = _list_prefix(s3, bucket, prefix, cutoff, state)
        mode = f"{len(folders)} top-level folders, sequential"

    # Sort so the result (and therefore which notes reach the AI) is identical
    # to a plain lexicographic listing, regardless of thread timing.
    keys.sort()
    if state.stop:
        del keys[MAX_KEYS_INDEXED:]
    return keys, state.seen, state.stop, mode


def _bucket_listing(s3, bucket: str, prefix: str, max_age_days: int = 0) -> list[tuple[str, int]]:
    """(key, size) for every object under prefix — fetched once, then cached.

    With `max_age_days` set, objects last modified before the cutoff are
    discarded as the listing streams past. S3 has no server-side date filter
    (ListObjectsV2 narrows by key prefix only), so every object is still walked;
    the walk is parallelised across top-level folders (see _walk), but its total
    request count still grows with the bucket. Narrow `DATALAKE_S3_PREFIX` if
    the walk itself needs to get shorter.

    A failure is cached too, so a permissions problem is reported once rather
    than once per ticker.
    """
    cache_key = (bucket, prefix, max_age_days)
    if cache_key in _listing_cache:
        return _listing_cache[cache_key]

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days) if max_age_days > 0 else None
    started = time.monotonic()
    try:
        keys, seen, capped, mode = _walk(s3, bucket, prefix, cutoff)
        elapsed = time.monotonic() - started
        if capped:
            log.warning(
                "Data lake listing capped at %d kept objects — set DATALAKE_S3_PREFIX or "
                "lower datalake.max_age_days",
                MAX_KEYS_INDEXED,
            )
        if cutoff is not None:
            log.info(
                "Data lake: kept %d of %d object(s) modified in the last %d day(s) "
                "from s3://%s/%s in %.1fs (%s)",
                len(keys), seen, max_age_days, bucket, prefix, elapsed, mode,
            )
        else:
            log.info(
                "Data lake: indexed %d object(s) from s3://%s/%s in %.1fs (%s)",
                len(keys), bucket, prefix, elapsed, mode,
            )
        if seen >= GROWTH_WARN_OBJECTS:
            log.warning(
                "The data lake has %d objects and the nightly walk grows with it — "
                "point DATALAKE_S3_PREFIX at a dated folder to keep it fast (see README, Data lake)",
                seen,
            )
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
