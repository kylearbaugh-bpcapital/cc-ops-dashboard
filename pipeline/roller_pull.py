#!/usr/bin/env python3
"""
Roller Data API daily pull for Construction Corner dashboard.

Reads creds from ../config/roller.env, authenticates against Roller's Data API,
pulls Bookings / Customers / Revenue / Venue Information modified in the last
N days (default 7 for first run, 2 for subsequent runs), and writes JSON
to ../data/cache/.

Run manually:
    python3 scripts/roller_pull.py

Schedule daily via launchd (see scripts/com.constructioncorner.rollerpull.plist).

Roller Data API quirks (per docs.roller.app):
  - Auth: POST /token  with JSON body {client_id, client_secret}
  - Returns Bearer token, 86400s (24h) lifetime — cache and reuse
  - Endpoints return paginated results filtered by modified date
  - Rate limited; recommend caching the token (don't re-auth every call)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "config" / "roller.env"
# CACHE_DIR is overridable so CI can point it at /tmp; locally it falls back
# to data/cache for backwards compatibility with the old launchd setup.
CACHE_DIR = Path(os.environ.get("ROLLER_CACHE_DIR") or str(ROOT / "data" / "cache"))
# Token cache lives inside CACHE_DIR so actions/cache captures it automatically.
TOKEN_CACHE = CACHE_DIR / ".roller_token.json"  # gitignored

# ---------------------------------------------------------------------------
# Roller Data API endpoints — discovered live against api.roller.app.
# Pattern: /data/{resource}  with required startDate + endDate (yyyy-mm-dd).
# Pagination: pageNumber (default 1) + pageSize (default 100, max ~500).
# Bookings doc says endDate should be +1 day from startDate; we loop daily.
# ---------------------------------------------------------------------------
# Date-windowed resources — loop one day at a time during the lookback range
DATE_RESOURCES = [
    # (cache_key, path, description, days_back, merge_key)
    # merge_key: if set, daily pulls MERGE new records into existing cache by that
    # key (so we accumulate history). If None, each pull replaces the cache.
    # Daily windows can be small because merge fills the long-tail gradually.
    # Run scripts/_bk_365day.py once to seed the cache with a year of history.
    ("bookingpayments",       "/data/bookingpayments",       "Payments — actual cash collected",                   14, "bookingPaymentId"),
    ("bookingitems",          "/data/bookingitems",          "Bookings + line items (parties, GA, memberships)",   30, "bookingItemId"),
    ("tickets",               "/data/tickets",               "Issued tickets (memberships, sessions)",             30, "ticketId"),
    ("giftcards",             "/data/giftcards",             "Gift card sales + redemptions",                       30, "giftCardId"),
    ("customers",             "/data/customers",             "Customer records",                                    30, "customerId"),
    ("discounts",             "/data/discounts",             "Discount applications",                               14, "discountId"),
    ("gx_scores",             "/reporting/gxs",              "Customer satisfaction (NPS-style) survey responses",  90, "gxsResponseId"),
]

# Single-date-param resources — Roller requires a `date` (yyyy-mm-dd) param,
# returning events on that one day. We loop daily across the lookback window.
SINGLE_DATE_RESOURCES = [
    # (cache_key, path, description, days_back, merge_key)
    ("membership_redemptions", "/data/membershipredemptions", "Member check-ins (visits) by day", 60, "ticketId"),
]

# Reference / low-change resources — pull once, no date window required
REFERENCE_RESOURCES = [
    ("products",              "/data/products",              "Product catalog (incl. memberships, GA, parties)"),
    ("modifiers",             "/data/modifiers",             "Product modifiers"),
    ("reporting_categories",  "/data/reportingcategories",   "Reporting categories"),
    ("roles",                 "/data/roles",                 "Staff roles"),
    ("devices",               "/data/devices",               "POS devices"),
]

# Resources known to exist (HTTP 400 with date params) but require unknown
# parameters. Skipped for now — revisit once docs are read.
PENDING_RESOURCES = [
    ("/data/membershipredemptions", "needs different params than startDate+endDate"),
    ("/data/membershipstatuses",    "needs different params than startDate+endDate"),
    ("/data/membershipcredits",     "needs different params than startDate+endDate"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_env() -> dict[str, str]:
    """Read Roller credentials. Prefer process environment (CI use case),
    fall back to config/roller.env (local dev). Returns dict containing the
    three required keys: ROLLER_CLIENT_ID, ROLLER_CLIENT_SECRET, ROLLER_API_BASE."""
    required = ("ROLLER_CLIENT_ID", "ROLLER_CLIENT_SECRET", "ROLLER_API_BASE")
    env: dict[str, str] = {}
    # 1) Process env wins (CI)
    for k in required:
        if os.environ.get(k):
            env[k] = os.environ[k]
    if all(k in env for k in required):
        return env
    # 2) Fall back to .env file (local dev)
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    missing = [k for k in required if k not in env]
    if missing:
        sys.exit(
            f"Missing Roller credentials: {missing}. "
            f"Set them as environment variables or in {ENV_FILE}."
        )
    return env


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict | str]:
    """Minimal HTTP client using urllib (no extra deps)."""
    data = None
    final_headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=final_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body_text)
        except json.JSONDecodeError:
            return e.code, body_text


def get_token(env: dict[str, str], force_refresh: bool = False) -> str:
    """Use cached token if it has > 60 seconds of life, otherwise refresh."""
    if not force_refresh and TOKEN_CACHE.exists():
        try:
            cache = json.loads(TOKEN_CACHE.read_text())
            if cache.get("expires_at", 0) - time.time() > 60:
                return cache["access_token"]
        except (json.JSONDecodeError, OSError):
            pass

    base = env["ROLLER_API_BASE"].rstrip("/")
    status, body = http(
        "POST",
        f"{base}/token",
        body={
            "client_id": env["ROLLER_CLIENT_ID"],
            "client_secret": env["ROLLER_CLIENT_SECRET"],
        },
    )
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        sys.exit(f"Auth failed ({status}): {body}")

    expires_in = int(body.get("expires_in", 86400))
    cache = {
        "access_token": body["access_token"],
        "token_type": body.get("token_type", "Bearer"),
        "expires_at": time.time() + expires_in - 30,  # safety margin
        "obtained_at": datetime.now(timezone.utc).isoformat(),
    }
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(cache))
    print(f"  ✓ token refreshed (expires in {expires_in}s)")
    return cache["access_token"]


def _extract_records(body) -> list:
    """Roller wraps responses inconsistently — bare list, {data:[...]}, {items:[...]},
    paginated {items:[...], totalPages, currentPage}, or a single-element list with
    a wrapper object like [{products:[...]}]."""
    if isinstance(body, list):
        if len(body) == 1 and isinstance(body[0], dict):
            for v in body[0].values():
                if isinstance(v, list):
                    return v
        return body
    if isinstance(body, dict):
        for key in ("items", "data", "results", "records"):
            if isinstance(body.get(key), list):
                return body[key]
        for v in body.values():
            if isinstance(v, list):
                return v
    return []


def pull_paginated(env, token, full_url_no_page, params_str) -> tuple[list, int, list, str]:
    """Walk pageNumber=1..N until a short page is returned. Handles 429s with
    exponential backoff. Returns (records, pages_pulled, errors, token)."""
    base = env["ROLLER_API_BASE"].rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    page = 1
    page_size = 100
    out = []
    pages = 0
    errors = []
    while True:
        sep = "&" if params_str else ""
        url = f"{full_url_no_page}?{params_str}{sep}pageNumber={page}&pageSize={page_size}"

        # Try with up to 3 retries for 429 (rate limit)
        attempt = 0
        while attempt < 3:
            status, body = http("GET", url, headers=headers)
            if status == 401:
                token = get_token(env, force_refresh=True)
                headers = {"Authorization": f"Bearer {token}"}
                attempt += 1
                continue
            if status == 429:
                # Exponential backoff: 5s, 15s, 30s
                wait = [5, 15, 30][min(attempt, 2)]
                time.sleep(wait)
                attempt += 1
                continue
            break  # success or non-retriable error

        if status == 401:
            errors.append(f"persistent 401 after retry at {url}")
            break
        if status == 429:
            errors.append(f"persistent 429 after backoff at page {page}")
            break
        if status == 404:
            errors.append(f"404 at {url}")
            break
        if status >= 400:
            errors.append(f"page {page} HTTP {status}: {str(body)[:160]}")
            break
        chunk = _extract_records(body)
        out.extend(chunk)
        pages += 1
        if len(chunk) < page_size:
            break
        page += 1
        if page > 100:
            errors.append("hit 100-page cap")
            break
        time.sleep(0.4)  # gentle on rate limits between pages
    return out, pages, errors, token


def pull_date_resource(env, token, path, start_date, end_date) -> dict:
    """For endpoints requiring startDate + endDate (yyyy-mm-dd). Loops one day
    at a time across the requested range since the Bookings doc explicitly
    asks for endDate = startDate+1day."""
    base = env["ROLLER_API_BASE"].rstrip("/")
    full_url = f"{base}{path}"
    all_records = []
    total_pages = 0
    errors = []
    cur = start_date
    one_day = timedelta(days=1)
    while cur < end_date:
        nxt = cur + one_day
        params = f"startDate={cur.isoformat()}&endDate={nxt.isoformat()}"
        recs, pages, errs, token = pull_paginated(env, token, full_url, params)
        all_records.extend(recs)
        total_pages += pages
        errors.extend(errs)
        cur = nxt
    return {
        "records": all_records,
        "record_count": len(all_records),
        "pages_pulled": total_pages,
        "status": 200 if not errors else 0,
        "errors": errors,
    }


def pull_reference_resource(env, token, path) -> dict:
    """For endpoints that don't need a date window."""
    base = env["ROLLER_API_BASE"].rstrip("/")
    full_url = f"{base}{path}"
    recs, pages, errs, _ = pull_paginated(env, token, full_url, "")
    return {
        "records": recs,
        "record_count": len(recs),
        "pages_pulled": pages,
        "status": 200 if not errs else 0,
        "errors": errs,
    }


def merge_records(existing: list, new: list, key_field: str) -> tuple[list, int, int]:
    """Merge new records into existing by key_field. Returns (merged_list,
    new_count, updated_count). New records win for duplicates so we get the
    latest state. Records without a key field get appended (no dedup possible)."""
    by_key = {}
    keyless = []
    for r in existing:
        if not isinstance(r, dict): continue
        k = r.get(key_field)
        if k: by_key[k] = r
        else: keyless.append(r)
    new_count = 0
    updated_count = 0
    for r in new:
        if not isinstance(r, dict): continue
        k = r.get(key_field)
        if not k:
            keyless.append(r)
            continue
        if k in by_key: updated_count += 1
        else: new_count += 1
        by_key[k] = r
    return list(by_key.values()) + keyless, new_count, updated_count


def pull_single_date_resource(env, token, path, start_date, end_date) -> dict:
    """For endpoints that take a single `date=yyyy-mm-dd` param (membership
    redemptions, statuses, credits). Loops day-by-day. Each day's response
    is tagged with that date in case the records don't carry it explicitly."""
    base = env["ROLLER_API_BASE"].rstrip("/")
    full_url = f"{base}{path}"
    all_records = []
    total_pages = 0
    errors = []
    cur = start_date
    one_day = timedelta(days=1)
    while cur < end_date:
        recs, pages, errs, token = pull_paginated(env, token, full_url, f"date={cur.isoformat()}")
        # Tag each record with the query date for easier downstream grouping
        for r in recs:
            if isinstance(r, dict):
                r.setdefault("_queryDate", cur.isoformat())
        all_records.extend(recs)
        total_pages += pages
        errors.extend(errs)
        cur = cur + one_day
    return {
        "records": all_records,
        "record_count": len(all_records),
        "pages_pulled": total_pages,
        "status": 200 if not errors else 0,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    env = load_env()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = CACHE_DIR / "_meta.json"
    today = datetime.now(timezone.utc).date()

    # Resource windows are now per-resource (see DATE_RESOURCES). The legacy
    # --backfill flags expand all of them uniformly.
    backfill_override = None
    if "--backfill=180" in sys.argv: backfill_override = 180
    elif "--backfill=90" in sys.argv: backfill_override = 90
    elif "--backfill=30" in sys.argv: backfill_override = 30

    print(f"Roller pull — today={today}; per-resource windows below")
    if backfill_override:
        print(f"  (backfill override: {backfill_override} days for all resources)")
    token = get_token(env)
    print()

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "today": str(today),
        "resources": {},
    }
    overall_ok = True

    end_date = today + timedelta(days=1)  # include today

    def _persist(cache_key, path, kind, days_back, result, merge_key):
        """Either merge new records into existing cache or replace it."""
        nonlocal overall_ok
        out_path = CACHE_DIR / f"{cache_key}.json"
        if merge_key and out_path.exists():
            try:
                existing = json.loads(out_path.read_text())
                if not isinstance(existing, list): existing = []
            except json.JSONDecodeError:
                existing = []
            merged, new_count, updated_count = merge_records(existing, result["records"], merge_key)
            out_path.write_text(json.dumps(merged, indent=2, default=str))
            stored = len(merged)
            extra = f"  (merged: +{new_count} new, ↻{updated_count} updated · cache total {stored})"
        else:
            out_path.write_text(json.dumps(result["records"], indent=2, default=str))
            stored = result["record_count"]
            extra = ""
        summary["resources"][cache_key] = {
            "path": path, "type": kind, "days_back": days_back,
            "merge_key": merge_key,
            "fetched_count": result["record_count"],
            "stored_count": stored,
            "pages_pulled": result["pages_pulled"],
            "errors": result["errors"][:3],
            "file": str(out_path.relative_to(ROOT)),
        }
        msg = f"  ✓ fetched {result['record_count']} records, {result['pages_pulled']} pages{extra}"
        if result["errors"]:
            msg += f"  ⚠ first errors: {result['errors'][:2]}"
            overall_ok = False
        print(msg)

    for cache_key, path, description, default_days, merge_key in DATE_RESOURCES:
        days_back = backfill_override if backfill_override else default_days
        start_date = today - timedelta(days=days_back)
        mode = f"merge by {merge_key}" if merge_key else "replace"
        print(f"→ {cache_key} ({path}): {days_back}d window {start_date} → {end_date} [{mode}]")
        result = pull_date_resource(env, token, path, start_date, end_date)
        _persist(cache_key, path, "date_window", days_back, result, merge_key)

    print()
    for cache_key, path, description, default_days, merge_key in SINGLE_DATE_RESOURCES:
        days_back = backfill_override if backfill_override else default_days
        start_date = today - timedelta(days=days_back)
        mode = f"merge by {merge_key}" if merge_key else "replace"
        print(f"→ {cache_key} ({path}): {days_back}d single-date loop {start_date} → {end_date} [{mode}]")
        result = pull_single_date_resource(env, token, path, start_date, end_date)
        _persist(cache_key, path, "single_date_loop", days_back, result, merge_key)

    print()
    for cache_key, path, description in REFERENCE_RESOURCES:
        print(f"→ {cache_key} ({path}): {description}")
        result = pull_reference_resource(env, token, path)
        out_path = CACHE_DIR / f"{cache_key}.json"
        out_path.write_text(json.dumps(result["records"], indent=2, default=str))
        summary["resources"][cache_key] = {
            "path": path, "type": "reference",
            "record_count": result["record_count"],
            "pages_pulled": result["pages_pulled"],
            "errors": result["errors"][:3],
            "file": str(out_path.relative_to(ROOT)),
        }
        msg = f"  ✓ {result['record_count']} records, {result['pages_pulled']} pages"
        if result["errors"]:
            msg += f"  ⚠ first errors: {result['errors'][:2]}"
            overall_ok = False
        print(msg)

    summary["pending_resources"] = [{"path": p, "reason": r} for p, r in PENDING_RESOURCES]
    meta_path.write_text(json.dumps(summary, indent=2))
    print()
    print(f"Wrote summary to {meta_path.relative_to(ROOT)}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
