#!/usr/bin/env python3
"""Material-change thresholds + entity keys (ARCHITECTURE ADR-02).

THE versioned home of "material change". These constants are quoted verbatim
on the methodology page and inside every alert email. Changing any value here
is a recorded decision (bump THRESHOLDS_VERSION + note why), never a code tweak.

Definition (ADR-02, v1, threshold_kind='material' — the only v1 kind):
  alert when |d median_wait_weeks_est| >= 1.0 week
       OR    |d waiting_list|          >= max(10% of previous, 25)
  - the absolute floor (25) kills small-provider jitter
  - a component whose value is null on either side is SKIPPED (no delta is
    computable); null -> null months therefore never alert
  - an entity vanishing from the extract alerts once as "no longer reported"
    (honest-boundaries rule; the once-ness is enforced by the send job's
    send_log, not here)

No sending happens here (W4). This module only defines the contract and
computes the per-entity month-on-month delta document the send job consumes.
"""
from datetime import datetime, timezone

THRESHOLDS_VERSION = 1
MEDIAN_DELTA_WEEKS = 1.0      # weeks, estimated median (from week-band interpolation)
WAITING_LIST_PCT = 0.10       # 10% of the previous month's waiting list...
WAITING_LIST_FLOOR = 25       # ...but never less than 25 pathways


def entity_key(provider_code, specialty_code):
    """Canonical alert entity key — matches the D1 watches table (ADR-01)."""
    return f"{provider_code}|{specialty_code}"


def material_change(prev_wl, curr_wl, prev_med, curr_med):
    """Pure predicate. Args are numbers or None (None = not reported / null).

    Returns (material: bool, reasons: list[str]) where reasons is a subset of
    ["median", "waiting_list"]. A component with a null on either side is
    skipped — never treated as zero, never alerted on.
    """
    reasons = []
    if prev_med is not None and curr_med is not None:
        if abs(curr_med - prev_med) >= MEDIAN_DELTA_WEEKS:
            reasons.append("median")
    if prev_wl is not None and curr_wl is not None:
        threshold = max(WAITING_LIST_PCT * abs(prev_wl), WAITING_LIST_FLOOR)
        if abs(curr_wl - prev_wl) >= threshold:
            reasons.append("waiting_list")
    return (bool(reasons), reasons)


def prev_calendar_month(month):
    """'2026-05' -> '2026-04'."""
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _num(s):
    s = (s or "").strip()
    return None if s == "" else float(s)


def compute_deltas(rows, generated_at=None):
    """Per-entity month-on-month deltas from the accumulated normalised layer.

    rows: iterable of dicts with keys month, provider_code, specialty_code,
          waiting_list, median_wait_weeks_est (strings; '' = null).

    Compares the latest month present against its previous CALENDAR month.
    If that previous month is absent (e.g. mid-backfill), entities is empty —
    a >1-month gap is never presented as a month-on-month movement.

    Entity classes:
      both months present -> deltas + material verdict
      prev only           -> vanished ("no longer reported"; material)
      curr only           -> appeared (informational, not material)
    """
    months = sorted({r["month"] for r in rows})
    doc = {
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "thresholds_version": THRESHOLDS_VERSION,
        "thresholds": {
            "median_delta_weeks": MEDIAN_DELTA_WEEKS,
            "waiting_list_pct": WAITING_LIST_PCT,
            "waiting_list_floor": WAITING_LIST_FLOOR,
        },
        "month": months[-1] if months else None,
        "prev_month": None,
        "entities": [],
    }
    if not months:
        return doc
    curr_m = months[-1]
    prev_m = prev_calendar_month(curr_m)
    if prev_m not in months:
        return doc  # no adjacent prior month yet — honest empty, never a fake delta
    doc["prev_month"] = prev_m

    def snap(r):
        wl = _num(r["waiting_list"])
        return {"wl": int(wl) if wl is not None else None,
                "med": _num(r["median_wait_weeks_est"])}

    prev = {entity_key(r["provider_code"], r["specialty_code"]): snap(r)
            for r in rows if r["month"] == prev_m}
    curr = {entity_key(r["provider_code"], r["specialty_code"]): snap(r)
            for r in rows if r["month"] == curr_m}

    for key in sorted(set(prev) | set(curr)):
        p, c = prev.get(key), curr.get(key)
        ent = {"key": key}
        if p is not None and c is not None:
            material, reasons = material_change(p["wl"], c["wl"], p["med"], c["med"])
            ent.update({
                "prev": p, "curr": c,
                "delta_wl": (c["wl"] - p["wl"])
                if (p["wl"] is not None and c["wl"] is not None) else None,
                "delta_med": round(c["med"] - p["med"], 1)
                if (p["med"] is not None and c["med"] is not None) else None,
                "material": material, "reasons": reasons,
            })
        elif c is None:
            ent.update({"prev": p, "curr": None, "vanished": True,
                        "material": True, "reasons": ["no_longer_reported"]})
        else:
            ent.update({"prev": None, "curr": c, "appeared": True,
                        "material": False, "reasons": []})
        doc["entities"].append(ent)
    return doc
