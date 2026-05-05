#!/usr/bin/env python3
"""Aggregate Roller cache into data/cache/summary.json — the compact data
file the dashboard reads. Re-run after every Roller pull (the launchd job
will eventually call this automatically).

CRITICAL: Roller's /data/bookingitems returns one row per LINE ITEM, but
bookingTotal is the TOTAL for the entire parent booking (not the line). So
summing bookingTotal across line items double-counts. All revenue
calculations dedupe by bookingReference and use bookingTotal once."""
from __future__ import annotations
import json
import os
import pathlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
# CACHE dir is overridable via ROLLER_CACHE_DIR (CI sets this to /tmp/roller_cache).
CACHE = pathlib.Path(os.environ.get("ROLLER_CACHE_DIR") or str(ROOT / "data" / "cache"))
# Final summary.json output. Default writes to data/summary.json (the location
# the dashboard fetches over raw.githubusercontent.com).
SUMMARY_OUT = pathlib.Path(os.environ.get("SUMMARY_OUT") or str(ROOT / "data" / "summary.json"))

def load(name):
    p = CACHE / f"{name}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []

def parse_date(s):
    if not s: return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None

booking_items   = load("bookingitems")
booking_payments= load("bookingpayments")
tickets         = load("tickets")
customers       = load("customers")
products        = load("products")
giftcards       = load("giftcards")
discounts       = load("discounts")
reporting_cats  = load("reporting_categories")
gx_scores       = load("gx_scores")
member_redemptions = load("membership_redemptions")

today = date.today()
month_start = today.replace(day=1)
yesterday = today - timedelta(days=1)
# "As-of" = yesterday. Same-day data is unsettled (walk-ins not yet rung up,
# refunds/changes still in flight) so the daily-ops view defaults to yesterday's
# firm numbers. Today is shown as a small preview only.
as_of_date = yesterday
prior_day = yesterday - timedelta(days=1)  # day before yesterday, for comparison

# Product → category map
product_lookup = {}
for p in products:
    pid = str(p.get("productId"))
    product_lookup[pid] = {
        "name": p.get("name"),
        "type": p.get("productType"),
        "category": p.get("reportingCategoryName") or "Uncategorized",
        "status": p.get("productStatus"),
    }

def is_active(b):
    return b.get("bookingStatus") not in (None, "Cancelled")

def category_of(b):
    p = product_lookup.get(str(b.get("productId")))
    return p["category"] if p else "Uncategorized"

# ============================================================
# Booking-level dedup helpers
# ============================================================
def dedupe_to_bookings(line_items):
    """Convert list of line items → dict keyed by bookingReference,
    keeping the booking-level fields (status, total, date, etc.) once.
    Each booking gets a 'categories' list with the categories of its items."""
    by_ref = {}
    for li in line_items:
        ref = str(li.get("bookingReference") or "")
        if not ref:
            continue
        if ref not in by_ref:
            by_ref[ref] = {
                "ref": ref,
                "name": li.get("bookingName"),
                "total": float(li.get("bookingTotal") or 0),
                "fee": float(li.get("bookingFeeAmount") or 0),
                "discount": float(li.get("discountAmount") or 0),
                "date": li.get("bookingDate"),
                "endDate": li.get("bookingEndDate"),
                "status": li.get("bookingStatus"),
                "location": li.get("bookingLocation"),
                "createdDate": li.get("bookingCreatedDate"),
                "modifiedDate": li.get("bookingModifiedDate"),
                "groupSize": li.get("groupSize"),
                "createdByStaff": li.get("bookingCreatedByStaffId"),
                "categories": [],
                "categoriesSet": set(),
                "lines": 0,
                "meta": li.get("meta") or [],
            }
        rec = by_ref[ref]
        cat = category_of(li)
        rec["categories"].append(cat)
        rec["categoriesSet"].add(cat)
        rec["lines"] += 1
    # Convert sets to lists for JSON
    for r in by_ref.values():
        r["categoriesUnique"] = sorted(r.pop("categoriesSet"))
    return by_ref


def revenue_in(bookings_iter, predicate=None):
    """Sum bookingTotal across unique bookings (passed in already-deduped)."""
    return round(sum(b["total"] for b in bookings_iter if (predicate is None or predicate(b))), 2)


def by_category_revenue(bookings_iter):
    """For category breakdown, allocate each booking's total across its
    categories proportionally to line count (best we can do without
    per-line price data)."""
    out = defaultdict(float)
    for b in bookings_iter:
        if b["lines"] == 0:
            continue
        # Count occurrences of each category in this booking
        cat_counts = defaultdict(int)
        for c in b["categories"]:
            cat_counts[c] += 1
        for cat, n in cat_counts.items():
            out[cat] += b["total"] * (n / b["lines"])
    return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda x: -x[1])}


# ============================================================
# Active-only working set
# ============================================================
active_lines = [b for b in booking_items if is_active(b)]
booking_index = dedupe_to_bookings(active_lines)
all_bookings = list(booking_index.values())


# ============================================================
# Revenue from PAYMENTS (the source of truth for cash collected)
# bookingpayments has total + paymentMethod + createdDate (= payment date)
# ============================================================
def parse_dt_to_date(s):
    """Parse ISO datetime string → date (UTC date that the timestamp falls on
    in user's local TZ — close enough for daily bucket assignment)."""
    if not s: return None
    try:
        # Strip timezone, parse as date
        return date.fromisoformat(s[:10])
    except ValueError:
        return None

# Index payments by date; track sale (positive), refund (negative), method
payments_by_date = defaultdict(lambda: {"gross_sales": 0.0, "refunds": 0.0, "net": 0.0, "txn_count": 0, "by_method": defaultdict(float)})
for p in booking_payments:
    pdate = parse_dt_to_date(p.get("createdDate"))
    if not pdate:
        continue
    amt = float(p.get("total") or 0)
    rec = payments_by_date[pdate.isoformat()]
    rec["txn_count"] += 1
    rec["net"] += amt
    if amt >= 0:
        rec["gross_sales"] += amt
    else:
        rec["refunds"] += amt
    rec["by_method"][p.get("paymentMethod") or "Unknown"] += amt

def payments_for(d):
    return payments_by_date.get(d.isoformat(), {"gross_sales":0.0,"refunds":0.0,"net":0.0,"txn_count":0,"by_method":defaultdict(float)})

# ============================================================
# As-of view (yesterday — firm) + Today preview (in-progress)
# ============================================================
as_of_bookings = [b for b in all_bookings if parse_date(b["date"]) == as_of_date]
prior_bookings = [b for b in all_bookings if parse_date(b["date"]) == prior_day]
todays = [b for b in all_bookings if parse_date(b["date"]) == today]

# REVENUE = cash collected (from payments), not bookingTotal
as_of_payments = payments_for(as_of_date)
prior_payments = payments_for(prior_day)
today_payments = payments_for(today)

revenue_as_of = round(as_of_payments["net"], 2)
revenue_prior = round(prior_payments["net"], 2)
revenue_today_inprogress = round(today_payments["net"], 2)

# Category breakdown — keep using bookings for now (payments don't have category)
as_of_by_cat = by_category_revenue(as_of_bookings)

def to_dict(b, extra=None):
    d = {
        "ref": b["ref"], "name": b["name"], "total": b["total"], "status": b["status"],
        "date": b["date"], "categories": b["categoriesUnique"],
        "location": b["location"], "groupSize": b["groupSize"],
        "discount": b["discount"], "lines": b["lines"],
    }
    if extra: d.update(extra)
    return d

as_of_list = [to_dict(b) for b in sorted(as_of_bookings, key=lambda x: -x["total"])]
today_preview_list = [to_dict(b) for b in sorted(todays, key=lambda x: -x["total"])]

# ============================================================
# Build the actual TRANSACTIONS list — payments collected yesterday
# (these sum to the headline revenue, unlike the bookings list which
# was conflating "service held yesterday" with "money in yesterday")
# ============================================================
booking_lookup = {b["ref"]: b for b in all_bookings}

def transactions_for(d):
    """All payments whose createdDate falls on date d, joined with booking
    info for context."""
    out = []
    for p in booking_payments:
        pdt = p.get("createdDate") or ""
        if pdt[:10] != d.isoformat():
            continue
        ref = str(p.get("bookingReference") or "")
        info = booking_lookup.get(ref, {})
        out.append({
            "paymentId": p.get("bookingPaymentId"),
            "ref": ref,
            "amount": float(p.get("total") or 0),
            "method": p.get("paymentMethod") or "Unknown",
            "method_variant": p.get("paymentMethodVariant"),
            "tip": float(p.get("tip") or 0),
            "createdAt": pdt,
            "createdTime": pdt[11:16] if len(pdt) >= 16 else "",
            "name": info.get("name") if info else "",
            "categories": info.get("categoriesUnique") if info else [],
            "booking_date": info.get("date") if info else None,
            "card_last4": p.get("creditCardLast4Digits") or "",
        })
    # Sort by time within day, most recent first
    out.sort(key=lambda x: x["createdAt"], reverse=True)
    return out

as_of_txns = transactions_for(as_of_date)
today_preview_txns = transactions_for(today)


# ============================================================
# MTD — payments-driven (revenue) + bookings-driven (category mix)
# ============================================================
mtd_bookings = [b for b in all_bookings
                if parse_date(b["date"])
                and parse_date(b["date"]) >= month_start
                and parse_date(b["date"]) <= today]
mtd_revenue = round(sum(payments_for(month_start + timedelta(days=i))["net"] for i in range((today - month_start).days + 1)), 2)
mtd_by_cat = by_category_revenue(mtd_bookings)  # category mix still from bookings (no category on payments)

# Daily revenue this month from payments
mtd_daily = []
cur = month_start
while cur <= today:
    p = payments_for(cur)
    mtd_daily.append({"date": cur.isoformat(), "revenue": round(p["net"], 2), "txns": p["txn_count"]})
    cur = cur + timedelta(days=1)


# ============================================================
# Closed months — payments-driven revenue, bookings-driven category mix
# (Roller-side; firm closed-month financials come from QB in the dashboard)
# ============================================================
closed_months = defaultdict(lambda: {"booking_count": 0, "bookings": []})
closed_payments = defaultdict(float)
for b in all_bookings:
    d = parse_date(b["date"])
    if not d: continue
    if d.year == today.year and d.month >= today.month: continue
    key = f"{d.year}-{d.month:02d}"
    closed_months[key]["booking_count"] += 1
    closed_months[key]["bookings"].append(b)

# Sum payments by closed month (using payment date)
for ymd, rec in payments_by_date.items():
    pd_date = date.fromisoformat(ymd)
    if pd_date.year == today.year and pd_date.month >= today.month: continue
    if pd_date < today.replace(day=1) - timedelta(days=400): continue  # ignore very old
    key = f"{pd_date.year}-{pd_date.month:02d}"
    closed_payments[key] += rec["net"]

closed_months_clean = {}
for k in sorted(set(closed_months.keys()) | set(closed_payments.keys())):
    v = closed_months.get(k, {"booking_count": 0, "bookings": []})
    closed_months_clean[k] = {
        "revenue_payments": round(closed_payments.get(k, 0), 2),  # cash collected
        "booking_count": v["booking_count"],
        "by_category": by_category_revenue(v["bookings"]),
    }


# ============================================================
# Better month projection (weighted blend)
# ============================================================
import calendar
days_in_month = calendar.monthrange(today.year, today.month)[1]
days_elapsed = today.day

# Naive linear extrapolation from MTD payments
mtd_pace_projection = round(mtd_revenue * (days_in_month / max(days_elapsed, 1)), 2) if days_elapsed > 0 else 0

# Historical baseline = average of most recent 3 closed months (payments-based)
recent_closed_revenues = [v["revenue_payments"] for v in list(closed_months_clean.values())[-3:] if v["revenue_payments"] > 0]
historical_avg = round(sum(recent_closed_revenues) / len(recent_closed_revenues), 2) if recent_closed_revenues else 0

# Weighted blend: more weight on MTD as the month progresses
weight = days_elapsed / days_in_month
blended_projection = round(weight * mtd_pace_projection + (1 - weight) * historical_avg, 2) if historical_avg > 0 else mtd_pace_projection


# ============================================================
# Party pipeline (PartyPackage product type)
# ============================================================
party_pid_set = {str(p.get("productId")) for p in products if p.get("productType") == "PartyPackage"}

def is_party_booking(b):
    """A booking counts as a party if ANY of its line items maps to a PartyPackage product."""
    # Get the original line items for this booking ref
    return b["ref"] in party_booking_refs

party_booking_refs = set()
for li in active_lines:
    if str(li.get("productId")) in party_pid_set:
        party_booking_refs.add(str(li.get("bookingReference") or ""))

future_bookings = [b for b in all_bookings if parse_date(b["date"]) and parse_date(b["date"]) >= today]

party_pipeline = {}
for label, days in [("next_30", 30), ("next_60", 60), ("next_90", 90)]:
    cutoff = today + timedelta(days=days)
    parties = [b for b in future_bookings if is_party_booking(b) and parse_date(b["date"]) <= cutoff]
    party_pipeline[label] = {
        "booking_count": len(parties),
        "total_revenue": round(sum(b["total"] for b in parties), 2),
    }

# Deposit status breakdown for parties next 60 days (deduped by booking)
party_deposit_count = defaultdict(int)
party_deposit_revenue = defaultdict(float)
for b in future_bookings:
    if is_party_booking(b) and parse_date(b["date"]) <= today + timedelta(days=60):
        s = b["status"] or "Unknown"
        party_deposit_count[s] += 1
        party_deposit_revenue[s] += b["total"]

# Parties without payment
parties_no_deposit = []
for b in future_bookings:
    if is_party_booking(b) and b["status"] == "PendingPayment":
        bd = parse_date(b["date"])
        days_until = (bd - today).days if bd else None
        parties_no_deposit.append({
            "ref": b["ref"], "name": b["name"], "date": b["date"],
            "days_until": days_until, "total": b["total"],
        })
parties_no_deposit.sort(key=lambda x: x["days_until"] if x["days_until"] is not None else 9999)

# Top 12 future parties by date (one per booking now, no dupes)
upcoming_party_records = sorted(
    [b for b in future_bookings if is_party_booking(b)],
    key=lambda x: (x["date"] or "", -x["total"])
)[:12]
upcoming_parties_clean = [{
    "ref": b["ref"], "name": b["name"], "date": b["date"],
    "total": b["total"], "status": b["status"],
    "group_size": b["groupSize"],
} for b in upcoming_party_records]


# ============================================================
# Memberships — count from bookingitems where product is in Memberships category
# (tickets-based count was undercounting because tickets only modified within the
# pull window; bookingitems with 365-day backfill captures all memberships)
# ============================================================
membership_product_ids = {str(p.get("productId")) for p in products if p.get("reportingCategoryName") == "Memberships"}

# Membership bookings = bookings whose line items include any membership product
membership_booking_refs = set()
membership_booking_dates = {}      # ref -> bookingDate (start)
membership_booking_end_dates = {}  # ref -> bookingEndDate (expiry)
for li in active_lines:  # all NON-cancelled line items
    if str(li.get("productId")) in membership_product_ids:
        ref = str(li.get("bookingReference") or "")
        if not ref: continue
        membership_booking_refs.add(ref)
        if ref not in membership_booking_dates:
            membership_booking_dates[ref] = parse_date(li.get("bookingDate"))
            membership_booking_end_dates[ref] = parse_date(li.get("bookingEndDate"))

# Deactivated memberships (from booking meta)
deactivated_refs = set()
for b in all_bookings:
    for m in (b["meta"] or []):
        if isinstance(m, dict) and m.get("attribute") == "MembershipStatus" and m.get("value") == "Deactivated":
            deactivated_refs.add(b["ref"])

# Active = membership booking that's not deactivated AND end-date hasn't passed
# (some memberships have very long end dates, e.g. 2027 — those are still active)
active_members = set()
expired_members = set()
for ref in membership_booking_refs:
    if ref in deactivated_refs:
        continue
    end = membership_booking_end_dates.get(ref)
    if end is None or end >= today:
        active_members.add(ref)
    else:
        expired_members.add(ref)

# New this month = membership booking whose start date falls in current month
new_member_refs = set()
for ref in active_members:
    sd = membership_booking_dates.get(ref)
    if sd and sd >= month_start and sd <= today:
        new_member_refs.add(ref)

# MRR estimate — sum bookingTotal of active memberships
# (Note: bookingTotal is the FULL membership value, not monthly. Annual memberships will skew high. Future: divide by membership term)
mrr_total = 0.0
for b in all_bookings:
    if b["ref"] in active_members:
        mrr_total += b["total"]

# Tickets-based fallback (older method) for sanity check
tix_member_refs = set()
for t in tickets:
    if t.get("productSubType") == "Membership":
        tix_member_refs.add(str(t.get("bookingReference") or ""))


# ============================================================
# Visit pattern from membership_redemptions
# ============================================================
visits_by_ref = defaultdict(list)        # bookingReference -> [redemption datetimes]
for r in member_redemptions:
    ref = str(r.get("bookingReference") or "")
    if not ref: continue
    rd = r.get("redemptionDate") or r.get("_queryDate")
    pd_date = parse_dt_to_date(rd) or parse_date(rd)
    if pd_date: visits_by_ref[ref].append(pd_date)

# Visit windows
visits_30d = defaultdict(int)
visits_60d = defaultdict(int)
for ref, dates in visits_by_ref.items():
    for d in dates:
        if d >= today - timedelta(days=30): visits_30d[ref] += 1
        if d >= today - timedelta(days=60): visits_60d[ref] += 1

# Apply to active members only
active_visits_30d = {ref: visits_30d.get(ref, 0) for ref in active_members}
active_visits_60d = {ref: visits_60d.get(ref, 0) for ref in active_members}

total_visits_30 = sum(active_visits_30d.values())
total_visits_60 = sum(active_visits_60d.values())
avg_visits_per_active = round(total_visits_30 / max(len(active_members), 1), 2)

# At-risk: active member but 0 visits in last 30 days
at_risk_refs = [ref for ref in active_members if active_visits_30d.get(ref, 0) == 0]
# 60-day at-risk (more concerning)
deeply_at_risk = [ref for ref in active_members if active_visits_60d.get(ref, 0) == 0]

# Top users — most visits in last 30 days
top_users_refs = sorted(active_visits_30d.items(), key=lambda x: -x[1])[:10]
booking_lookup_for_members = {b["ref"]: b for b in all_bookings}

def member_summary(ref, visits_30, visits_60):
    b = booking_lookup_for_members.get(ref, {})
    return {
        "ref": ref,
        "name": b.get("name") if b else "(unknown)",
        "visits_30d": visits_30,
        "visits_60d": visits_60,
        "membership_value": b.get("total") if b else None,
        "since": b.get("date") if b else None,
    }

top_users = [member_summary(ref, v, active_visits_60d.get(ref, 0)) for ref, v in top_users_refs]
at_risk_list = [member_summary(ref, 0, active_visits_60d.get(ref, 0)) for ref in at_risk_refs[:30]]

# Daily visit chart (last 30 days)
visits_per_day = defaultdict(int)
for r in member_redemptions:
    rd = r.get("redemptionDate") or r.get("_queryDate")
    pd_date = parse_dt_to_date(rd) or parse_date(rd)
    if pd_date and pd_date >= today - timedelta(days=30) and pd_date <= today:
        visits_per_day[pd_date.isoformat()] += 1
visits_daily = [{"date": d, "count": visits_per_day[d]} for d in sorted(visits_per_day.keys())]


# ============================================================
# GX (customer satisfaction) aggregations
# ============================================================
# Overall stats
gx_total = len(gx_scores)
fan_count = sum(1 for g in gx_scores if g.get("isFan"))
critic_count = sum(1 for g in gx_scores if g.get("isCritic"))
overall_ratings = [g.get("overallRating") for g in gx_scores if g.get("overallRating") is not None]
avg_overall = round(sum(overall_ratings) / len(overall_ratings), 2) if overall_ratings else None
fan_rate = round(fan_count / gx_total * 100, 1) if gx_total else 0

# Rating distribution
rating_dist = defaultdict(int)
for r in overall_ratings:
    rating_dist[r] += 1
rating_dist_clean = {str(k): rating_dist[k] for k in sorted(rating_dist.keys(), reverse=True)}

# Sub-rating averages
def avg_sub(field):
    vs = [g.get(field) for g in gx_scores if g.get(field) is not None]
    return round(sum(vs) / len(vs), 2) if vs else None

sub_ratings = {
    "service": avg_sub("serviceRating"),
    "safety": avg_sub("safetyRating"),
    "facilities": avg_sub("facilitiesRating"),
    "value": avg_sub("valueRating"),
}

# Most-cited reasons (across all reasons fields)
reason_counts = defaultdict(int)
for g in gx_scores:
    for field in ("serviceRatingReasons", "safetyRatingReasons", "facilitiesRatingReasons", "valueRatingReasons"):
        for reason in (g.get(field) or []):
            reason_counts[reason] += 1
top_reasons = sorted(reason_counts.items(), key=lambda x: -x[1])[:8]

# Detractors needing follow-up (low rating, not yet actioned)
unactioned_detractors = []
for g in gx_scores:
    if (g.get("isCritic") or (g.get("overallRating") and g.get("overallRating") <= 3)) and not g.get("actioned"):
        unactioned_detractors.append({
            "id": g.get("gxsResponseId"),
            "customerId": g.get("customerId"),
            "ref": g.get("bookingReference"),
            "rating": g.get("overallRating"),
            "createdDate": g.get("createdDate"),
            "service": g.get("serviceRating"),
            "safety": g.get("safetyRating"),
            "facilities": g.get("facilitiesRating"),
            "value": g.get("valueRating"),
        })
unactioned_detractors.sort(key=lambda x: x.get("createdDate") or "", reverse=True)

# Recent responses (last 10)
recent_gx = sorted(gx_scores, key=lambda g: g.get("createdDate") or "", reverse=True)[:10]
recent_gx_clean = [{
    "id": g.get("gxsResponseId"),
    "rating": g.get("overallRating"),
    "isFan": g.get("isFan"),
    "isCritic": g.get("isCritic"),
    "service": g.get("serviceRating"),
    "safety": g.get("safetyRating"),
    "facilities": g.get("facilitiesRating"),
    "value": g.get("valueRating"),
    "createdDate": g.get("createdDate"),
    "actioned": g.get("actioned"),
    "ref": g.get("bookingReference"),
} for g in recent_gx]

# 30-day rolling avg
gx_30d = [g for g in gx_scores if parse_dt_to_date(g.get("createdDate") or "") and parse_dt_to_date(g.get("createdDate")) >= today - timedelta(days=30)]
gx_30d_overall = [g.get("overallRating") for g in gx_30d if g.get("overallRating") is not None]
avg_30d = round(sum(gx_30d_overall) / len(gx_30d_overall), 2) if gx_30d_overall else None


def _build_member_block():
    return {
        "active_count": len(active_members),
        "new_this_month": len(new_member_refs),
        "deactivated_total": len(deactivated_refs),
        "expired_total": len(expired_members),
        "total_membership_bookings_seen": len(membership_booking_refs),
        "tickets_membership_count_legacy": len(tix_member_refs),
        "mrr_estimate_lifetime_value": round(mrr_total, 2),
        # Visit pattern
        "visits_30d_total": total_visits_30,
        "visits_60d_total": total_visits_60,
        "avg_visits_per_member_30d": avg_visits_per_active,
        "at_risk_count_30d": len(at_risk_refs),
        "at_risk_count_60d": len(deeply_at_risk),
        "at_risk_list": at_risk_list,
        "top_users_30d": top_users,
        "visits_daily": visits_daily,
    }


# ============================================================
# Customers
# ============================================================
new_customers_this_month = sum(1 for c in customers if parse_date(c.get("createdDate")) and parse_date(c.get("createdDate")) >= month_start)
total_customers_modified = len(customers)
marketing_optin = sum(1 for c in customers if c.get("acceptMarketing"))


# ============================================================
# Channel mix (last 30 days, deduped)
# ============================================================
channel_mix = defaultdict(float)
for b in all_bookings:
    d = parse_date(b["date"])
    if d and d >= today - timedelta(days=30) and d <= today:
        channel_mix[b["location"] or "Unknown"] += b["total"]
channel_mix = {k: round(v, 2) for k, v in channel_mix.items()}


# ============================================================
# Discounts insight
# ============================================================
discount_total_30d = 0.0
disc_count_30d = 0
for b in all_bookings:
    d = parse_date(b["date"])
    if d and d >= today - timedelta(days=30) and d <= today and b["discount"] > 0:
        discount_total_30d += b["discount"]
        disc_count_30d += 1


# ============================================================
# Alerts
# ============================================================
alerts = []
for p in parties_no_deposit:
    if p["days_until"] is not None and p["days_until"] <= 14 and p["days_until"] >= 0:
        alerts.append({
            "severity": "red" if p["days_until"] <= 7 else "yellow",
            "kind": "Party without deposit",
            "msg": f"{p['name']} on {p['date']} ({p['days_until']}d out, ${p['total']:.0f})",
        })

upcoming_by_day = defaultdict(float)
for b in future_bookings:
    d = parse_date(b["date"])
    if d and d <= today + timedelta(days=14):
        upcoming_by_day[d.isoformat()] += b["total"]
for d, amt in upcoming_by_day.items():
    if amt >= 2000:
        alerts.append({"severity": "info", "kind": "Big day", "msg": f"{d}: ${amt:.0f} booked"})


# ============================================================
# Output
# ============================================================
summary = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "today": today.isoformat(),
    "month_start": month_start.isoformat(),
    "days_in_month": days_in_month,
    "days_elapsed": days_elapsed,
    "venue_name": "Construction Corner",
    "categories": [c.get("name") for c in reporting_cats],
    "today_view": {
        # Field name kept for back-compat; this is YESTERDAY's firm data.
        "date": as_of_date.isoformat(),
        "label": "Yesterday",
        "revenue": revenue_as_of,                         # net cash collected (payments)
        "revenue_yesterday": revenue_prior,               # day-before (for comparison)
        "comparison_label": "Day before",
        "booking_count": as_of_payments["txn_count"],     # transaction count from payments
        "txn_count": as_of_payments["txn_count"],
        "gross_sales": round(as_of_payments["gross_sales"], 2),
        "refunds": round(as_of_payments["refunds"], 2),
        "by_method": {k: round(v, 2) for k, v in sorted(as_of_payments["by_method"].items(), key=lambda x: -x[1])},
        "by_category": as_of_by_cat,
        # Transactions — list of payments collected yesterday (sums to revenue)
        "transactions": as_of_txns,
        # Activity — list of services held / bookings dated yesterday (for context, not revenue)
        "activity": as_of_list,
    },
    "today_preview": {
        "date": today.isoformat(),
        "revenue": revenue_today_inprogress,
        "booking_count": today_payments["txn_count"],
        "transactions": today_preview_txns,
        "activity": today_preview_list,
    },
    "mtd": {
        "month": today.strftime("%B %Y"),
        "month_short": today.strftime("%Y-%m"),
        "revenue": mtd_revenue,
        "by_category": mtd_by_cat,
        "daily": mtd_daily,
        "projected_full_month_naive_pace": mtd_pace_projection,
        "projected_full_month_blended": blended_projection,
        "historical_3mo_avg_roller": historical_avg,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
    },
    "closed_months": closed_months_clean,
    "party_pipeline": party_pipeline,
    "party_deposit_breakdown": {
        "by_status_count": dict(party_deposit_count),
        "by_status_revenue": {k: round(v, 2) for k, v in party_deposit_revenue.items()},
    },
    "parties_pending_deposit": parties_no_deposit,
    "upcoming_parties": upcoming_parties_clean,
    "memberships": _build_member_block(),
    "customers": {
        "modified_30d": total_customers_modified,
        "new_this_month": new_customers_this_month,
        "marketing_optin": marketing_optin,
    },
    "discounts": {
        "30d_total_value": round(discount_total_30d, 2),
        "30d_application_count": disc_count_30d,
        "catalog_count": len(discounts),
    },
    "channel_mix_30d": channel_mix,
    "alerts": alerts,
    "gx": {
        "total_responses": gx_total,
        "fan_count": fan_count,
        "critic_count": critic_count,
        "fan_rate_pct": fan_rate,
        "avg_overall": avg_overall,
        "avg_overall_30d": avg_30d,
        "rating_distribution": rating_dist_clean,
        "sub_ratings": sub_ratings,
        "top_reasons": [{"reason": r, "count": n} for r, n in top_reasons],
        "unactioned_detractors": unactioned_detractors,
        "recent": recent_gx_clean,
    },
}

out_path = SUMMARY_OUT
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(summary, indent=2))
size_kb = out_path.stat().st_size / 1024
try:
    rel = out_path.relative_to(ROOT)
except ValueError:
    rel = out_path
print(f"Wrote {rel} ({size_kb:.1f} KB)")
print()
print(f"  As-of view: {as_of_date} (yesterday)")
print(f"    Revenue (payments-firm): ${revenue_as_of:,.0f} from {as_of_payments['txn_count']} transactions")
print(f"      Gross: ${as_of_payments['gross_sales']:,.2f} · Refunds: ${as_of_payments['refunds']:,.2f}")
for m, v in sorted(as_of_payments['by_method'].items(), key=lambda x: -x[1]):
    print(f"      {m}: ${v:,.2f}")
print(f"    Day before: ${revenue_prior:,.0f}")
print(f"    Today so far (preview): ${revenue_today_inprogress:,.0f}")
print(f"  MTD: ${mtd_revenue:,.0f} (day {days_elapsed}/{days_in_month})")
print(f"  Naive pace projection: ${mtd_pace_projection:,.0f}")
print(f"  Historical 3-mo avg (Roller): ${historical_avg:,.0f}")
print(f"  Blended projection: ${blended_projection:,.0f}")
print()
print(f"  Active members: {len(active_members)}  ·  new this month: {len(new_member_refs)}  ·  deactivated: {len(deactivated_refs)}  ·  expired: {len(expired_members)}")
print(f"    (legacy tickets-based count: {len(tix_member_refs)} — should match if tickets pull is current)")
print(f"    Member visits: {total_visits_30} in last 30d ({avg_visits_per_active}/active member)  ·  at-risk: {len(at_risk_refs)} (no visits 30d), {len(deeply_at_risk)} (no visits 60d)")
print(f"  GX scores: {gx_total} responses · avg {avg_overall} · {fan_rate}% fans · {len(unactioned_detractors)} detractors needing follow-up")
print(f"  Parties next 30d: {party_pipeline['next_30']['booking_count']} (${party_pipeline['next_30']['total_revenue']:,.0f})")
print(f"  Pending-deposit parties: {len(parties_no_deposit)}  ·  Alerts: {len(alerts)}")
print(f"  Closed months in data: {list(closed_months_clean.keys())}")
