"""Deterministic estimate engine: fee schedule x 271 cost-share rules -> patient responsibility.

The LLM never computes or alters these numbers.
"""
from .data import BundleLine, DataStore
from .parser271 import PlanBenefits

EM_PREFIX = "99"  # office-visit E/M codes are where flat copays apply


def _money(x: float) -> float:
    return round(x + 1e-9, 2)


def _units_text(units: float, repeat: int, basis: str) -> str:
    u = int(units) if float(units).is_integer() else units
    return f"{u} x {repeat} visits" if repeat > 1 else f"{u}"


def is_em(cpt: str) -> bool:
    return cpt.startswith(EM_PREFIX)


def benefit_for(cpt: str, cpt_info, pb: PlanBenefits):
    """Pick the benefit that governs this CPT. Returns (stc, benefit, plan_level_fallback).

    Office visits (E/M) use the first STC that reports a copay. Everything else (tests, injections, drugs)
    uses coinsurance from its own primary STC, else the plan-level benefit (STC 30). Copays are NOT applied
    to procedures, and fallback STCs are not scanned for coinsurance (unrelated STCs can report 0%).
    """
    if is_em(cpt):
        for stc in cpt_info.stcs:
            b = pb.by_stc.get(stc)
            if b and (b.copay is not None or b.not_covered):
                return stc, b, False
    else:
        stc = cpt_info.stcs[0]
        b = pb.by_stc.get(stc)
        if b and (b.coinsurance is not None or b.not_covered):
            return stc, b, False
    plan = pb.by_stc.get("30")
    if plan and plan.coinsurance is not None:
        return "30", plan, True
    return None, None, False


def _scenario(lines: list[BundleLine], which: str, store: DataStore, pb: PlanBenefits, tp_id: str, npi: str):
    ded = pb.deductible_remaining or 0.0
    oop = pb.oop_remaining if pb.oop_remaining is not None else float("inf")
    work = []
    for bl in lines:
        info = store.catalog.get(bl.cpt)
        units = bl.units_min if which == "min" else bl.units_max
        fee = store.fees.get((tp_id, npi, bl.cpt))
        row = {"cpt": bl.cpt, "name": info.plain_name if info else bl.cpt, "units": units,
               "repeat_visits": bl.repeat_visits, "units_text": _units_text(units, bl.repeat_visits, ""),
               "sequence": bl.sequence, "estimable": False, "patient_cost": None,
               "allowed_total": None, "cost_type": None, "note": ""}
        if not fee:
            row["note"] = "No negotiated rate on file for this clinic and plan"
        elif not info:
            row["note"] = "Procedure not in catalog"
        else:
            stc, ben, plan_level = benefit_for(bl.cpt, info, pb)
            if ben is None:
                row["note"] = "Plan did not report a benefit for this service"
            elif ben.not_covered:
                row["note"] = "Plan reports this service as not covered"
            else:
                row["estimable"] = True
                row["stc"] = stc
                if plan_level:
                    row["note"] = (f"No separate benefit reported for this service; your plan's general "
                                   f"{round(ben.coinsurance * 100)}% coinsurance after deductible is assumed")
                row["allowed_each"] = fee.allowed
                row["allowed_total"] = _money(fee.allowed * units * bl.repeat_visits)
        work.append(row)

    # adjudicate highest allowed first so the shared deductible is consumed realistically
    for row in sorted((r for r in work if r["estimable"]), key=lambda r: -r["allowed_total"]):
        ben = pb.by_stc[row["stc"]]
        use_copay = is_em(row["cpt"]) and ben.copay is not None
        if use_copay:
            cost = ben.copay * row["repeat_visits"]
            row["cost_type"] = "copay"
        else:
            coins = ben.coinsurance if ben.coinsurance is not None else 0.0
            ded_applied = min(row["allowed_total"], ded)
            ded -= ded_applied
            cost = ded_applied + (row["allowed_total"] - ded_applied) * coins
            row["cost_type"] = "deductible" if ded_applied and ded_applied >= row["allowed_total"] else (
                "deductible + coinsurance" if ded_applied else "coinsurance")
            row["coinsurance_pct"] = coins
        cost = min(cost, oop)
        oop -= cost
        row["patient_cost"] = _money(cost)
    work.sort(key=lambda r: r["sequence"])
    return work, ded, oop


def estimate_clinic(store: DataStore, bundle_id: str, pb: PlanBenefits, tp_id: str, npi: str) -> dict:
    lines = store.bundles[bundle_id]
    lo, ded_lo, _ = _scenario(lines, "min", store, pb, tp_id, npi)
    hi, ded_hi, _ = _scenario(lines, "max", store, pb, tp_id, npi)
    out_lines = []
    for a, b in zip(lo, hi):
        out_lines.append({
            "cpt": a["cpt"], "name": a["name"], "estimable": a["estimable"] and b["estimable"],
            "units_low": a["units_text"], "units_high": b["units_text"],
            "cost_type": a["cost_type"],
            # shared-deductible ordering can invert a single line; keep each range low <= high
            "low": None if a["patient_cost"] is None else min(a["patient_cost"], b["patient_cost"]),
            "high": None if a["patient_cost"] is None else max(a["patient_cost"], b["patient_cost"]),
            "note": a["note"] or b["note"]})
    est = [l for l in out_lines if l["estimable"]]
    warnings = [f"{l['name']}: {l['note']}" for l in out_lines if not l["estimable"]]
    assumptions = [f"{l['name']}: {l['note']}" for l in out_lines if l["estimable"] and l["note"]]
    clinic = store.clinics.get(npi)
    return {
        "npi": npi, "clinic": clinic.name if clinic else npi, "bundle_id": bundle_id, "lines": out_lines,
        "total_low": _money(sum(a["patient_cost"] for a in lo if a["estimable"])),
        "total_high": _money(sum(b["patient_cost"] for b in hi if b["estimable"])),
        "complete": not warnings, "warnings": warnings, "assumptions": assumptions,
        "deductible_remaining_before": pb.deductible_remaining,
        "deductible_remaining_after_low": _money(ded_lo), "deductible_remaining_after_high": _money(ded_hi),
        "fee_sources": sorted({store.fees[(tp_id, npi, l["cpt"])].source for l in out_lines
                               if (tp_id, npi, l["cpt"]) in store.fees}),
    }


def compare_clinics(clinics: list[dict]) -> dict | None:
    """Side-by-side view. Only clinics with every line priced are ranked, so a gap never looks like a saving."""
    if len(clinics) < 2:
        return None
    mid = lambda c: (c["total_low"] + c["total_high"]) / 2
    ranked = sorted((c for c in clinics if c["complete"]), key=mid)
    names = [c["clinic"] for c in clinics]
    rows = []
    for i, line in enumerate(clinics[0]["lines"]):
        cells = []
        for c in clinics:
            l = c["lines"][i]
            cells.append({"low": l["low"], "high": l["high"]} if l["estimable"] else None)
        rows.append({"cpt": line["cpt"], "name": line["name"], "cells": cells})
    out = {"clinics": names, "rows": rows,
           "totals": [{"low": c["total_low"], "high": c["total_high"], "complete": c["complete"]} for c in clinics],
           "cheapest": None, "savings": None}
    if len(ranked) >= 2:
        a, b = ranked[0], ranked[-1]
        out["cheapest"] = a["clinic"]
        out["savings"] = {"vs": b["clinic"], "amount": _money(mid(b) - mid(a))}
    return out
