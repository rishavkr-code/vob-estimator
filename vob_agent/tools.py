"""Tools exposed to the LLM. They return PHI-free results (no names, member IDs or DOB)."""
from .session import Session, SessionService, normalize_dob

TOOL_SPECS = [
    {"name": "record_patient_info",
     "description": "Save any patient details the user has given so far. Call whenever the user supplies "
                    "a field; pass only the fields you have. Returns which fields are still missing. "
                    "Eligibility is fetched in the background as soon as everything is collected.",
     "input_schema": {"type": "object", "properties": {
         "first_name": {"type": "string"}, "last_name": {"type": "string"},
         "member_id": {"type": "string"},
         "date_of_birth": {"type": "string", "description": "YYYY-MM-DD or MM/DD/YYYY"},
         "payer_name": {"type": "string", "description": "Insurance company name as the patient said it"}}}},
    {"name": "set_visit_reason",
     "description": "Only for patients who chose 'I'm not sure'. Set the treatment bundle once their "
                    "situation is clear. Valid ids are listed in the system prompt.",
     "input_schema": {"type": "object", "properties": {"bundle_id": {"type": "string"}},
                      "required": ["bundle_id"]}},
    {"name": "get_estimate",
     "description": "Compute the out-of-pocket estimate once all details are collected. Waits for the "
                    "insurance check to finish. The structured result is shown to the patient by the app; "
                    "summarise it, never recompute or alter numbers.",
     "input_schema": {"type": "object", "properties": {}}},
]


async def run_tool(svc: SessionService, s: Session, name: str, args: dict) -> dict:
    if name == "record_patient_info":
        issues = []
        for k in ("first_name", "last_name", "member_id"):
            if args.get(k):
                s.patient[k] = args[k].strip()
        if args.get("date_of_birth"):
            dob = normalize_dob(args["date_of_birth"])
            if dob:
                s.patient["date_of_birth"] = dob
            else:
                issues.append("date_of_birth not recognised or in the future; ask again")
        if args.get("payer_name"):
            matches = svc.store.resolve_payer(args["payer_name"])
            if len(matches) == 1:
                s.patient["payer"] = matches[0].name
                s.payer_tp_id = matches[0].trading_partner_id
            elif not matches:
                issues.append("payer not supported yet; supported: " +
                              ", ".join(p.name for p in svc.store.payers))
            else:
                issues.append("payer ambiguous: " + ", ".join(p.name for p in matches))
        svc.maybe_start_eligibility(s)
        return {"missing": s.missing(), "issues": issues, "insurance_check_started": s.eligibility_task is not None}
    if name == "set_visit_reason":
        if args.get("bundle_id") in svc.store.bundles:
            s.bundle_id = args["bundle_id"]
            svc.maybe_start_eligibility(s)
            return {"ok": True, "missing": s.missing()}
        return {"ok": False, "valid_ids": sorted(svc.store.bundles)}
    if name == "get_estimate":
        return await svc.run_estimate(s)
    return {"error": f"unknown tool {name}"}
