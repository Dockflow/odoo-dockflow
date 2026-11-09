import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command

from ..lib import dockflow_api, mapping

_logger = logging.getLogger(__name__)

PARAM = "dockflow_tracking."
ETA_SHIFT_TO_REPORT = timedelta(hours=24)
# The token sees every tradeflow of the company, so Odoo's references must not collide with those of other systems.
REFERENCE_PREFIX = "ODOO-"


class DockflowShipment(models.Model):
    _name = "dockflow.shipment"
    _description = "Dockflow shipment"
    _order = "id desc"

    name = fields.Char("Tradeflow reference", readonly=True, copy=False, index=True)
    purchase_order_id = fields.Many2one("purchase.order", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one(related="purchase_order_id.company_id", store=True)
    reference_type = fields.Selection(
        [("bill_of_lading", "Bill of lading"), ("booking", "Booking"), ("container", "Container")],
        required=True, default="bill_of_lading",
    )
    reference = fields.Char(required=True)
    state = fields.Selection(
        [
            ("draft", "Not shared"),
            ("sent", "Sent to Dockflow"),
            ("tracking", "Tracking"),
            ("error", "Error"),
            ("deactivated", "Deactivated"),
        ],
        default="draft", readonly=True, copy=False,
    )
    last_error = fields.Text(readonly=True, copy=False)
    last_updated = fields.Datetime("Last update from Dockflow", readonly=True, copy=False)
    carrier_name = fields.Char("Carrier", readonly=True, copy=False)
    vessel = fields.Char(readonly=True, copy=False)
    bill_of_lading = fields.Char(readonly=True, copy=False)
    booking = fields.Char(readonly=True, copy=False)
    port_of_loading = fields.Char("POL", readonly=True, copy=False)
    port_of_discharge = fields.Char("POD", readonly=True, copy=False)
    transshipment_ports = fields.Char(readonly=True, copy=False)
    etd = fields.Datetime("ETD", readonly=True, copy=False)
    etd_is_actual = fields.Boolean("Departed", readonly=True, copy=False)
    eta = fields.Datetime("ETA", readonly=True, copy=False)
    eta_is_actual = fields.Boolean("Arrived", readonly=True, copy=False)
    container_ids = fields.One2many("dockflow.container", "shipment_id", readonly=True, copy=False)
    event_ids = fields.One2many("dockflow.event", "shipment_id", readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        shipments = super().create(vals_list)
        for shipment in shipments:
            order = shipment.purchase_order_id
            taken = set(order.dockflow_shipment_ids.mapped("name"))
            number = 1
            while f"{REFERENCE_PREFIX}{order.name}-{number}" in taken:
                number += 1
            shipment.name = f"{REFERENCE_PREFIX}{order.name}-{number}"
        shipments.filtered(lambda s: s._sharing_enabled())._send()
        return shipments

    def write(self, vals):
        result = super().write(vals)
        if {"reference", "reference_type"} & set(vals):
            self.filtered(lambda s: s.state != "deactivated" and s._sharing_enabled())._send()
        return result

    def unlink(self):
        self.filtered(lambda s: s.state in ("sent", "tracking", "error"))._deactivate()
        return super().unlink()

    def _config(self):
        params = self.env["ir.config_parameter"].sudo()
        return {
            "opt_in": params.get_bool(PARAM + "opt_in"),
            "token": params.get_str(PARAM + "token"),
            "environment": params.get_str(PARAM + "environment") or "prod",
            "sync_receipt_date": params.get_bool(PARAM + "sync_receipt_date"),
            "mock_scenario": params.get_str(PARAM + "mock_scenario") or False,
        }

    def _sharing_enabled(self):
        config = self._config()
        return config["opt_in"] and bool(config["token"])

    def _client(self):
        config = self._config()
        return dockflow_api.DockflowClient(config["token"], config["environment"])

    def _send(self):
        if not self:
            return
        config = self._config()
        if not (config["opt_in"] and config["token"]):
            raise UserError(self.env._("Switch on sharing with Dockflow and enter an API token in the Purchase settings first."))
        scenario = config["mock_scenario"] if config["environment"] == "uat" else False
        payloads = [
            mapping.downstream_payload(s.name, s.reference_type, s.reference, s.purchase_order_id.name, scenario)
            for s in self
        ]
        try:
            self._client().send_tradeflows(payloads)
        except dockflow_api.DockflowApiError as exc:
            self.write({"state": "error", "last_error": str(exc)})
            return
        self.filtered(lambda s: s.state in ("draft", "error")).write({"state": "sent", "last_error": False})

    def _deactivate(self):
        if not self._sharing_enabled():
            return
        client = self._client()
        for shipment in self:
            try:
                client.deactivate_tradeflow(shipment.name)
            except dockflow_api.DockflowApiError as exc:
                _logger.warning("Dockflow: could not deactivate %s: %s", shipment.name, exc)
                continue
            shipment.state = "deactivated"

    def action_send(self):
        self._send()

    @api.model
    def _cron_sync(self):
        if not self._sharing_enabled():
            return
        self.search([("state", "in", ("draft", "error"))])._send()
        open_shipments = self.search([("state", "in", ("sent", "tracking"))])
        if not open_shipments:
            return
        params = self.env["ir.config_parameter"].sudo()
        since = None
        if not params.get_bool(PARAM + "polling_seeded"):
            since = (min(open_shipments.mapped("create_date")) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            records = self._client().poll_updates(since=since)
        except dockflow_api.DockflowApiError as exc:
            _logger.warning("Dockflow: polling failed: %s", exc)
            return
        params.set_bool(PARAM + "polling_seeded", True)
        self._apply_records(records)

    @api.model
    def _apply_records(self, records):
        grouped = mapping.group_by_tradeflow(records)
        if not grouped:
            return
        sync_receipt_date = self._config()["sync_receipt_date"]
        for shipment in self.search([("name", "in", list(grouped)), ("state", "!=", "deactivated")]):
            values, containers, events = mapping.shipment_values(grouped[shipment.name])
            if shipment.last_updated and values["last_updated"] and values["last_updated"] < shipment.last_updated:
                continue
            previous_eta = shipment.eta
            values["state"] = "tracking" if values.pop("is_active") else "deactivated"
            values["container_ids"] = [Command.clear()] + [Command.create(c) for c in containers]
            values["event_ids"] = [Command.clear()] + [Command.create(e) for e in events]
            shipment.write(values)
            order = shipment.purchase_order_id
            if previous_eta and shipment.eta and abs(shipment.eta - previous_eta) > ETA_SHIFT_TO_REPORT:
                order.message_post(body=self.env._(
                    "Dockflow: the ETA of %(shipment)s (%(reference)s) moved from %(old)s to %(new)s (UTC).",
                    shipment=shipment.name, reference=shipment.reference,
                    old=fields.Datetime.to_string(previous_eta), new=fields.Datetime.to_string(shipment.eta),
                ))
            if sync_receipt_date and shipment.eta:
                receipts = order.picking_ids.filtered(
                    lambda p: p.picking_type_code == "incoming" and p.state not in ("done", "cancel")
                )
                receipts.write({"scheduled_date": order.dockflow_eta})


class DockflowContainer(models.Model):
    _name = "dockflow.container"
    _description = "Dockflow container"
    _order = "reference"

    shipment_id = fields.Many2one("dockflow.shipment", required=True, ondelete="cascade", index=True)
    reference = fields.Char("Container", required=True)
    iso_type = fields.Char("Type")
    eta = fields.Datetime("ETA")
    eta_is_actual = fields.Boolean("Arrived")
    latest_milestone = fields.Char()
    latest_milestone_date = fields.Datetime()
    latest_milestone_location = fields.Char()


class DockflowEvent(models.Model):
    _name = "dockflow.event"
    _description = "Dockflow milestone"
    _order = "event_date, id"

    shipment_id = fields.Many2one("dockflow.shipment", required=True, ondelete="cascade", index=True)
    code = fields.Char()
    message = fields.Char("Milestone")
    event_date = fields.Datetime("Date")
    is_actual = fields.Boolean("Actual")
    location = fields.Char()
    container_reference = fields.Char("Container")
