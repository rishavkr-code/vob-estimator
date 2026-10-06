"""REST endpoints for the patient mobile app (`/v1/...`). Shapes match the app's Zod schemas (src/api/types.ts).

The backend does the pricing: the app sends its intent + clinic NPIs and renders the result.
PHI only travels in request bodies; nothing here logs request bodies.
"""
import asyncio
import math
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel

from . import assistant as assistant_mod
from . import cardvision
from .engine import benefit_for, compare_clinics, estimate_clinic
from .session import Session, normalize_dob

ELIGIBILITY_TIMEOUT = 18  # the app gives up after 20s
MEMBER_NOT_FOUND_CODES = {"67", "71", "72", "73", "75", "76", "79"}
PLAN_CATEGORIES = ["allergy_testing", "pulmonary_function", "allergen_immunotherapy",
                   "allergy_injection", "drug_administration", "specialty_drug"]


class CardFields(BaseModel):
    payerId: str
    payerName: str = ""
    memberId: str
    groupNumber: str = ""
    firstName: str
    lastName: str
    dateOfBirth: str  # YYYY-MM-DD


class ProvidersBody(BaseModel):
    specialty: str = "allergy_immunology"
    tier: int = 1
    near: dict


class EstimateBody(BaseModel):
    intentId: str
    drugId: str | None = None
    npis: list[str]


class AssistantBody(BaseModel):
    text: str


_hits: dict[str, deque] = defaultdict(deque)


def _limit(key: str, max_hits: int, window: int = 3600) -> None:
    now = time.time()
    q = _hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= max_hits:
        raise HTTPException(429, "Too many requests. Please try again later.")
    q.append(now)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


def _cents(x) -> int:
    return int(round((x or 0) * 100))


def _miles(a_lat, a_lng, b_lat, b_lng) -> float:
    r = 3958.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def plan_benefits_json(store, pb, payer_name: str) -> dict:
    """Parsed 271 -> the app's PlanBenefits shape (cents and basis points)."""
    plan = pb.by_stc.get("30")
    coins = plan.coinsurance if plan and plan.coinsurance is not None else None
    if coins is None:  # plan level silent: use the first procedure code's own coinsurance
        for cpt, info in store.catalog.items():
            if not cpt.startswith("99"):
                _, b, _ = benefit_for(cpt, info, pb)
                if b and b.coinsurance is not None:
                    coins = b.coinsurance
                    break
    coins_bps = round((coins or 0) * 10000)
    visit_info = store.catalog.get("99204")
    copay = None
    if visit_info:
        _, b, _ = benefit_for("99204", visit_info, pb)
        copay = b.copay if b and b.copay is not None else None
    rules = {}
    if copay is not None:
        rules["office_visit"] = {"kind": "copay", "copayCents": _cents(copay)}
    if coins is not None:
        for cat in PLAN_CATEGORIES:
            rules[cat] = {"kind": "coinsurance", "coinsuranceBps": coins_bps, "deductibleApplies": True}
    out = {
        "coverageStatus": "active" if pb.active else "inactive",
        "payerName": payer_name,
        "deductible": None if pb.deductible_total is None and pb.deductible_remaining is None else {
            "totalCents": _cents(pb.deductible_total if pb.deductible_total is not None else pb.deductible_remaining),
            "remainingCents": _cents(pb.deductible_remaining)},
        "outOfPocket": None if pb.oop_total is None and pb.oop_remaining is None else {
            "totalCents": _cents(pb.oop_total if pb.oop_total is not None else pb.oop_remaining),
            "remainingCents": _cents(pb.oop_remaining)},
        "defaultCoinsuranceBps": coins_bps,
        "rules": rules,
        "accumulators": {"copayCountsTowardDeductible": False, "deductibleAppliesToCopayServices": False,
                         "copayCountsTowardOutOfPocket": True},
    }
    if copay is not None:
        out["specialistCopayCents"] = _cents(copay)
    return out


def _cost_type(line: dict) -> str:
    """Engine cost types -> the app's CostShareType names."""
    if not line["estimable"]:
        return "not_covered" if "not covered" in (line["note"] or "").lower() else "price_unavailable"
    return {"deductible + coinsurance": "deductible_then_coinsurance"}.get(line["cost_type"], line["cost_type"])


def clinic_estimate_json(est: dict, pb, bundle_lines) -> dict:
    unpriced = [l for l in est["lines"] if not l["estimable"]]
    reasons = []
    if unpriced:
        reasons.append("price_unavailable")
    if est.get("assumptions"):
        reasons.append("benefit_rule_assumed")
    if any(bl.repeat_visits > 1 for bl in bundle_lines):
        reasons.append("multi_visit_course")
    if pb.oop_total is None and pb.oop_remaining is None:
        reasons.append("no_out_of_pocket_max")
    tier = "low" if unpriced else ("medium" if reasons else "high")

    def acc(total, before, low, high):
        if total is None and before is None:
            return None
        return {"totalCents": _cents(total if total is not None else before), "beforeCents": _cents(before),
                "afterLowCents": _cents(low), "afterHighCents": _cents(high)}
    return {
        "kind": "estimate", "npi": est["npi"], "name": est["clinic"],
        "totalLowCents": _cents(est["total_low"]), "totalHighCents": _cents(est["total_high"]),
        "confidence": tier, "confidenceReasons": reasons,
        "lines": [{"name": l["name"], "costType": _cost_type(l), "estimable": l["estimable"],
                   "lowCents": None if l["low"] is None else _cents(l["low"]),
                   "highCents": None if l["high"] is None else _cents(l["high"]),
                   "unitsLow": l["units_low"], "unitsHigh": l["units_high"], "note": l["note"]}
                  for l in est["lines"]],
        "deductible": acc(pb.deductible_total, est["deductible_remaining_before"],
                          est["deductible_remaining_after_low"], est["deductible_remaining_after_high"]),
        "outOfPocket": acc(pb.oop_total, est["oop_remaining_before"],
                           est["oop_remaining_after_low"], est["oop_remaining_after_high"])
        if est["oop_remaining_before"] is not None else None,
        "assumptions": est["assumptions"], "warnings": est["warnings"],
    }


def comparison_json(clinics: list[dict], estimates: list[dict]) -> dict | None:
    cmp_ = compare_clinics(estimates)
    if not cmp_:
        return None
    by_name = {c["name"]: c["npi"] for c in clinics}
    sv = cmp_["savings"]
    return {
        "clinicNpis": [c["npi"] for c in clinics],
        "rows": [{"name": r["name"], "cells": [None if c is None else {"lowCents": _cents(c["low"]),
                                                                          "highCents": _cents(c["high"])}
                                               for c in r["cells"]]} for r in cmp_["rows"]],
        "totals": [{"lowCents": _cents(t["low"]), "highCents": _cents(t["high"]), "complete": t["complete"]}
                   for t in cmp_["totals"]],
        "cheapestNpi": by_name.get(cmp_["cheapest"]) if cmp_["cheapest"] else None,
        "versusNpi": by_name.get(sv["vs"]) if sv else None,
        "savingsCents": _cents(sv["amount"]) if sv else None,
    }


def build_router(get_svc, get_agent) -> APIRouter:
    router = APIRouter(prefix="/v1")

    def session(sid: str | None) -> Session:
        if not sid:
            raise HTTPException(401, "Missing X-Session-Id")
        try:
            return get_svc().get(sid)
        except KeyError:
            raise HTTPException(401, "Session expired or unknown")

    @router.post("/patient-sessions")
    def create_session(request: Request):
        svc = get_svc()
        lim = svc.store.settings.get("limits", {})
        _limit("sess:" + _ip(request), lim.get("sessions_per_ip_per_hour", 30))
        svc.store.maybe_refresh()
        svc.purge_expired()
        s = svc.create()
        s.gateway = True
        exp = datetime.now(timezone.utc) + timedelta(seconds=svc.store.settings.get("session_idle_seconds", 900))
        return {"sessionId": s.id, "expiresAt": exp.isoformat()}

    @router.delete("/patient-sessions/{sid}", status_code=204)
    def end_session(sid: str, x_session_id: str | None = Header(None)):
        if x_session_id != sid:
            raise HTTPException(401, "Session mismatch")
        get_svc().delete(sid)

    @router.get("/payers")
    def payers(specialty: str = "allergy"):
        store = get_svc().store
        scope = set(store.settings.get("payers_in_scope", []))
        out = [{"id": p.trading_partner_id, "name": p.name, "supported": p.trading_partner_id in scope}
               for p in store.payers]
        have = {p["name"].lower() for p in out}
        for name in store.settings.get("other_payers", []):
            if name.lower() not in have:
                out.append({"id": "unsupported-" + "".join(c if c.isalnum() else "-" for c in name.lower()),
                            "name": name, "supported": False})
        return sorted(out, key=lambda p: (not p["supported"], p["name"]))

    @router.post("/patient-sessions/card-ocr")
    async def card_ocr(request: Request, front: UploadFile | None = File(None), back: UploadFile | None = File(None),
                       x_session_id: str | None = Header(None)):
        """Reads the card photo(s) with Claude vision. Images stay in memory and are never stored."""
        svc = get_svc()
        session(x_session_id)
        _limit("ocr:" + _ip(request), svc.store.settings.get("limits", {}).get("card_reads_per_ip_per_hour", 15))
        images = []
        for f in (front, back):
            if f is None:
                continue
            data = await f.read(cardvision.MAX_BYTES + 1)
            if len(data) > cardvision.MAX_BYTES:
                raise HTTPException(413, "Image too large (max 6 MB)")
            prepared = await asyncio.to_thread(cardvision.prepare, data)
            if not prepared:
                raise HTTPException(415, f"Unsupported or unreadable file (use {cardvision.SUPPORTED_TEXT})")
            images.append(prepared)
        return await cardvision.extract_card(images, svc.store)

    @router.post("/patient-sessions/eligibility")
    async def eligibility(body: CardFields, request: Request, x_session_id: str | None = Header(None)):
        svc = get_svc()
        s = session(x_session_id)
        lim = svc.store.settings.get("limits", {})
        scope = set(svc.store.settings.get("payers_in_scope", []))
        payer = next((p for p in svc.store.payers if p.trading_partner_id == body.payerId), None)
        if not payer or payer.trading_partner_id not in scope:
            return {"status": "payer_not_supported", "payerName": body.payerName or body.payerId}
        dob = normalize_dob(body.dateOfBirth)
        if not dob:
            raise HTTPException(422, "dateOfBirth must be a past date as YYYY-MM-DD")
        s.patient = {"first_name": body.firstName.strip(), "last_name": body.lastName.strip(),
                     "member_id": body.memberId.strip(), "date_of_birth": dob, "payer": payer.name}
        s.payer_tp_id = payer.trading_partner_id
        key = "|".join([payer.trading_partner_id, s.patient["member_id"].upper(), s.patient["first_name"].upper(),
                        s.patient["last_name"].upper(), dob])
        if s.benefits_task is None or s.benefits_key != key:  # same details: reuse, no extra Stedi cost
            if s.eligibility_calls >= lim.get("eligibility_per_session", 3):
                raise HTTPException(429, "Too many insurance checks in this session")
            _limit("elig:" + _ip(request), lim.get("eligibility_per_ip_per_hour", 20))
            s.eligibility_calls += 1
            s.benefits = None
            s.benefits_key = key
            s.benefits_task = asyncio.create_task(svc.fetch_benefits(s))
        try:
            pb = await asyncio.wait_for(asyncio.shield(s.benefits_task), ELIGIBILITY_TIMEOUT)
        except asyncio.TimeoutError:
            raise HTTPException(504, "The insurer is taking too long to respond")
        except Exception:
            s.benefits_task = None
            raise HTTPException(502, "Could not complete the insurance check")
        if not pb.active:
            if MEMBER_NOT_FOUND_CODES & set(pb.error_codes):
                return {"status": "member_not_found"}
            if pb.error_codes:
                s.benefits_task = None
                raise HTTPException(502, "The insurer could not process the request")
            return {"status": "inactive", "payerName": payer.name}
        return {"status": "ok", "benefits": plan_benefits_json(svc.store, pb, payer.name)}

    @router.post("/patient-sessions/providers")
    def providers(body: ProvidersBody, x_session_id: str | None = Header(None)):
        svc = get_svc()
        s = session(x_session_id)
        near = body.near
        origin = None
        if near.get("kind") == "coords":
            try:
                origin = (float(near["lat"]), float(near["lng"]))
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "near.lat and near.lng must be numbers")
        elif near.get("kind") == "zip":
            import zipcodes
            z = zipcodes.matching(str(near.get("zip", ""))[:5])
            if z:
                origin = (float(z[0]["lat"]), float(z[0]["long"]))
        else:
            raise HTTPException(422, "near.kind must be 'zip' or 'coords'")
        if origin is None:
            return []
        radius = svc.store.settings.get("provider_radius_miles")
        out = []
        for c in svc.store.priced_clinics(s.payer_tp_id or svc.store.payers[0].trading_partner_id):
            dist = _miles(*origin, c.lat, c.lng) if c.lat is not None and c.lng is not None else 0.0
            if radius and c.lat is not None and dist > radius:
                continue
            out.append({"npi": c.npi, "name": c.name, "addressLine": c.address_line, "city": c.city,
                        "state": c.state, "zip": c.zip, "phone": c.phone, "tier": 1,
                        "distanceMiles": round(dist, 1)})
        return sorted(out, key=lambda p: p["distanceMiles"])

    @router.post("/patient-sessions/estimate")
    async def estimate(body: EstimateBody, x_session_id: str | None = Header(None)):
        svc = get_svc()
        s = session(x_session_id)
        bundle_id = svc.store.settings.get("intent_bundles", {}).get(body.intentId)
        if not bundle_id or bundle_id not in svc.store.bundles:
            raise HTTPException(422, "Unknown intentId")
        if s.benefits is None and s.benefits_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(s.benefits_task), ELIGIBILITY_TIMEOUT)
            except Exception:
                raise HTTPException(409, "Insurance check not finished")
        pb = s.benefits
        if pb is None:
            raise HTTPException(409, "Run the insurance check first")
        if not pb.active:
            return {"status": "not_estimable", "reason": "coverage_inactive"}
        lines = svc.store.bundles[bundle_id]
        priced = {c.npi for c in svc.store.priced_clinics(s.payer_tp_id)}
        clinics, estimates = [], []
        for npi in dict.fromkeys(body.npis):
            if npi not in priced:
                clinics.append({"kind": "not_estimable", "npi": npi, "reason": "out_of_network"})
                continue
            est = estimate_clinic(svc.store, bundle_id, pb, s.payer_tp_id, npi)
            estimates.append(est)
            clinics.append(clinic_estimate_json(est, pb, lines))
        ok = [c for c in clinics if c["kind"] == "estimate"]
        return {"status": "ok", "dateOfService": date.today().isoformat(), "intentId": body.intentId,
                "clinics": clinics, "comparison": comparison_json(ok, estimates),
                "disclaimer": svc.store.settings["disclaimer"]}

    @router.post("/patient-sessions/assistant")
    async def assistant(body: AssistantBody, request: Request, x_session_id: str | None = Header(None)):
        """Chat that works out which supported visit type the patient means. See assistant.py."""
        svc = get_svc()
        s = session(x_session_id)
        _limit("asst:" + _ip(request), 60)
        if not body.text.strip():
            raise HTTPException(422, "text is empty")
        return await assistant_mod.assist(s, body.text[:1000], svc.store.settings)

    return router
