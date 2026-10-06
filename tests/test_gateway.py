import os

os.environ["VOB_USE_FIXTURES"] = "1"
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from vob_agent import api
from vob_agent.agent import Agent
from vob_agent.data import BundleLine
from vob_agent.stedi import MockStedi

CARD = {"payerId": "62308", "payerName": "Cigna", "memberId": "ABC123", "groupNumber": "G1",
        "firstName": "Jane", "lastName": "Doe", "dateOfBirth": "1985-03-15"}
H = lambda sid: {"X-Session-Id": sid}


class BadMemberStedi:
    async def check(self, request):
        return {"planStatus": [], "benefitsInformation": [],
                "errors": [{"code": "72", "description": "Invalid/Missing Subscriber/Insured ID"}]}


class AssistLLM:
    def __init__(self):
        self.n = 0

    async def complete(self, system, messages, tools):
        self.n += 1
        if self.n == 1:
            return NS(content=[NS(type="tool_use", id="t1", name="set_visit_reason", input={"bundle_id": "BREATHING_TEST"})])
        return NS(content=[NS(type="text", text="That sounds like a breathing test visit.")])


def _client():
    api.svc.stedi = MockStedi()
    api.svc.sessions.clear()
    return TestClient(api.app)


def _session(c):
    return c.post("/v1/patient-sessions").json()["sessionId"]


def test_full_mobile_flow():
    with _client() as c:
        s = c.post("/v1/patient-sessions").json()
        sid = s["sessionId"]
        assert s["expiresAt"]
        payers = c.get("/v1/payers?specialty=allergy").json()
        assert payers[0] == {"id": "62308", "name": "Cigna", "supported": True}
        assert any(not p["supported"] for p in payers)
        assert c.post("/v1/patient-sessions/card-ocr", headers=H(sid)).json() == {"status": "unreadable"}

        el = c.post("/v1/patient-sessions/eligibility", json=CARD, headers=H(sid)).json()
        assert el["status"] == "ok"
        b = el["benefits"]
        assert b["coverageStatus"] == "active" and b["deductible"] == {"totalCents": 70000, "remainingCents": 67900}
        assert b["specialistCopayCents"] == 4000 and b["defaultCoinsuranceBps"] == 2000
        assert b["rules"]["office_visit"] == {"kind": "copay", "copayCents": 4000}

        prov = c.post("/v1/patient-sessions/providers", headers=H(sid),
                      json={"specialty": "allergy_immunology", "tier": 1, "near": {"kind": "zip", "zip": "80202"}}).json()
        assert {p["npi"] for p in prov} == {"1871550590", "1609834373"} and all(p["tier"] == 1 for p in prov)
        assert c.post("/v1/patient-sessions/providers", headers=H(sid),
                      json={"near": {"kind": "zip", "zip": "00000"}}).json() == []

        r = c.post("/v1/patient-sessions/estimate", headers=H(sid),
                   json={"intentId": "allergy_testing_new", "npis": ["1871550590", "1609834373", "9999999999"]})
        est = r.json()
        assert r.status_code == 200 and est["status"] == "ok"
        kinds = [x["kind"] for x in est["clinics"]]
        assert kinds == ["estimate", "estimate", "not_estimable"]
        bay = est["clinics"][0]
        assert (bay["totalLowCents"], bay["totalHighCents"]) == (50000, 77520)
        assert all(isinstance(bay[k], int) for k in ("totalLowCents", "totalHighCents"))
        assert bay["deductible"]["beforeCents"] == 67900 and bay["confidence"] in ("high", "medium")
        cmp_ = est["comparison"]
        assert cmp_["cheapestNpi"] == "1609834373" and cmp_["versusNpi"] == "1871550590" and cmp_["savingsCents"] > 0
        assert {l["costType"] for l in bay["lines"]} <= {"copay", "deductible", "deductible_then_coinsurance", "coinsurance"}
        assert "99204" not in str(bay["lines"])  # no billing codes in patient-facing fields
        assert "ABC123" not in str(est) and "Doe" not in str(est)

        assert c.delete(f"/v1/patient-sessions/{sid}", headers=H(sid)).status_code == 204
        assert c.post("/v1/patient-sessions/card-ocr", headers=H(sid)).status_code == 401


def test_eligibility_statuses():
    with _client() as c:
        sid = _session(c)
        r = c.post("/v1/patient-sessions/eligibility", headers=H(sid), json={**CARD, "payerId": "unsupported-aetna", "payerName": "Aetna"})
        assert r.json() == {"status": "payer_not_supported", "payerName": "Aetna"}
        api.svc.stedi = BadMemberStedi()
        assert c.post("/v1/patient-sessions/eligibility", headers=H(sid), json=CARD).json() == {"status": "member_not_found"}
        assert c.post("/v1/patient-sessions/eligibility", headers=H(sid), json={**CARD, "dateOfBirth": "2999-01-01"}).status_code == 422


def test_guards():
    with _client() as c:
        assert c.post("/v1/patient-sessions/eligibility", json=CARD).status_code == 401
        sid = _session(c)
        r = c.post("/v1/patient-sessions/estimate", headers=H(sid), json={"intentId": "follow_up", "npis": ["1871550590"]})
        assert r.status_code == 409                       # eligibility not run yet
        assert c.post("/v1/patient-sessions/estimate", headers=H(sid), json={"intentId": "nope", "npis": []}).status_code == 422
        assert c.post("/admin/reload").status_code == 403  # admin disabled without ADMIN_TOKEN


def test_session_expires_when_idle():
    with _client() as c:
        sid = _session(c)
        api.svc.sessions[sid].touched -= 10_000
        assert c.post("/v1/patient-sessions/card-ocr", headers=H(sid)).status_code == 401


def test_cpt_max_used_for_high_scenario():
    with _client() as c:
        sid = _session(c)
        c.post("/v1/patient-sessions/eligibility", headers=H(sid), json=CARD)
        api.svc.store.bundles["FOLLOW_UP"] = [BundleLine("99213", 1, 1, 1, 1, cpt_max="99214")]
        try:
            est = c.post("/v1/patient-sessions/estimate", headers=H(sid),
                         json={"intentId": "follow_up", "npis": ["1871550590"]}).json()
        finally:
            api.svc.store.refresh()
        line = est["clinics"][0]["lines"][0]
        assert line["lowCents"] == line["highCents"] == 4000   # copay either way, but the high code resolved


def test_assistant_maps_free_text_to_intent():
    with _client() as c:
        api.agent = Agent(api.svc, AssistLLM())
        sid = _session(c)
        r = c.post("/v1/patient-sessions/assistant", headers=H(sid), json={"text": "I get short of breath"}).json()
        assert r["intentId"] == "breathing_test" and "breathing" in r["reply"]
