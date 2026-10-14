from kbc_access.identity import principal_from_headers
from kbc_access.web import build_guard


def test_seeded_runtime_gate_and_guard(runtime, headers_for):
    petr = principal_from_headers(headers_for("petr.sales@pharma-a.example"), runtime.settings.identity).principal
    assert runtime.gate.check(petr, "sales").allowed
    assert not runtime.gate.check(petr, "admin").allowed
    guard = build_guard(runtime, "sales")
    petr = petr.with_attributes(runtime.store.attributes_for(petr.email))
    result = guard.run(petr, "select distinct company from drug_sales where visibility = 'private'")
    assert result.error is None
    assert {r[0] for r in result.result.rows} == {"PHARMA_A"}
