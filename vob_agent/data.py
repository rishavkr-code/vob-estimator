"""Reference data loader: Google Sheets (public CSV export) with local CSV fallback."""
import csv
import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = {
    "clinics": ["tenant_key", "npi", "org_name", "has_fee_data"],
    "trading_partners": ["trading_partner_id", "handler_key", "payer_name", "aliases", "max_stcs_per_call"],
    "bundles": ["bundle_id", "cpt", "units_min", "units_max", "repeat_visits", "sequence"],
    "cpt_catalog": ["cpt", "plain_name", "unit_basis", "stc_primary", "stc_fallbacks"],
    "fee_schedule": ["trading_partner_id", "npi", "cpt", "allowed_amount", "unit_basis"],
}


class DataSourceError(RuntimeError):
    pass


def load_settings() -> dict:
    return json.loads((ROOT / "config/settings.json").read_text())


def load_clinic_details() -> dict:
    path = ROOT / "config/clinic_details.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _zip_latlng(zip_code: str):
    try:
        import zipcodes
        m = zipcodes.matching((zip_code or "")[:5])
        return (float(m[0]["lat"]), float(m[0]["long"])) if m else (None, None)
    except Exception:
        return (None, None)


def load_menu() -> dict:
    return json.loads((ROOT / "config/menu.json").read_text())


@dataclass
class Clinic:
    npi: str
    name: str
    has_fee_data: bool
    # optional columns in the clinics sheet; empty until the sheet is filled in
    address_line: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    phone: str = ""
    lat: float | None = None
    lng: float | None = None


@dataclass
class Payer:
    trading_partner_id: str
    handler_key: str
    name: str
    aliases: list
    max_stcs_per_call: int


@dataclass
class CptInfo:
    cpt: str
    plain_name: str
    unit_basis: str
    stcs: list  # primary first, then fallbacks


@dataclass
class BundleLine:
    cpt: str
    units_min: float
    units_max: float
    repeat_visits: int
    sequence: int
    cpt_max: str = ""  # optional costlier code used only for the high scenario (e.g. 99214 for 99213)


@dataclass
class Fee:
    allowed: float
    unit_basis: str
    source: str


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class DataStore:
    def __init__(self, settings: dict | None = None, force_local: bool = False):
        self.settings = settings or load_settings()
        self.force_local = force_local
        self.loaded_at = 0.0
        self.last_error = None
        self.source = {}
        self.clinics: dict[str, Clinic] = {}
        self.payers: list[Payer] = []
        self.catalog: dict[str, CptInfo] = {}
        self.bundles: dict[str, list[BundleLine]] = {}
        self.fees: dict[tuple, Fee] = {}
        self.refresh()

    # ---- fetching -------------------------------------------------------
    def _fetch_rows(self, table: str) -> list[dict]:
        """Google Sheet is the source of truth. Local CSV is used only when force_local=True (tests)."""
        if self.force_local:
            path = ROOT / self.settings.get("data_source", {}).get("test_fixture_dir", "tests/fixtures") / f"{table}.csv"
            self.source[table] = "local_fixture"
            return list(csv.DictReader(path.open(newline="")))
        sheet_id = self.settings.get("data_source", {}).get("sheets", {}).get(table)
        if not sheet_id:
            raise DataSourceError(f"{table}: no sheet id configured in settings.json")
        try:
            r = httpx.get(f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv",
                          follow_redirects=True, timeout=20)
        except httpx.HTTPError as e:
            raise DataSourceError(f"{table}: could not reach Google Sheets ({type(e).__name__})") from e
        if r.status_code != 200 or r.text.lstrip().startswith("<"):
            raise DataSourceError(
                f"{table}: sheet {sheet_id} not readable (HTTP {r.status_code}). "
                "Share it as 'Anyone with the link can view'.")
        self.source[table] = "google_sheet"
        return list(csv.DictReader(io.StringIO(r.text)))

    def _validated(self, table: str) -> list[dict]:
        rows = self._fetch_rows(table)
        missing = [c for c in REQUIRED[table] if rows and c not in rows[0]]
        if not rows or missing:
            raise ValueError(f"{table}: empty or missing columns {missing}")
        return rows

    def refresh(self):
        """Reload all tables; if any table fails validation keep the previous good data."""
        rows = {t: self._validated(t) for t in REQUIRED}
        self.clinics = {
            r["npi"]: Clinic(r["npi"], r["org_name"], r["has_fee_data"].strip().lower() == "true",
                             (r.get("address_line") or "").strip(), (r.get("city") or "").strip(),
                             (r.get("state") or "").strip(), (r.get("zip") or "").strip(),
                             (r.get("phone") or "").strip(), _num(r.get("lat")), _num(r.get("lng")))
            for r in rows["clinics"]}
        self._apply_clinic_details()
        self.payers = [
            Payer(r["trading_partner_id"], r["handler_key"], r["payer_name"],
                  [a.strip().lower() for a in r["aliases"].split(";") if a.strip()],
                  int(r["max_stcs_per_call"] or 4))
            for r in rows["trading_partners"]]
        self.catalog = {
            r["cpt"]: CptInfo(r["cpt"], r["plain_name"], r["unit_basis"],
                              [r["stc_primary"]] + [s for s in r["stc_fallbacks"].split(";") if s])
            for r in rows["cpt_catalog"]}
        bundles: dict[str, list[BundleLine]] = {}
        for r in rows["bundles"]:
            bundles.setdefault(r["bundle_id"], []).append(
                BundleLine(r["cpt"], float(r["units_min"]), float(r["units_max"]),
                           int(r["repeat_visits"]), int(r["sequence"]), (r.get("cpt_max") or "").strip()))
        self.bundles = {k: sorted(v, key=lambda b: b.sequence) for k, v in bundles.items()}
        self.fees = {}
        for r in rows["fee_schedule"]:
            if not r["allowed_amount"].strip():
                continue
            self.fees[(r["trading_partner_id"], r["npi"], r["cpt"])] = Fee(
                float(r["allowed_amount"]), r["unit_basis"], r.get("source", ""))
        self.loaded_at = time.time()

    def _apply_clinic_details(self):
        """Fill gaps from config/clinic_details.json. Sheet values win; ZIP supplies coordinates."""
        details = load_clinic_details()
        for npi, c in self.clinics.items():
            d = details.get(npi, {})
            if d.get("name") and (not c.name or c.name.endswith("...")):
                c.name = d["name"]
            for f in ("address_line", "city", "state", "zip", "phone"):
                if not getattr(c, f) and d.get(f):
                    setattr(c, f, d[f])
            if c.lat is None or c.lng is None:
                c.lat, c.lng = _zip_latlng(c.zip)

    def maybe_refresh(self):
        """Refresh on TTL. On failure keep serving the last good copy (and record the error)."""
        ttl = self.settings.get("data_source", {}).get("refresh_seconds", 300)
        if time.time() - self.loaded_at > ttl:
            try:
                self.refresh()
            except DataSourceError as e:
                self.last_error = str(e)

    # ---- lookups --------------------------------------------------------
    def resolve_payer(self, text: str) -> list[Payer]:
        """Return matching payers (empty = no match, >1 = ambiguous)."""
        q = _norm(text)
        if not q:
            return []
        scope = set(self.settings.get("payers_in_scope", []))
        pool = [p for p in self.payers if not scope or p.trading_partner_id in scope]
        exact = [p for p in pool if q in {_norm(p.name), _norm(p.handler_key), p.trading_partner_id,
                                          *[_norm(a) for a in p.aliases]}]
        if exact:
            return exact
        return [p for p in pool if any(a and (a in q or q in a) for a in [_norm(p.name), *map(_norm, p.aliases)])]

    def priced_clinics(self, tp_id: str) -> list[Clinic]:
        """In-scope clinics that have at least one fee row for this payer."""
        scope = set(self.settings.get("clinics_in_scope", []))
        return [c for npi, c in self.clinics.items()
                if npi in scope and any(k[0] == tp_id and k[1] == npi for k in self.fees)]
