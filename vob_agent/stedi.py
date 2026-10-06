"""Stedi 270/271 client (live) and a deterministic mock for development/tests."""
import json
import os
from pathlib import Path
from datetime import date

import httpx

STEDI_URL = "https://healthcare.us.stedi.com/2024-04-01/change/medicalnetwork/eligibility/v3"


def build_request(first, last, member_id, dob, tp_id, npi, org_name, stcs, dos=None) -> dict:
    """dob and dos are YYYYMMDD. Shape follows the raw request in the spec."""
    return {
        "subscriber": {"firstName": first, "lastName": last, "memberId": member_id, "dateOfBirth": dob},
        "controlNumber": None,
        "tradingPartnerServiceId": tp_id,
        "provider": {"npi": npi, "organizationName": org_name},
        "encounter": {"serviceTypeCodes": list(stcs), "dateOfService": dos or date.today().strftime("%Y%m%d")},
    }


class LiveStedi:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["STEDI_API_KEY"]

    async def check(self, request: dict) -> dict:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(STEDI_URL, json=request,
                             headers={"Authorization": self.api_key, "Content-Type": "application/json"})
            if r.status_code >= 400:
                raise RuntimeError(f"Stedi HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
        if os.getenv("VOB_DEBUG_DUMP") == "1":  # local debugging only: raw 271 contains PHI, folder is gitignored
            import time
            d = Path(__file__).resolve().parent.parent / "debug"
            d.mkdir(exist_ok=True)
            stcs = "-".join(request["encounter"]["serviceTypeCodes"])
            (d / f"271_{request['provider']['npi']}_{stcs}_{int(time.time())}.json").write_text(
                json.dumps(data, indent=1))
        return data


class MockStedi:
    """Plan: $700 individual deductible ($679 remaining), $3,000 OOP remaining,
    $40 copay for office visits (STC 98/96/BY/A0), 20% coinsurance for everything else."""
    COPAY_STCS = {"98", "96", "BY", "A0"}

    def __init__(self, deductible_remaining=679.0, deductible_total=700.0,
                 oop_remaining=3000.0, oop_total=4000.0, copay=40.0, coinsurance=0.2):
        self.p = dict(ded_rem=deductible_remaining, ded_tot=deductible_total,
                      oop_rem=oop_remaining, oop_tot=oop_total, copay=copay, coins=coinsurance)

    async def check(self, request: dict) -> dict:
        p = self.p
        stcs = request["encounter"]["serviceTypeCodes"]
        base = {"inPlanNetworkIndicatorCode": "Y", "coverageLevelCode": "IND"}
        info = [
            {"code": "1", "name": "Active Coverage", "serviceTypeCodes": ["30"], **base},
            {"code": "C", "name": "Deductible", "serviceTypeCodes": ["30"], "timeQualifierCode": "23",
             "benefitAmount": str(p["ded_tot"]), **base},
            {"code": "C", "name": "Deductible", "serviceTypeCodes": ["30"], "timeQualifierCode": "29",
             "benefitAmount": str(p["ded_rem"]), **base},
            {"code": "G", "name": "Out of Pocket (Stop Loss)", "serviceTypeCodes": ["30"],
             "timeQualifierCode": "23", "benefitAmount": str(p["oop_tot"]), **base},
            {"code": "G", "name": "Out of Pocket (Stop Loss)", "serviceTypeCodes": ["30"],
             "timeQualifierCode": "29", "benefitAmount": str(p["oop_rem"]), **base},
        ]
        for s in stcs:
            if s == "30":
                continue
            if s in self.COPAY_STCS:
                info.append({"code": "B", "name": "Co-Payment", "serviceTypeCodes": [s],
                             "benefitAmount": str(p["copay"]), **base})
            else:
                info.append({"code": "A", "name": "Co-Insurance", "serviceTypeCodes": [s],
                             "benefitPercent": str(p["coins"]), **base})
        return {"planStatus": [{"statusCode": "1", "status": "Active Coverage", "serviceTypeCodes": ["30"]}],
                "benefitsInformation": info}


def get_client():
    mode = os.getenv("STEDI_MODE", "mock").lower()
    return LiveStedi() if mode == "live" else MockStedi()
