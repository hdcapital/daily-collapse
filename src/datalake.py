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
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log"}


def _snippet_around(text: str, ticker: str, max_chars: int) -> str:
    m = re.search(rf"\b{re.escape(ticker)}\b", text)
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
        if ticker in p.name.upper() or re.search(rf"\b{ticker}\b", text):
            hits.append({"source": f"local:{p}", "text": _snippet_around(text, ticker, max_chars)})
    return hits


def scan_s3(ticker: str, max_files: int, max_chars: int) -> list[dict]:
    bucket = os.environ.get("DATALAKE_S3_BUCKET")
    if not bucket:
        return []
    prefix = os.environ.get("DATALAKE_S3_PREFIX", "")
    hits: list[dict] = []
    try:
        import boto3

        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if len(hits) >= max_files:
                    return hits
                key = obj["Key"]
                if Path(key).suffix.lower() not in TEXT_EXT or obj["Size"] > 2_000_000:
                    continue
                if ticker not in key.upper():
                    continue  # cheap pass: filename match only, to avoid downloading the lake
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read(200_000)
                text = body.decode(errors="ignore")
                hits.append({"source": f"s3://{bucket}/{key}", "text": _snippet_around(text, ticker, max_chars)})
    except Exception as e:  # noqa: BLE001
        log.warning("S3 data-lake scan failed: %s", e)
    return hits


def gather_context(ticker: str, cfg: dict) -> list[dict]:
    dl = cfg.get("datalake", {})
    if not dl.get("enabled", False):
        return []
    max_files = int(dl.get("max_files_per_ticker", 5))
    max_chars = int(dl.get("max_chars_per_file", 4000))
    hits = scan_s3(ticker, max_files, max_chars)
    hits += scan_local(ticker, dl.get("local_dir", ""), max_files - len(hits), max_chars)
    return hits
