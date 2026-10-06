"""Reduce one or more Stedi 271 JSON responses to the cost-share facts the estimate engine needs."""
from dataclasses import dataclass, field

TOTAL_QUALIFIERS = {"22", "23", "24", "25", "26", "27"}  # service/calendar year, episode, etc.
REMAINING_QUALIFIER = "29"


@dataclass
class StcBenefit:
    copay: float | None = None
    coinsurance: float | None = None  # fraction, e.g. 0.2
    not_covered: bool = False


@dataclass
class PlanBenefits:
    active: bool = False
    deductible_total: float | None = None
    deductible_remaining: float | None = None
    oop_total: float | None = None
    oop_remaining: float | None = None
    by_stc: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _in_network(e) -> bool:
    return e.get("inPlanNetworkIndicatorCode") in (None, "Y", "W")


def _level_rank(e) -> int:
    return 0 if e.get("coverageLevelCode") in (None, "IND") else 1  # prefer individual


def parse_271(responses: list[dict]) -> PlanBenefits:
    pb = PlanBenefits()
    entries = []
    for r in responses:
        if any(s.get("statusCode") == "1" for s in r.get("planStatus", [])):
            pb.active = True
        entries += r.get("benefitsInformation", [])
        for err in r.get("errors", []):
            pb.notes.append(f"payer error: {err.get('description') or err.get('code')}")
    if any(e.get("code") == "1" for e in entries):
        pb.active = True
    entries = [e for e in entries if _in_network(e)]
    entries.sort(key=_level_rank)  # individual first, so setdefault-style picks prefer IND

    def plan_amount(code, qualifiers):
        for e in entries:
            if e.get("code") == code and e.get("timeQualifierCode") in qualifiers:
                v = _f(e.get("benefitAmount"))
                if v is not None:
                    return v
        return None

    pb.deductible_total = plan_amount("C", TOTAL_QUALIFIERS)
    pb.deductible_remaining = plan_amount("C", {REMAINING_QUALIFIER})
    pb.oop_total = plan_amount("G", TOTAL_QUALIFIERS)
    pb.oop_remaining = plan_amount("G", {REMAINING_QUALIFIER})
    if pb.deductible_remaining is None and pb.deductible_total is not None:
        pb.deductible_remaining = pb.deductible_total
        pb.notes.append("deductible remaining not reported; assumed full deductible is unmet")
    if pb.oop_remaining is None and pb.oop_total is not None:
        pb.oop_remaining = pb.oop_total

    for e in entries:
        code = e.get("code")
        if code not in ("B", "A", "I"):
            continue
        for stc in e.get("serviceTypeCodes", []):
            b = pb.by_stc.setdefault(stc, StcBenefit())
            if code == "B":  # several copays can share an STC (PCP vs specialist): keep the highest
                v = _f(e.get("benefitAmount"))
                if v is not None and (b.copay is None or v > b.copay):
                    b.copay = v
            elif code == "A" and b.coinsurance is None:
                b.coinsurance = _f(e.get("benefitPercent"))
            elif code == "I":
                b.not_covered = True
    return pb
