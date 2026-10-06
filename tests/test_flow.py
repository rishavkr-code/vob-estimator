import json
import os

os.environ["VOB_USE_FIXTURES"] = "1"  # tests use the checked-in fixtures, never the live sheets
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from vob_agent import api
from vob_agent.agent import Agent
from vob_agent.stedi import MockStedi


class FakeLLM:
    """Scripted stand-in for Claude: records patient info, then asks for the estimate."""
    def __init__(self):
        self.step = 0

    async def complete(self, system, messages, tools):
        self.step += 1
        use = lambda i, n, a: NS(type="tool_use", id=i, name=n, input=a)
        if self.step == 1:
            return NS(content=[use("t1", "record_patient_info", {
                "first_name": "Jane", "last_name": "Doe", "member_id": "ABC123",
                "date_of_birth": "03/15/1985", "payer_name": "Cigna"})])
        return NS(content=[NS(type="text", text="Thanks Jane!")])


def test_info_first_then_locked_menu_then_estimate_and_comparison():
    api.svc.stedi = MockStedi()
    api.agent = Agent(api.svc, FakeLLM())
    with TestClient(api.app) as c:
        sess = c.post("/sessions").json()
        sid = sess["session_id"]
        assert sess["input_locked"] is False                       # step 1: free chat for patient details
        assert c.post(f"/sessions/{sid}/select", json={"option_id": "ALLERGY_TEST"}).status_code == 409

        r = c.post(f"/sessions/{sid}/messages", json={"text": "Jane Doe, ABC123, 3/15/1985, Cigna"}).json()
        assert r["input_locked"] is True and len(r["menu"]["options"]) == 8   # step 2: locked + options
        assert c.post(f"/sessions/{sid}/messages", json={"text": "hi"}).status_code == 409

        r = c.post(f"/sessions/{sid}/select", json={"option_id": "ALLERGY_TEST"}).json()
        assert r["input_locked"] is True and r["quick_replies"]
        r = c.post(f"/sessions/{sid}/select", json={"option_id": "ALLERGY_TEST", "followup_answer": "no"}).json()
        assert r["input_locked"] is False                          # chat unlocks after choosing
        est = r["estimate"]                                        # step 3: estimate
        assert est["status"] == "ok" and len(est["clinics"]) == 2
        assert {cl["bundle_id"] for cl in est["clinics"]} == {"ALLERGY_TEST_NEW"}
        cmp_ = est["comparison"]                                   # step 4: comparison
        assert len(cmp_["clinics"]) == 2 and cmp_["cheapest"] == "ALLERGY ASTHMA CLINIC LTD"
        assert cmp_["savings"]["amount"] > 0
        assert "ABC123" not in json.dumps(est) and "Doe" not in json.dumps(est)
