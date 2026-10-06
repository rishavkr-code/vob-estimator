# VOB Estimate API: Contract v1

For the mobile app (`vob-patient-mobile`). Backend repo: `whoosh-labs/vob-estimate-agent`.
Prototype base URL: `https://vob-estimator.onrender.com`

**Status:** prototype, test patients only. No HIPAA agreements are in place with the host or the LLM provider.
The backend prices the estimate. The app collects input, calls these endpoints and renders the result.
The app's local engine and the `fee-schedule` endpoint are **not used** (see "Changes needed in the app").

---

## 1. Conventions

| Topic | Rule |
|---|---|
| Transport | HTTPS, JSON, UTF-8. `Accept: application/json`. |
| Session | `POST /v1/patient-sessions` returns `sessionId`. Send it as header `X-Session-Id` on every other call except `GET /v1/payers`. |
| Session lifetime | Expires after **15 minutes idle**. Every call resets the timer. An expired or unknown session returns **401**; the app should clear its state and start again. |
| Money | **Integer cents** (`50000` = $500.00). Never floats. |
| Percentages | Basis points (`3000` = 30%). |
| Dates | ISO `YYYY-MM-DD`. Must be in the past for date of birth. |
| PHI | Request bodies only. Never in URLs. The backend does not log bodies. Responses never echo name, member ID or date of birth. |
| Billing codes | CPT codes are never returned in patient-facing fields. |
| Errors | HTTP status plus `{"detail": "message"}`. See section 4. |
| Cold start | The prototype host sleeps after 15 idle minutes. The first call can take about 50 seconds. Show a loading state; use a longer timeout (60s) for the first request. |
| CORS | Open (`*`). Not relevant for native apps. |

### Call order

```
POST /v1/patient-sessions                 -> sessionId
GET  /v1/payers                           -> payer picker
POST /v1/patient-sessions/card-ocr        -> fields read from the card photo (or unreadable: type details)
POST /v1/patient-sessions/eligibility     -> status + benefits   (call as soon as the card is confirmed)
POST /v1/patient-sessions/providers       -> clinics near the patient
POST /v1/patient-sessions/estimate        -> priced estimate per clinic + comparison
POST /v1/patient-sessions/assistant       -> optional: free text -> intentId ("I'm not sure")
DELETE /v1/patient-sessions/{id}          -> "Start over"
```

`estimate` needs a successful `eligibility` in the same session (otherwise **409**).

---

## 2. Endpoints

### 2.1 Create session

`POST /v1/patient-sessions`  (no session header, no body)

Rate limit: 30 sessions per IP per hour (429).

Response `200`:
```json
{
  "sessionId": "f694d56e13344a41a771a19237a01136",
  "expiresAt": "2026-10-06T04:47:38.744812+00:00"
}
```

### 2.2 List payers

`GET /v1/payers?specialty=allergy`  (no session header)

Matches the app's `payerSchema`. Cigna is the only supported payer in the prototype. Others are listed with
`supported: false` so the app can show "not supported yet". `phone` is not provided yet (optional in the schema).

Response `200`:
```json
[
  {
    "id": "62308",
    "name": "Cigna",
    "supported": true
  },
  {
    "id": "unsupported-aetna",
    "name": "Aetna",
    "supported": false
  },
  {
    "id": "unsupported-blue-cross-blue-shield",
    "name": "Blue Cross Blue Shield",
    "supported": false
  },
  {
    "id": "unsupported-humana",
    "name": "Humana",
    "supported": false
  },
  {
    "id": "unsupported-kaiser-permanente",
    "name": "Kaiser Permanente",
    "supported": false
  },
  {
    "id": "unsupported-medicare",
    "name": "Medicare",
    "supported": false
  },
  {
    "id": "unsupported-unitedhealthcare",
    "name": "UnitedHealthcare",
    "supported": false
  }
]
```

The supported payer's `id` (`62308`) is what the app must send as `payerId` in eligibility.

### 2.3 Card photo reading

`POST /v1/patient-sessions/card-ocr`  header `X-Session-Id`  **multipart/form-data** with optional file parts `front` and `back`
(**JPEG, PNG, WebP, GIF, HEIC or PDF**, up to 6 MB each, at most 2 files; the type is checked from the file bytes, not the extension).

The backend reads the photo(s) with Claude vision. HEIC (iPhone) photos are converted to JPEG in memory, upright and capped at 2000 px; PDFs are read directly. Images are processed in memory only: they are not stored, logged or kept in the session.
No separate OCR service is used.

Response `200` (matches the app's `OcrResult`):
```json
{
  "status": "ok",
  "fields": {
    "firstName": "Jane",
    "lastName": "Doe",
    "memberId": "U1234567890",
    "groupNumber": "3344556",
    "payerName": "Cigna",
    "payerId": "62308"
  },
  "lowConfidenceFields": []
}
```

or, when nothing usable was read, not an insurance card, or vision is unavailable:
```json
{
  "status": "unreadable"
}
```

Notes:
- `fields` is partial. Only fields actually printed on the card are returned. **`dateOfBirth` is usually absent** (cards rarely print it), so the app must still ask for it.
- `payerId` is set only when the card's insurer matches a supported payer (e.g. Cigna becomes `62308`). Otherwise only `payerName` is returned.
- `lowConfidenceFields` lists fields that were blurry or unsure: show them highlighted for the patient to confirm.
- Card numbers that are not the member ID (group, RxBIN, RxPCN, phone numbers) are not returned as `memberId`. `groupNumber` is returned when printed.
- Errors: `413` image over 6 MB, `415` unsupported or corrupt file, `401` session, `429` more than 15 reads per IP per hour.
- The extraction is a best-effort reading. The patient must confirm every field on the confirm screen before eligibility runs.
- Privacy: card photos go to the LLM provider (Anthropic). No BAA is in place in this prototype, so use synthetic or consenting test cards only.

### 2.4 Eligibility (runs the 270/271)

`POST /v1/patient-sessions/eligibility`  header `X-Session-Id`

Request:
```json
{
  "payerId": "62308",
  "payerName": "Cigna",
  "memberId": "ABC123456",
  "groupNumber": "",
  "firstName": "Jane",
  "lastName": "Doe",
  "dateOfBirth": "1985-03-15"
}
```

| Field | Notes |
|---|---|
| `payerId` | From `GET /v1/payers`. Unsupported id returns `payer_not_supported`. |
| `memberId` | Without any card-issuer prefix. |
| `groupNumber` | Accepted, currently unused. |
| `dateOfBirth` | `YYYY-MM-DD`, past date, else **422**. |

Behaviour: one batched Stedi lookup, then cached for the session. Calling again with the **same** details returns the
same result with no new Stedi call. Changing member ID, name, DOB or payer triggers a new lookup.
Limit: 3 lookups per session and 20 per IP per hour (429). The app's 20s timeout is respected: the server
answers within 18s or returns 504.

Responses `200` (matches the app's `EligibilityResult`):

| `status` | Extra fields | Meaning |
|---|---|---|
| `ok` | `benefits` (below) | Active coverage |
| `inactive` | `payerName` | Coverage found but not active |
| `member_not_found` | none | Payer rejected the ID, name or date of birth |
| `payer_not_supported` | `payerName` | `payerId` is not a supported payer |

`ok` example (`benefits` matches the app's `planBenefitsSchema`; amounts in cents, in-network, individual level):
```json
{
  "status": "ok",
  "benefits": {
    "coverageStatus": "active",
    "payerName": "Cigna",
    "deductible": {
      "totalCents": 70000,
      "remainingCents": 67900
    },
    "outOfPocket": {
      "totalCents": 400000,
      "remainingCents": 300000
    },
    "defaultCoinsuranceBps": 2000,
    "rules": {
      "office_visit": {
        "kind": "copay",
        "copayCents": 4000
      },
      "allergy_testing": {
        "kind": "coinsurance",
        "coinsuranceBps": 2000,
        "deductibleApplies": true
      },
      "...more categories": "same coinsurance shape"
    },
    "accumulators": {
      "copayCountsTowardDeductible": false,
      "deductibleAppliesToCopayServices": false,
      "copayCountsTowardOutOfPocket": true
    },
    "specialistCopayCents": 4000
  }
}
```

Notes on `benefits`:
- `deductible` / `outOfPocket` are `null` if the plan reports none.
- `defaultCoinsuranceBps` is the plan's coinsurance. `specialistCopayCents` is the specialist office-visit copay.
- `rules` is populated for the 7 benefit categories; the backend prices from its own rules, so treat `benefits` as display data (deductible bar, "plan details").
- `planName` is not provided.
- The same benefits apply to every clinic in this prototype (one lookup, not one per clinic).

Errors: `502` insurer/Stedi failure (offer Retry), `504` insurer too slow, `429` too many checks, `422` bad date.

### 2.5 Providers

`POST /v1/patient-sessions/providers`  header `X-Session-Id`

Request (`near` is one of two shapes):
```json
{
  "specialty": "allergy_immunology",
  "tier": 1,
  "near": {
    "kind": "zip",
    "zip": "94025"
  }
}
```
```json
{
  "specialty": "allergy_immunology",
  "tier": 1,
  "near": {
    "kind": "coords",
    "lat": 37.45,
    "lng": -122.18
  }
}
```

Response `200`: array matching the app's `providerSchema`, sorted by distance. Empty array if the ZIP is unknown (use the app's empty state).
```json
[
  {
    "npi": "1871550590",
    "name": "ALLERGY & ASTHMA MEDICAL GROUP OF THE BAY AREA INC",
    "addressLine": "370 N Wiget Ln Ste 210",
    "city": "Walnut Creek",
    "state": "CA",
    "zip": "94598",
    "phone": "925-935-6252",
    "tier": 1,
    "distanceMiles": 34.3
  }
]
```

Notes:
- Returns only clinics that have a fee schedule for the patient's payer.
- Address, phone and distance are filled from backend config for the two prototype clinics (public listings, **not yet verified**). `distanceMiles` is measured from the patient's ZIP (or coordinates) to the clinic's ZIP centroid, so it is approximate. Values entered later in the clinics sheet take precedence.
- `near.zip` is US, first 5 digits used.

### 2.6 Estimate (new)

`POST /v1/patient-sessions/estimate`  header `X-Session-Id`. Requires a prior `eligibility` with `status: ok`.

Request:
```json
{
  "intentId": "allergy_testing_new",
  "npis": [
    "1871550590",
    "1609834373"
  ]
}
```

| Field | Notes |
|---|---|
| `intentId` | One of the ids in section 3. Unknown id returns **422**. |
| `drugId` | Optional, accepted and **ignored** for now (biologic bundle uses one default drug line). |
| `npis` | Clinic NPIs from the providers call. Duplicates ignored. An NPI with no fee schedule returns a `not_estimable` entry. |

Response `200`:
```json
{
  "status": "ok",
  "dateOfService": "2026-10-06",
  "intentId": "allergy_testing_new",
  "clinics": [
    {
      "kind": "estimate",
      "npi": "1871550590",
      "name": "ALLERGY & ASTHMA MEDICAL GROUP OF THE BAY AR...",
      "totalLowCents": 38530,
      "totalHighCents": 72052,
      "confidence": "high",
      "confidenceReasons": [],
      "lines": [
        {
          "name": "Office visit (new patient)",
          "costType": "copay",
          "estimable": true,
          "lowCents": 4000,
          "highCents": 4000,
          "unitsLow": "1",
          "unitsHigh": "1",
          "note": ""
        },
        {
          "name": "Allergy skin test",
          "costType": "deductible",
          "estimable": true,
          "lowCents": 22860,
          "highCents": 45720,
          "unitsLow": "30",
          "unitsHigh": "60",
          "note": ""
        },
        {
          "...": "remaining lines omitted"
        }
      ],
      "deductible": {
        "totalCents": 70000,
        "beforeCents": 67900,
        "afterLowCents": 33370,
        "afterHighCents": 0
      },
      "outOfPocket": {
        "totalCents": 400000,
        "beforeCents": 300000,
        "afterLowCents": 261470,
        "afterHighCents": 227948
      },
      "assumptions": [],
      "warnings": []
    },
    {
      "kind": "estimate",
      "npi": "1609834373",
      "name": "ALLERGY ASTHMA CLINIC LTD",
      "totalLowCents": 22552,
      "totalHighCents": 41072,
      "...": "same shape as above"
    },
    {
      "kind": "not_estimable",
      "npi": "9999999999",
      "reason": "out_of_network"
    }
  ],
  "comparison": {
    "clinicNpis": [
      "1871550590",
      "1609834373"
    ],
    "rows": [
      {
        "name": "Office visit (new patient)",
        "cells": [
          {
            "lowCents": 4000,
            "highCents": 4000
          },
          {
            "lowCents": 4000,
            "highCents": 4000
          }
        ]
      },
      {
        "name": "Allergy skin test",
        "cells": [
          {
            "lowCents": 22860,
            "highCents": 45720
          },
          {
            "lowCents": 11100,
            "highCents": 22200
          }
        ]
      },
      {
        "...": "one row per line"
      }
    ],
    "totals": [
      {
        "lowCents": 38530,
        "highCents": 72052,
        "complete": true
      },
      {
        "lowCents": 22552,
        "highCents": 41072,
        "complete": true
      }
    ],
    "cheapestNpi": "1609834373",
    "versusNpi": "1871550590",
    "savingsCents": 23479
  },
  "disclaimer": "This is an estimate based on your current plan benefits. Actual cost may vary based on services rendered."
}
```

Field reference:

| Field | Type | Notes |
|---|---|---|
| `status` | `"ok"` \| `"not_estimable"` | `not_estimable` with `reason: "coverage_inactive"` when the plan is not active |
| `clinics[].kind` | `"estimate"` \| `"not_estimable"` | Not-estimable clinics carry `reason: "out_of_network"` |
| `totalLowCents`, `totalHighCents` | int | Low uses minimum units, high uses maximum units |
| `confidence` | `high` \| `medium` \| `low` | `low` when any line has no price |
| `confidenceReasons[]` | enum | `price_unavailable`, `benefit_rule_assumed`, `multi_visit_course`, `no_out_of_pocket_max` |
| `lines[].costType` | enum | `copay`, `deductible`, `deductible_then_coinsurance`, `coinsurance`, `not_covered`, `price_unavailable` |
| `lines[].estimable` | bool | If `false`, `lowCents`/`highCents` are `null` and the line is excluded from the totals |
| `lines[].unitsLow/High` | string | Display text such as `"30"` or `"1 x 26 visits"` |
| `lines[].note` | string | Plain-language explanation, may be empty. Show it when present |
| `deductible`, `outOfPocket` | object or null | `beforeCents` now, `afterLowCents`/`afterHighCents` after the visit |
| `assumptions[]` | string[] | e.g. "No separate benefit reported ... general 30% coinsurance after deductible is assumed". Show under the estimate |
| `warnings[]` | string[] | Lines that could not be priced |
| `comparison` | object or null | Present when 2+ clinics were priced. `cheapestNpi`/`versusNpi`/`savingsCents` are `null` unless every line was priced at 2+ clinics. Savings compare range midpoints. `rows[].cells` is aligned with `clinicNpis`; a `null` cell means no price |
| `disclaimer` | string | Show verbatim near the total |

Display rule: show the headline as `totalLowCents` to `totalHighCents`; if equal, show a single amount.
Line items show cents; the spec's rounding rule for headline ranges is the app's choice.

### 2.7 Assistant (optional)

`POST /v1/patient-sessions/assistant`  header `X-Session-Id`  (for "I'm not sure" / free text)

Request: ```json
{
  "text": "My doctor wants me to start allergy shots"
}
```  (max 1000 chars)

Response `200`: ```json
{
  "reply": "That sounds like starting allergy shots. I'll show you an estimate for that.",
  "intentId": "starting_shots"
}
```

`intentId` is `null` until the assistant has decided; keep sending the patient's replies in the same session.
When `intentId` is set, call `estimate` with it. Limit: 60 calls per IP per hour. Uses an LLM, so allow up to about 15 seconds.

### 2.8 End session

`DELETE /v1/patient-sessions/{sessionId}`  header `X-Session-Id` (must equal the path id, else 401)

Response `204` (no body). Call it on "Start over" and on idle timeout. The server also drops sessions after 15 idle minutes.

---

## 3. Intent ids

Send these as `intentId`. They are the ids already used in the app's `treatment-bundles.json`.

| `intentId` | Backend bundle |
|---|---|
| `allergy_testing_new` | New-patient visit, skin and intradermal tests, breathing test |
| `allergy_testing_established` | Established-patient visit and skin tests |
| `starting_shots` | Serum preparation plus 26 weekly injection visits (a course) |
| `regular_shot` | One allergy shot visit |
| `rapid_desensitization` | Visit, testing, serum, rapid desensitization, injections |
| `breathing_test` | Visit plus spirometry codes |
| `biologic_start` | Visit, administration, one default drug line |
| `follow_up` | Follow-up visit |
| `not_sure` | Same as `allergy_testing_new` |

Unit ranges and codes live in the backend sheets and are **not yet clinically validated**.

---

## 4. Errors

| HTTP | When | App behaviour |
|---|---|---|
| 401 | Missing, unknown or expired `X-Session-Id` | Clear state, create a new session |
| 409 | `estimate` before a successful eligibility | Run eligibility first |
| 422 | Invalid body (bad date, unknown `intentId`, bad `near`) | Fix the input. FastAPI validation errors return `detail` as an array |
| 429 | Rate limit | Ask the patient to try again later |
| 502 | Insurer or Stedi failure | Show Retry |
| 504 | Insurer too slow | Show Retry |

Insurance outcomes (`inactive`, `member_not_found`, `payer_not_supported`) are **200 responses**, not errors.

---

## 5. TypeScript schema for the new `estimate` response

Drop into `src/api/types.ts` (uses the app's existing `cents` helper).

```ts
const accumulator = z.object({
  totalCents: cents, beforeCents: cents, afterLowCents: cents, afterHighCents: cents,
}).nullable();

const estimateLine = z.object({
  name: z.string(),
  costType: z.enum(['copay', 'deductible', 'deductible_then_coinsurance', 'coinsurance',
                    'not_covered', 'price_unavailable']),
  estimable: z.boolean(),
  lowCents: cents.nullable(),
  highCents: cents.nullable(),
  unitsLow: z.string(),
  unitsHigh: z.string(),
  note: z.string(),
});

const clinicEstimate = z.discriminatedUnion('kind', [
  z.object({
    kind: z.literal('estimate'),
    npi: z.string(), name: z.string(),
    totalLowCents: cents, totalHighCents: cents,
    confidence: z.enum(['high', 'medium', 'low']),
    confidenceReasons: z.array(z.enum(['price_unavailable', 'benefit_rule_assumed',
                                       'multi_visit_course', 'no_out_of_pocket_max'])),
    lines: z.array(estimateLine),
    deductible: accumulator, outOfPocket: accumulator,
    assumptions: z.array(z.string()), warnings: z.array(z.string()),
  }),
  z.object({ kind: z.literal('not_estimable'), npi: z.string(), reason: z.literal('out_of_network') }),
]);

const comparison = z.object({
  clinicNpis: z.array(z.string()),
  rows: z.array(z.object({
    name: z.string(),
    cells: z.array(z.object({ lowCents: cents, highCents: cents }).nullable()),
  })),
  totals: z.array(z.object({ lowCents: cents, highCents: cents, complete: z.boolean() })),
  cheapestNpi: z.string().nullable(),
  versusNpi: z.string().nullable(),
  savingsCents: cents.nullable(),
}).nullable();

export const estimateResponseSchema = z.discriminatedUnion('status', [
  z.object({
    status: z.literal('ok'),
    dateOfService: z.string(), intentId: z.string(),
    clinics: z.array(clinicEstimate), comparison, disclaimer: z.string(),
  }),
  z.object({ status: z.literal('not_estimable'), reason: z.literal('coverage_inactive') }),
]);

export const assistantResponseSchema = z.object({ reply: z.string(), intentId: z.string().nullable() });
```

Add to `ApiClient`:

```ts
getEstimate(sessionId: string, req: { intentId: string; drugId?: string; npis: string[] }): Promise<EstimateResponse>;
askAssistant(sessionId: string, text: string): Promise<{ reply: string; intentId: string | null }>;
endSession(sessionId: string): Promise<void>;
```

---

## 6. Changes needed in the app

1. Replace `getFeeSchedule` + local `estimate()` in `useClinicEstimates` with one `getEstimate` call (all selected NPIs in one request).
2. Map `EstimateResponse` to the estimate and comparison screens. `ENABLE_PROVIDER_COMPARISON` can be switched on: the comparison is returned.
3. Call `endSession` on "Start over" and idle timeout.
4. Optional: the app's idle timer is 10 minutes (`SESSION.IDLE_TIMEOUT_MS`); the server allows 15.
5. Optional: wire the "I'm not sure" path to `askAssistant`.
6. `EXPO_PUBLIC_API_BASE_URL=https://vob-estimator.onrender.com`, `EXPO_PUBLIC_USE_MOCK_API=false`.

---

## 7. Known limitations (prototype)

| Item | State |
|---|---|
| Card photo reading | Claude vision (best effort, patient confirms). Test cards only until a BAA is in place |
| Clinic address, phone, distance | From unverified public listings; distance is ZIP-centroid based |
| Payers | Cigna only |
| Biologic drug choice | `drugId` ignored |
| Cheaper/costlier code ranges (e.g. 99213/99214) | Supported by the backend but needs the `cpt_max` column filled in the bundles sheet |
| Benefits | One lookup shared by all clinics |
| Allergy-test cost sharing | Cigna does not report a separate allergy-testing benefit, so the plan's general coinsurance after deductible is assumed (shown in `assumptions`) |
| Some fees | A few clinic fees are estimates copied from a peer clinic |
| Sessions | In memory; lost when the server restarts or sleeps |
| Data | Test patients only |

## 8. Postman

Import `docs/vob_estimate_api.postman_collection.json`. Run requests 0 to 8 in order; request 1 stores the `sessionId`.
