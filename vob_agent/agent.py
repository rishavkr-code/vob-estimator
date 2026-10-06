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
Do NOT ask why they are visiting; the app shows a menu for that right after. When nothing is missing, say one short \
line thanking them (the insurance check is already running in the background, never tell them to wait) and stop."""

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
        self.model = os.getenv("LLM_MODEL", "claude-sonnet-5-5")

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
        self.model = os.getenv("LLM_MODEL", "deepseek/deepseek-v4.1-flash")

    @staticmethod
    def _convert(system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
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
    return OpenRouterLLM() if os.getenv("OPENROUTER_API_KEY") else ClaudeLLM()


def _blocks(resp) -> list[dict]:
    out = []
    for b in resp.content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


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
        text = COLLECT if s.phase == "collecting" else CHAT.format(
            reason=reason, bundles=", ".join(sorted(self.svc.store.bundles)))
        return SYSTEM.format(phase_text=text)

    async def respond(self, s: Session, text: str) -> str:
        s.history.append({"role": "user", "content": text})
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
