"""HTTP API for the frontend. Run: uvicorn vob_agent.api:app --reload"""
import asyncio
import base64
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .agent import Agent
from . import cardvision
from .data import ROOT, DataStore, load_menu
from .gateway import build_router
from .session import SessionService
from .stedi import get_client

# minimal .env loader (no extra dependency)
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

menu = load_menu()
# Runtime always reads Google Sheets. VOB_USE_FIXTURES=1 is for automated tests only.
store = DataStore(force_local=os.getenv("VOB_USE_FIXTURES") == "1")
store.settings["_menu_labels"] = {o["option_id"]: o["label"] for o in menu["options"]}
svc = SessionService(store, get_client())
agent = Agent(svc)

app = FastAPI(title="VOB Estimate Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(build_router(lambda: svc, lambda: agent))  # /v1/... endpoints for the mobile app


class SelectBody(BaseModel):
    option_id: str
    followup_answer: str | None = None  # "yes" | "no"


class ImageIn(BaseModel):
    media_type: str
    data: str  # base64, no data: prefix


class MessageBody(BaseModel):
    text: str = ""
    images: list[ImageIn] = []  # insurance card photo(s), step 1 only


def _session(sid):
    try:
        return svc.get(sid)
    except KeyError:
        raise HTTPException(404, "unknown session")


GREETING = ("Hi! I can estimate what you'd pay out of pocket for your allergy visit. "
            "First I'll check your insurance. What's your first and last name?")


def _menu_payload():
    m = menu["first_message"]
    return {"message": m["text"], "options": [{"option_id": o["option_id"], "label": o["label"]}
                                              for o in menu["options"]]}


def _money(x):
    return "n/a" if x is None else f"${x:,.2f}"


def _summary(est, label):
    """Plain-text recap added to the LLM history so follow-up questions use the real figures."""
    parts = [f"Patient chose: {label}. Estimates:"]
    for c in est["clinics"]:
        parts.append(f"- {c['clinic']}: {_money(c['total_low'])} to {_money(c['total_high'])}"
                     + ("" if c["complete"] else f" (not priced: {'; '.join(c['warnings'])})"))
        for l in c["lines"]:
            if l["estimable"]:
                parts.append(f"    * {l['name']} ({l['cost_type']}): {_money(l['low'])} to {_money(l['high'])}"
                             f", units {l['units_low']} to {l['units_high']}")
        parts.append(f"    deductible remaining before this visit: {_money(c['deductible_remaining_before'])}")
    cmp_ = est.get("comparison") or {}
    if cmp_.get("cheapest"):
        sv = cmp_["savings"]
        parts.append(f"Cheapest: {cmp_['cheapest']}, saving about {_money(sv['amount'])} "
                     f"versus {sv['vs']}.")
    return "\n".join(parts) + "\n" + est["disclaimer"]


@app.post("/sessions")
def create_session():
    store.maybe_refresh()
    s = svc.create()
    return {"session_id": s.id, "message": GREETING, "input_locked": False}


@app.post("/sessions/{sid}/select")
async def select(sid: str, body: SelectBody):
    s = _session(sid)
    if s.phase != "choose":
        raise HTTPException(409, "Options are only available after your insurance details are collected")
    opt = next((o for o in menu["options"] if o["option_id"] == body.option_id), None)
    if not opt:
        raise HTTPException(400, "unknown option")
    bundle = opt["bundle_id"]
    if opt.get("follow_up_question"):
        if body.followup_answer not in ("yes", "no"):
            return {"input_locked": True, "message": opt["follow_up_question"],
                    "quick_replies": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]}
        if body.followup_answer == "yes":  # tested before -> established-patient bundle
            bundle = opt["follow_up_alt_bundle_id"]
    s.option_id, s.bundle_id, s.phase = opt["option_id"], bundle, "chat"
    if not bundle:  # "not sure": the agent asks questions, then sets the bundle itself
        msg = "No problem. Tell me a bit about why you were referred and I'll find the right estimate."
        s.history += [{"role": "user", "content": f"Patient chose: {opt['label']}"},
                      {"role": "assistant", "content": msg}]
        return {"input_locked": False, "message": msg, "estimate": None}
    est = await svc.run_estimate(s)
    if est.get("status") != "ok":
        msg = {"inactive_or_not_found": "I couldn't confirm active coverage with those details. "
                                        "Please double-check your member ID, date of birth and insurer.",
               }.get(est.get("status"), "I couldn't complete your insurance check just now. Please try again.")
        s.history += [{"role": "user", "content": f"Patient chose: {opt['label']}"},
                      {"role": "assistant", "content": msg}]
        return {"input_locked": False, "message": msg, "estimate": est}
    msg = "Here's your estimate."
    s.history += [{"role": "user", "content": _summary(est, opt["label"])},
                  {"role": "assistant", "content": msg}]
    return {"input_locked": False, "message": msg, "estimate": est}


@app.post("/sessions/{sid}/messages")
async def message(sid: str, body: MessageBody):
    s = _session(sid)
    if not s.unlocked:
        raise HTTPException(409, "Choose one of the options first")
    images = []
    if body.images:
        if s.phase != "collecting":
            raise HTTPException(400, "Card photos are only accepted while collecting insurance details")
        if not cardvision.vision_enabled():
            return {"message": "I can't read photos right now. Could you type your name, member ID, "
                               "date of birth and insurance company instead?",
                    "input_locked": False, "missing": s.missing(), "estimate": s.estimate}
        if len(body.images) > cardvision.MAX_IMAGES:
            raise HTTPException(400, "At most 2 images")
        for im in body.images:
            try:
                raw = base64.b64decode(im.data, validate=True)
            except Exception:
                raise HTTPException(400, "Image is not valid base64")
            if len(raw) > cardvision.MAX_BYTES:
                raise HTTPException(413, "Image too large (max 6 MB)")
            prepared = await asyncio.to_thread(cardvision.prepare, raw)
            if not prepared:
                raise HTTPException(415, f"Unsupported or unreadable file (use {cardvision.SUPPORTED_TEXT})")
            data, mt = prepared
            images.append((mt, base64.standard_b64encode(data).decode()))
    elif not body.text.strip():
        raise HTTPException(400, "Empty message")
    reply = await agent.respond(s, body.text, images or None)
    out = {"message": reply, "input_locked": False, "missing": s.missing(), "estimate": s.estimate}
    if s.phase == "collecting" and s.ready():   # step 1 done -> lock the input, show the menu
        s.phase = "choose"
        out.update(input_locked=True, menu=_menu_payload())
    return out


@app.get("/sessions/{sid}/estimate")
def get_estimate(sid: str):
    s = _session(sid)
    return s.estimate or {"status": "not_ready", "missing": s.missing()}


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}


@app.post("/admin/reload")
def reload_data(x_admin_token: str | None = Header(None)):
    token = os.getenv("ADMIN_TOKEN")  # unset = endpoint disabled
    if not token or x_admin_token != token:
        raise HTTPException(403, "forbidden")
    store.refresh()
    return {"sources": store.source, "fee_rows": len(store.fees), "last_error": store.last_error}


@app.get("/", include_in_schema=False)
def chat_page():
    return FileResponse(Path(__file__).parent / "static" / "chat.html")
