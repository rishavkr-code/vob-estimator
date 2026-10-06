"""Chat agent: Claude tool-calling loop around the deterministic services."""
import json
import os

from .session import Session, SessionService
from .tools import TOOL_SPECS, run_tool

SYSTEM = """You are a friendly assistant that helps patients of allergy and asthma clinics understand what \
a visit may cost them out of pocket. You do NOT compute prices: all numbers come from the estimate data.

{phase_text}

Rules:
- Only talk about cost estimates for allergy/asthma visits. Politely decline anything else (no medical advice).
- Never invent, adjust or round numbers. Quote totals as the range given (low to high). The range exists because the number of tests or units at the visit varies (see each line's units); never attribute it to the deductible.
- Explain in plain language: copay = flat fee, coinsurance = percentage, deductible = amount paid first.
- If the estimate has warnings or complete=false, say which items could not be priced.
Keep replies short."""

COLLECT = """STEP 1 - identify the patient. The app already greeted them and asked for their name. Collect, conversationally and one or two at a time: first name, last name, \
insurance member ID, date of birth, and insurance company. Call record_patient_info each time you learn something. \
Patients always give their date of birth as MM/DD/YYYY (US, month first): never ask which format, and never ask them to confirm day versus month. If the patient sends a photo of their insurance card, read it yourself: record the first name, last name, member ID \
and insurance company you can read with record_patient_info (date of birth only if printed), then ask only for what \
is still missing, usually just the date of birth. If a field is blurry or unsure, ask them to confirm it. Photos are deleted right after you read them, so you will not see an earlier photo again: that is expected. \
Never retract or doubt details you already recorded from a photo. If the photo \
is unreadable, say so kindly and ask them to type the details. Do NOT ask why they are visiting; the app shows a menu for that right after. When nothing is missing, say one short \
line thanking them (the insurance check is already running in the background, never tell them to wait) and stop."""

ASSIST = """You help a patient work out which kind of visit fits their situation. Ask one to three short, \
friendly questions (what their doctor said, symptoms they want checked). When it is clear, call set_visit_reason \
with the best match from: {bundles}, then say in one sentence which visit type you picked and that the app will \
show the estimate. Never ask for personal details, never quote prices, never give medical advice."""

CHAT = """The patient picked their reason for visit ({reason}) and the app already showed them the estimate for each \
clinic and a side-by-side comparison. Answer follow-up questions about it using only the figures in the conversation. \
If they chose "not sure", ask a few questions, then call set_visit_reason with one of: {bundles}, then call \
get_estimate. Always end a freshly computed estimate with the disclaimer."""


class ClaudeLLM:
    def __init__(self):
        from anthropic import AsyncAnthropic
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set (add it to .env)")
        self.client = AsyncAnthropic()
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

    async def complete(self, system, messages, tools):
        return await self.client.messages.create(model=self.model, max_tokens=1024, system=system,
                                                 messages=messages, tools=tools)


class OpenRouterLLM:
    """OpenAI-style chat/tool-calling via OpenRouter; translates to/from the Claude-style blocks the Agent uses."""
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self):
        self.key = os.getenv("OPENROUTER_API_KEY")
        if not self.key:
            raise RuntimeError("OPENROUTER_API_KEY is not set (add it to .env)")
        self.model = os.getenv("OPENROUTER_MODEL") or os.getenv("LLM_MODEL", "deepseek/deepseek-v4.1-flash")

    @staticmethod
    def _convert(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
            elif m["role"] == "user" and any(b.get("type") == "text" for b in c) and not any(b.get("type") == "tool_result" for b in c):
                out.append({"role": "user", "content": "\n".join(b["text"] for b in c if b.get("type") == "text")})
            elif m["role"] == "assistant":
                text = "\n".join(b["text"] for b in c if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                         for b in c if b["type"] == "tool_use"]
                msg = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:  # user turn carrying tool results
                for b in c:
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
        return out

    async def complete(self, system, messages, tools):
        import httpx
        from types import SimpleNamespace as NS
        body = {"model": self.model, "max_tokens": 1024, "messages": self._convert(system, messages),
                "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                            "parameters": t["input_schema"]}} for t in tools]}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(self.URL, json=body, headers={"Authorization": f"Bearer {self.key}"})
        if r.status_code != 200:
            raise RuntimeError(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
        msg = r.json()["choices"][0]["message"]
        content = []
        if msg.get("content"):
            content.append(NS(type="text", text=msg["content"]))
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append(NS(type="tool_use", id=tc["id"], name=tc["function"]["name"], input=args))
        return NS(content=content)


def make_llm():
    """LLM_PROVIDER=anthropic|openrouter wins; otherwise Anthropic if its key is set, else OpenRouter."""
    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider == "openrouter" or (not provider and not os.getenv("ANTHROPIC_API_KEY") and os.getenv("OPENROUTER_API_KEY")):
        return OpenRouterLLM()
    return ClaudeLLM()


def _blocks(resp) -> list[dict]:
    out = []
    for b in resp.content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


def _strip_images(history: list) -> None:
    """Card photos contain PHI and are costly to resend: keep only a text note once the turn is done."""
    for m in history:
        c = m.get("content")
        if m["role"] == "user" and isinstance(c, list) and any(b.get("type") == "image" for b in c):
            texts = [b for b in c if b.get("type") == "text"]
            m["content"] = [*texts, {"type": "text", "text": "[Insurance card photo shared, read, then deleted for "
                                                              "privacy. The details read from it were recorded.]"}]


class Agent:
    def __init__(self, svc: SessionService, llm=None):
        self.svc, self._llm = svc, llm

    @property
    def llm(self):
        if self._llm is None:
            self._llm = make_llm()
        return self._llm

    def _system(self, s: Session) -> str:
        menu = self.svc.store.settings.get("_menu_labels", {})
        reason = menu.get(s.option_id, s.option_id or "unknown")
        if s.phase == "assist":
            return SYSTEM.format(phase_text=ASSIST.format(bundles=", ".join(sorted(self.svc.store.bundles))))
        text = COLLECT if s.phase == "collecting" else CHAT.format(
            reason=reason, bundles=", ".join(sorted(self.svc.store.bundles)))
        return SYSTEM.format(phase_text=text)

    async def respond(self, s: Session, text: str, images: list | None = None) -> str:
        """images: [(media_type, base64_str)]. They are shown to the model for this turn only, then dropped from history."""
        if images:
            from .cardvision import image_block
            blocks = [image_block(mt, b64) for mt, b64 in images]
            blocks.append({"type": "text", "text": text or "Here is a photo of my insurance card."})
            s.history.append({"role": "user", "content": blocks})
        else:
            s.history.append({"role": "user", "content": text})
        try:
            return await self._loop(s)
        finally:
            _strip_images(s.history)

    async def _loop(self, s: Session) -> str:
        for _ in range(6):
            resp = await self.llm.complete(self._system(s), s.history, TOOL_SPECS)
            blocks = _blocks(resp)
            s.history.append({"role": "assistant", "content": blocks})
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                return "\n".join(b["text"] for b in blocks if b["type"] == "text")
            results = []
            for c in calls:
                r = await run_tool(self.svc, s, c["name"], c["input"])
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(r)})
            s.history.append({"role": "user", "content": results})
        return "Sorry, I couldn't finish that. Please try again."
