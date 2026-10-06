"""Session state, patient-slot validation and the background eligibility fetch."""
import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date

from .data import DataStore
from .engine import compare_clinics, estimate_clinic
from .parser271 import parse_271
from .stedi import build_request

SLOTS = ["first_name", "last_name", "member_id", "date_of_birth", "payer"]


def normalize_dob(text: str) -> str | None:
    """Accept YYYY-MM-DD, YYYYMMDD or MM/DD/YYYY; return YYYYMMDD."""
    t = text.strip()
    for pat, order in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "ymd"), (r"^(\d{4})(\d{2})(\d{2})$", "ymd"),
                       (r"^(\d{1,2})/(\d{1,2})/(\d{4})$", "mdy")):
        m = re.match(pat, t)
        if m:
            a, b, c = map(int, m.groups())
            y, mo, d = (a, b, c) if order == "ymd" else (c, a, b)
            try:
                dt = date(y, mo, d)
            except ValueError:
                return None
            return dt.strftime("%Y%m%d") if dt < date.today() else None
    return None


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    phase: str = "collecting"       # collecting (free chat) -> choose (input locked, menu) -> chat
    option_id: str | None = None
    bundle_id: str | None = None
    pending_followup: str | None = None
    patient: dict = field(default_factory=dict)
    payer_tp_id: str | None = None
    eligibility_task: asyncio.Task | None = None
    estimate: dict | None = None
    history: list = field(default_factory=list)  # LLM message history
    # --- mobile-app (gateway) flow ---
    touched: float = field(default_factory=time.time)
    gateway: bool = False                 # True: eligibility is driven by /v1 endpoints, not the chat flow
    benefits: object | None = None        # parsed PlanBenefits shared by every clinic estimate
    benefits_task: asyncio.Task | None = None
    benefits_key: str | None = None
    eligibility_calls: int = 0

    @property
    def unlocked(self) -> bool:
        return self.phase != "choose"

    def missing(self) -> list[str]:
        return [s for s in SLOTS if not self.patient.get(s)]

    def ready(self) -> bool:
        return not self.missing()


class SessionService:
    def __init__(self, store: DataStore, stedi):
        self.store, self.stedi = store, stedi
        self.sessions: dict[str, Session] = {}

    def create(self) -> Session:
        s = Session()
        self.sessions[s.id] = s
        return s

    def get(self, sid: str) -> Session:
        s = self.sessions[sid]
        idle = self.store.settings.get("session_idle_seconds", 600)
        if time.time() - s.touched > idle:
            self.sessions.pop(sid, None)
            raise KeyError(sid)
        s.touched = time.time()
        return s

    def delete(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            for t in (s.eligibility_task, s.benefits_task):
                if t and not t.done():
                    t.cancel()

    def purge_expired(self) -> None:
        idle = self.store.settings.get("session_idle_seconds", 600)
        for sid in [k for k, v in self.sessions.items() if time.time() - v.touched > idle]:
            self.delete(sid)

    async def fetch_benefits(self, s: Session):
        """One set of 270s (batched STCs) for the first priced clinic; every clinic estimate reuses the result.

        Used by the mobile flow, where eligibility runs before a clinic is chosen. Cigna returned identical
        plan benefits for different provider NPIs, so one lookup is enough (and ~6x cheaper than per clinic).
        """
        payer = next(p for p in self.store.payers if p.trading_partner_id == s.payer_tp_id)
        clinics = self.store.priced_clinics(payer.trading_partner_id)
        if not clinics:
            raise RuntimeError("no priced clinic available for eligibility")
        stcs = self.all_stcs()
        groups = [stcs[i:i + payer.max_stcs_per_call] for i in range(0, len(stcs), payer.max_stcs_per_call)]
        pt, c = s.patient, clinics[0]
        reqs = [build_request(pt["first_name"], pt["last_name"], pt["member_id"], pt["date_of_birth"],
                              payer.trading_partner_id, c.npi, c.name, g) for g in groups]
        s.benefits = parse_271(await asyncio.gather(*(self.stedi.check(r) for r in reqs)))
        return s.benefits

    # ---- eligibility ----------------------------------------------------
    def all_stcs(self) -> list[str]:
        """Eligibility runs before the patient picks a visit reason, so ask for every STC any bundle needs."""
        stcs = list(self.store.settings.get("stedi", {}).get("plan_level_stcs", ["30"]))
        for lines in self.store.bundles.values():
            for bl in lines:
                info = self.store.catalog.get(bl.cpt)
                for s in (info.stcs if info else []):
                    if s not in stcs:
                        stcs.append(s)
        return stcs

    async def _fetch(self, s: Session) -> dict:
        payer = next(p for p in self.store.payers if p.trading_partner_id == s.payer_tp_id)
        stcs = self.all_stcs()
        groups = [stcs[i:i + payer.max_stcs_per_call] for i in range(0, len(stcs), payer.max_stcs_per_call)]
        pt = s.patient
        out = {}
        for clinic in self.store.priced_clinics(payer.trading_partner_id):
            reqs = [build_request(pt["first_name"], pt["last_name"], pt["member_id"], pt["date_of_birth"],
                                  payer.trading_partner_id, clinic.npi, clinic.name, g) for g in groups]
            out[clinic.npi] = await asyncio.gather(*(self.stedi.check(r) for r in reqs))
        return out

    def maybe_start_eligibility(self, s: Session):
        if s.gateway:
            return
        if s.ready() and s.eligibility_task is None:
            s.eligibility_task = asyncio.create_task(self._fetch(s))

    async def run_estimate(self, s: Session) -> dict:
        """Wait for the background 271s, then price every clinic. Returns PHI-free structured JSON."""
        if not s.ready() or not s.bundle_id:
            return {"status": "missing_info", "missing": s.missing() or ["reason_for_visit"]}
        self.maybe_start_eligibility(s)
        try:
            raw = await s.eligibility_task
        except Exception as e:  # network, auth, payer timeout
            s.eligibility_task = None
            return {"status": "eligibility_error", "detail": type(e).__name__}
        clinics = []
        for npi, responses in raw.items():
            pb = parse_271(responses)
            if not pb.active:
                return {"status": "inactive_or_not_found",
                        "detail": "; ".join(pb.notes) or "Coverage was not reported as active"}
            clinics.append(estimate_clinic(self.store, s.bundle_id, pb, s.payer_tp_id, npi))
        if not clinics:
            return {"status": "no_priced_clinics"}
        s.estimate = {"status": "ok", "date_of_service": date.today().isoformat(), "clinics": clinics,
                      "comparison": compare_clinics(clinics),
                      "disclaimer": self.store.settings["disclaimer"]}
        return s.estimate
