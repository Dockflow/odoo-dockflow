import copy
import json
import pathlib
import sys
from datetime import datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dockflow_tracking" / "lib"))

import dockflow_api  # noqa: E402
import mapping  # noqa: E402

FIXTURE = json.loads((ROOT / "dockflow_tracking/tests/fixtures/upstream_ocean_basic.json").read_text())
UAT_TOKEN = "uat_dockflow-uat_" + "a" * 43
PROD_TOKEN = "prod_dockflow_" + "b" * 43


class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def test_parse_datetime_converts_offsets_to_naive_utc():
    assert mapping.parse_datetime("2025-06-06T15:30:00-07:00") == datetime(2025, 6, 6, 22, 30)
    assert mapping.parse_datetime("2026-11-08T19:04:36Z") == datetime(2026, 11, 8, 19, 4, 36)
    assert mapping.parse_datetime(None) is None


def test_shipment_values_from_uat_fixture():
    values, containers, events = mapping.shipment_values(FIXTURE["data"])
    assert values["port_of_loading"] == "BEANR"
    assert values["port_of_discharge"] == "USLAX"
    assert values["etd"] == datetime(2026, 11, 8, 19, 4, 36)
    assert values["etd_is_actual"] is True
    assert values["eta"] == datetime(2026, 12, 9, 19, 4, 36)
    assert values["eta_is_actual"] is False
    assert values["vessel"] is False
    assert [c["reference"] for c in containers] == ["MSCU1234565"]
    assert containers[0]["latest_milestone"] == "VD"
    assert [e["code"] for e in events] == ["GE", "GN", "AE", "VD", "VA", "UV", "GT", "GR"]
    assert [e["is_actual"] for e in events] == [True] * 4 + [False] * 4


def test_split_shipment_voyage_from_first_split_and_containers_from_all():
    first = copy.deepcopy(FIXTURE["data"][0])
    second = copy.deepcopy(first)
    second["shipment"]["split_shipment_number"] = 2
    second["shipment"]["port_of_loading"] = "NLRTM"
    second["containers"][0]["reference"] = "TGHU1234569"
    second["events"] = [dict(second["events"][0], container_reference="TGHU1234569")]
    values, containers, events = mapping.shipment_values([second, first])
    assert values["port_of_loading"] == "BEANR"
    assert [c["reference"] for c in containers] == ["MSCU1234565", "TGHU1234569"]
    assert len(events) == 9


def test_downstream_payload_without_and_with_mock():
    plain = mapping.downstream_payload("P00012-1", "bill_of_lading", " MEDU123 ", "P00012")
    assert plain == {
        "tradeflow_reference": "P00012-1",
        "bill_of_lading_reference": ["MEDU123"],
        "bill_of_lading_references_complete": True,
        "custom_references": {"odoo_purchase_order": "P00012"},
        "active": True,
    }
    mock = mapping.downstream_payload("P00012-1", "container", "MSCU1234565", "P00012", "ocean_basic")
    assert mock["mock_tracking"] == {"scenario": "ocean_basic"}
    assert (mock["port_of_loading"], mock["port_of_discharge"]) == ("BEANR", "USLAX")


def test_client_refuses_a_token_for_the_other_environment():
    with pytest.raises(dockflow_api.DockflowApiError, match="prod token"):
        dockflow_api.DockflowClient(PROD_TOKEN, "uat")
    with pytest.raises(dockflow_api.DockflowApiError, match="not a Dockflow v3 token"):
        dockflow_api.DockflowClient("abc", "prod")


def test_send_requires_202_and_batches_by_100():
    session = FakeSession([FakeResponse(202), FakeResponse(202)])
    client = dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=session)
    client.send_tradeflows([{"tradeflow_reference": str(i)} for i in range(150)])
    assert [len(call[2]["json"]) for call in session.calls] == [100, 50]
    assert session.calls[0][1] == "https://uat.dockflow.com/v3/downstream/tradeflows"
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer " + UAT_TOKEN

    failing = dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=FakeSession([FakeResponse(422, text="bad")]))
    with pytest.raises(dockflow_api.DockflowApiError, match="HTTP 422: bad"):
        failing.send_tradeflows([{"tradeflow_reference": "x"}])


def test_deactivate_accepts_404():
    session = FakeSession([FakeResponse(404)])
    dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=session).deactivate_tradeflow("P0001/1")
    assert session.calls[0][1].endswith("/downstream/tradeflows/P0001%2F1")


def test_check_connection_does_not_use_failsafe():
    session = FakeSession([FakeResponse(200, {"data": [], "next_cursor": None})])
    dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=session).check_connection()
    assert "failsafe" not in session.calls[0][2]["params"]

    refused = dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=FakeSession([FakeResponse(401, text="no")]))
    with pytest.raises(dockflow_api.DockflowApiError, match="HTTP 401"):
        refused.check_connection()


def test_poll_follows_cursor_until_null():
    session = FakeSession([
        FakeResponse(200, {"data": [{"tradeflow_reference": "a"}], "next_cursor": "c1"}),
        FakeResponse(200, {"data": [{"tradeflow_reference": "b"}], "next_cursor": None}),
    ])
    records = dockflow_api.DockflowClient(UAT_TOKEN, "uat", session=session).poll_updates(since="2026-11-01T00:00:00Z")
    assert [r["tradeflow_reference"] for r in records] == ["a", "b"]
    assert session.calls[0][2]["params"] == {"failsafe": 1, "per_page": 1000, "since": "2026-11-01T00:00:00Z"}
    assert session.calls[1][2]["params"]["cursor"] == "c1"
