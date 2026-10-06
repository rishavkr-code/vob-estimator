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


def test_provider_details_and_distance():
    with _client() as c:
        sid = _session(c)
        near_bay = c.post("/v1/patient-sessions/providers", headers=H(sid),
                          json={"near": {"kind": "zip", "zip": "94598"}}).json()
        bay, phx = near_bay[0], near_bay[1]
        assert bay["name"] == "ALLERGY & ASTHMA MEDICAL GROUP OF THE BAY AREA INC"   # untruncated name
        assert (bay["city"], bay["state"], bay["zip"]) == ("Walnut Creek", "CA", "94598")
        assert bay["distanceMiles"] < 1 and phx["city"] == "Phoenix"
        assert 550 < phx["distanceMiles"] < 800                                      # Walnut Creek to Phoenix
        near_phx = c.post("/v1/patient-sessions/providers", headers=H(sid),
                          json={"near": {"kind": "zip", "zip": "85013"}}).json()
        assert near_phx[0]["npi"] == "1609834373"                                    # nearest first


# ---------------- card photo reading ----------------
import base64

from vob_agent import cardvision

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def test_card_ocr_endpoint_returns_app_shape(monkeypatch):
    seen = {}

    async def fake_extract(images, store):
        seen["n"], seen["types"] = len(images), [t for _, t in images]
        return {"status": "ok", "fields": {"firstName": "Jane", "lastName": "Doe", "memberId": "U123", "payerId": "62308",
                                           "payerName": "Cigna"}, "lowConfidenceFields": ["memberId"]}
    monkeypatch.setattr(cardvision, "extract_card", fake_extract)
    with _client() as c:
        sid = _session(c)
        r = c.post("/v1/patient-sessions/card-ocr", headers=H(sid),
                   files={"front": ("front.jpg", PNG, "image/jpeg"), "back": ("back.jpg", PNG, "image/jpeg")})
        assert r.status_code == 200 and r.json()["status"] == "ok"
        assert r.json()["fields"]["memberId"] == "U123" and r.json()["lowConfidenceFields"] == ["memberId"]
        assert seen == {"n": 2, "types": ["image/png", "image/png"]}      # type sniffed from bytes, not the header
        bad = c.post("/v1/patient-sessions/card-ocr", headers=H(sid), files={"front": ("x.txt", b"not an image", "image/jpeg")})
        assert bad.status_code == 415
        big = c.post("/v1/patient-sessions/card-ocr", headers=H(sid),
                     files={"front": ("big.png", PNG + b"0" * cardvision.MAX_BYTES, "image/png")})
        assert big.status_code == 413
        assert c.post("/v1/patient-sessions/card-ocr", files={"front": ("a.png", PNG, "image/png")}).status_code == 401


class PhotoLLM:
    """Records what the model is shown; reads the 'card' by calling record_patient_info like the real model would."""
    def __init__(self):
        self.n, self.saw_image = 0, False

    async def complete(self, system, messages, tools):
        self.n += 1
        if self.n == 1:
            self.saw_image = any(isinstance(m["content"], list) and any(b.get("type") == "image" for b in m["content"])
                                 for m in messages)
            return NS(content=[NS(type="tool_use", id="t1", name="record_patient_info", input={
                "first_name": "Jane", "last_name": "Doe", "member_id": "U123", "payer_name": "Cigna"})])
        return NS(content=[NS(type="text", text="Thanks Jane! What's your date of birth?")])


def test_chat_card_photo_is_read_then_dropped_from_history(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    llm = PhotoLLM()
    from vob_agent import api as a
    with _client() as c:
        a.agent = Agent(a.svc, llm)
        sid = c.post("/sessions").json()["session_id"]
        r = c.post(f"/sessions/{sid}/messages", json={"text": "", "images": [
            {"media_type": "image/png", "data": base64.b64encode(PNG).decode()}]})
        assert r.status_code == 200 and "date of birth" in r.json()["message"]
        assert llm.saw_image                                           # the model saw the photo this turn
        assert r.json()["missing"] == ["date_of_birth"]                # only DOB left to ask
        hist = a.svc.sessions[sid].history
        assert not any(isinstance(m["content"], list) and any(b.get("type") == "image" for b in m["content"]) for m in hist)
        assert c.post(f"/sessions/{sid}/messages", json={"text": "", "images": []}).status_code == 400
        bad = c.post(f"/sessions/{sid}/messages", json={"images": [{"media_type": "image/png", "data": "!!notb64"}]})
        assert bad.status_code == 400
        txt = c.post(f"/sessions/{sid}/messages", json={"images": [{"media_type": "image/png", "data": base64.b64encode(b"hello").decode()}]})
        assert txt.status_code == 415


def test_photo_without_vision_asks_to_type(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with _client() as c:
        sid = c.post("/sessions").json()["session_id"]
        r = c.post(f"/sessions/{sid}/messages", json={"images": [{"media_type": "image/png", "data": base64.b64encode(PNG).decode()}]})
        assert r.status_code == 200 and "type" in r.json()["message"].lower()


def _sample_files():
    import io

    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    im = Image.new("RGB", (320, 200), "#e8702a")
    out = {}
    for name, fmt in (("jpeg", "JPEG"), ("png", "PNG"), ("webp", "WEBP"), ("heic", "HEIF"), ("pdf", "PDF")):
        b = io.BytesIO()
        im.save(b, format=fmt)
        out[name] = b.getvalue()
    return out


def test_sniff_and_prepare_all_supported_types():
    f = _sample_files()
    assert [cardvision.sniff(f[k]) for k in ("jpeg", "png", "webp", "heic", "pdf")] == [
        "image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"]
    data, mt = cardvision.prepare(f["heic"])
    assert mt == "image/jpeg" and cardvision.sniff(data) == "image/jpeg"     # HEIC becomes JPEG for the model
    assert cardvision.prepare(f["pdf"])[1] == "application/pdf"
    assert cardvision.prepare(b"%PDF-" and b"plain text") is None
    assert cardvision.prepare(b"\x00\x00\x00\x18ftypheic" + b"junk") is None  # corrupt HEIC rejected, no crash
    assert cardvision.image_block("application/pdf", b"x")["type"] == "document"
    assert cardvision.image_block("image/jpeg", b"x")["type"] == "image"


def test_card_ocr_accepts_heic_and_pdf(monkeypatch):
    seen = []

    async def fake_extract(images, store):
        seen.append([t for _, t in images])
        return {"status": "ok", "fields": {"memberId": "U1"}, "lowConfidenceFields": []}
    monkeypatch.setattr(cardvision, "extract_card", fake_extract)
    f = _sample_files()
    with _client() as c:
        sid = _session(c)
        for key, mime in (("heic", "image/heic"), ("pdf", "application/pdf"), ("webp", "image/webp")):
            r = c.post("/v1/patient-sessions/card-ocr", headers=H(sid), files={"front": (f"c.{key}", f[key], mime)})
            assert r.status_code == 200 and r.json()["status"] == "ok", key
        assert seen == [["image/jpeg"], ["application/pdf"], ["image/webp"]]
        bad = c.post("/v1/patient-sessions/card-ocr", headers=H(sid), files={"front": ("c.pdf", b"not really a pdf", "application/pdf")})
        assert bad.status_code == 415
