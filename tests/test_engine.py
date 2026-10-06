import asyncio
from vob_agent.data import DataStore
from vob_agent.engine import estimate_clinic
from vob_agent.parser271 import parse_271
from vob_agent.stedi import MockStedi, build_request

TP = "62308"
BAY, ACL = "1871550590", "1609834373"


def _pb(**kw):
    req = build_request("A", "B", "X1", "19800101", TP, BAY, "CLINIC", ["30", "98", "79", "BT"])
    return parse_271([asyncio.run(MockStedi(**kw).check(req))])


def test_parse_mock_271():
    pb = _pb()
    assert pb.active and pb.deductible_remaining == 679 and pb.oop_remaining == 3000
    assert pb.by_stc["98"].copay == 40 and pb.by_stc["79"].coinsurance == 0.2


def test_new_patient_allergy_testing_range():
    store = DataStore(force_local=True)
    est = estimate_clinic(store, "ALLERGY_TEST_NEW", _pb(), TP, BAY)
    assert est["complete"]
    assert est["total_low"] == 500.00 and est["total_high"] == 775.20  # hand-checked: 40 copay + deductible lines (+20% after $679)


def test_oop_cap_applies():
    store = DataStore(force_local=True)
    est = estimate_clinic(store, "ALLERGY_TEST_NEW", _pb(oop_remaining=100.0), TP, BAY)
    assert est["total_high"] == 100.0


def test_missing_fee_is_flagged_not_guessed():
    store = DataStore(force_local=True)
    del store.fees[(TP, ACL, "94375")]
    est = estimate_clinic(store, "BREATHING_TEST", _pb(), TP, ACL)
    assert not est["complete"] and any("94375" in l["cpt"] and not l["estimable"] for l in est["lines"])


def test_line_ranges_never_inverted():
    store = DataStore(force_local=True)
    est = estimate_clinic(store, "ALLERGY_TEST_NEW", _pb(), TP, BAY)
    assert all(l["low"] <= l["high"] for l in est["lines"] if l["estimable"])
