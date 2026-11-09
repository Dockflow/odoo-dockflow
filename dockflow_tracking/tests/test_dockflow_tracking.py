import copy
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

CLIENT = "odoo.addons.dockflow_tracking.lib.dockflow_api.DockflowClient"
FIXTURE = json.loads((Path(__file__).parent / "fixtures/upstream_ocean_basic.json").read_text())
TOKEN = "uat_dockflow-uat_" + "a" * 43


@tagged("post_install", "-at_install")
class TestDockflowTracking(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "Supplier"})
        cls.product = cls.env["product.product"].create({"name": "Pallet", "type": "consu", "is_storable": True})
        cls.order = cls.env["purchase.order"].create({
            "partner_id": cls.partner.id,
            "order_line": [(0, 0, {"product_id": cls.product.id, "product_qty": 1, "price_unit": 10})],
        })
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_str("dockflow_tracking.token", TOKEN)
        cls.params.set_str("dockflow_tracking.environment", "uat")

    def _record(self, reference, **shipment):
        record = copy.deepcopy(FIXTURE["data"][0])
        record["tradeflow_reference"] = reference
        record["shipment"].update(shipment)
        return record

    def _shipment(self, reference="MEDU1234567"):
        return self.env["dockflow.shipment"].create({
            "purchase_order_id": self.order.id, "reference_type": "bill_of_lading", "reference": reference,
        })

    def test_nothing_is_sent_without_opt_in(self):
        with patch(CLIENT + ".send_tradeflows") as send:
            shipment = self._shipment()
        send.assert_not_called()
        self.assertEqual(shipment.state, "draft")
        self.assertEqual(shipment.name, "ODOO-" + self.order.name + "-1")

    def test_opt_in_sends_and_numbers_per_order(self):
        self.params.set_bool("dockflow_tracking.opt_in", True)
        with patch(CLIENT + ".send_tradeflows") as send:
            first = self._shipment()
            second = self._shipment("MEDU7654321")
        self.assertEqual(second.name, "ODOO-" + self.order.name + "-2")
        self.assertEqual((first.state, second.state), ("sent", "sent"))
        payload = send.call_args_list[0].args[0][0]
        self.assertEqual(payload["tradeflow_reference"], first.name)
        self.assertEqual(payload["bill_of_lading_reference"], ["MEDU1234567"])
        self.assertEqual(payload["custom_references"], {"odoo_purchase_order": self.order.name})
        self.assertNotIn("mock_tracking", payload)

    def test_refused_send_is_shown_on_the_shipment(self):
        self.params.set_bool("dockflow_tracking.opt_in", True)
        from odoo.addons.dockflow_tracking.lib.dockflow_api import DockflowApiError
        with patch(CLIENT + ".send_tradeflows", side_effect=DockflowApiError("Dockflow answered HTTP 422: bad", 422)):
            shipment = self._shipment()
        self.assertEqual(shipment.state, "error")
        self.assertIn("HTTP 422", shipment.last_error)

    def test_poll_applies_record_and_reports_eta_shift(self):
        self.params.set_bool("dockflow_tracking.opt_in", True)
        with patch(CLIENT + ".send_tradeflows"):
            shipment = self._shipment()
        unrelated = self._record("SOMEONE-ELSE")
        with patch(CLIENT + ".poll_updates", return_value=[self._record(shipment.name), unrelated]):
            self.env["dockflow.shipment"]._cron_sync()
        self.assertEqual(shipment.state, "tracking")
        self.assertEqual(shipment.eta, datetime(2026, 12, 9, 19, 4, 36))
        self.assertEqual(shipment.port_of_loading, "BEANR")
        self.assertEqual(len(shipment.event_ids), 8)
        self.assertEqual(shipment.container_ids.reference, "MSCU1234565")
        self.assertEqual(self.order.dockflow_eta, shipment.eta)

        later = self._record(shipment.name, arrival_event_date="2026-12-12T19:04:36+00:00")
        later["metadata"]["last_updated"] = "2026-11-10T08:00:00+00:00"
        messages_before = len(self.order.message_ids)
        with patch(CLIENT + ".poll_updates", return_value=[later]):
            self.env["dockflow.shipment"]._cron_sync()
        self.assertEqual(shipment.eta, datetime(2026, 12, 12, 19, 4, 36))
        self.assertEqual(len(self.order.message_ids), messages_before + 1)
        self.assertIn("moved from", self.order.message_ids[0].body)

        stale = self._record(shipment.name, arrival_event_date="2027-01-01T00:00:00+00:00")
        with patch(CLIENT + ".poll_updates", return_value=[stale]):
            self.env["dockflow.shipment"]._cron_sync()
        self.assertEqual(shipment.eta, datetime(2026, 12, 12, 19, 4, 36))

    def test_receipt_date_follows_eta_when_enabled(self):
        self.params.set_bool("dockflow_tracking.opt_in", True)
        self.params.set_bool("dockflow_tracking.sync_receipt_date", True)
        self.order.button_confirm()
        receipt = self.order.picking_ids
        self.assertTrue(receipt)
        with patch(CLIENT + ".send_tradeflows"):
            shipment = self._shipment()
        with patch(CLIENT + ".poll_updates", return_value=[self._record(shipment.name)]):
            self.env["dockflow.shipment"]._cron_sync()
        self.assertEqual(receipt.scheduled_date, datetime(2026, 12, 9, 19, 4, 36))

    def test_cancel_deactivates_on_dockflow(self):
        self.params.set_bool("dockflow_tracking.opt_in", True)
        with patch(CLIENT + ".send_tradeflows"):
            shipment = self._shipment()
        with patch(CLIENT + ".deactivate_tradeflow") as deactivate:
            self.order.button_cancel()
        deactivate.assert_called_once_with(shipment.name)
        self.assertEqual(shipment.state, "deactivated")
