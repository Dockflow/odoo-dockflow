import hashlib
import logging
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Command
from odoo.modules import module as odoo_module

from ..lib import dockflow_api, mapping

_logger = logging.getLogger(__name__)

PARAM = "dockflow_tracking."
ETA_SHIFT_TO_REPORT = timedelta(hours=24)
# The token sees every tradeflow of the company, so Odoo's references must not collide with those of other systems.
REFERENCE_PREFIX = "ODOO-"
LIVE_STATES = ("sent", "tracking", "error")


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
            order = shipment.purchase_order_id.sudo()
            # A number is never reused: Dockflow keeps a deactivated tradeflow, and reusing its name would revive it.
            number = order.dockflow_last_number
            while True:
                number += 1
                name = f"{REFERENCE_PREFIX}{order.name}-{number}"
                if not self.sudo().search_count([("name", "=", name)]):
                    break
            order.dockflow_last_number = number
            shipment.name = name
        shipments._sendable()._send()
        return shipments

    def write(self, vals):
        result = super().write(vals)
        if {"reference", "reference_type"} & set(vals):
            self.filtered(lambda s: s.state != "deactivated")._sendable()._send()
        return result

    def unlink(self):
        self.filtered(lambda s: s.state in LIVE_STATES)._deactivate()
        return super().unlink()

    def _config(self):
        params = self.env["ir.config_parameter"].sudo()
        return {
            "opt_in": params.get_param(PARAM + "opt_in") == "True",
            "token": params.get_param(PARAM + "token") or "",
            "environment": params.get_param(PARAM + "environment") or "prod",
            "sync_receipt_date": params.get_param(PARAM + "sync_receipt_date") == "True",
            "mock_scenario": params.get_param(PARAM + "mock_scenario") or False,
        }

    def _sharing_enabled(self):
        config = self._config()
        return config["opt_in"] and bool(config["token"])

    def _sendable(self):
        if not self._sharing_enabled():
            return self.browse()
        return self.filtered(lambda s: s.purchase_order_id.state != "cancel")

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
        """Best effort: a shipment that cannot be deactivated now is retried by the cron."""
        if not self or not self._sharing_enabled():
            return
        try:
            client = self._client()
        except dockflow_api.DockflowApiError as exc:
            _logger.warning("Dockflow: cannot deactivate %s: %s", ", ".join(self.mapped("name")), exc)
            return
        for shipment in self:
            try:
                client.deactivate_tradeflow(shipment.name)
            except dockflow_api.DockflowApiError as exc:
                _logger.warning("Dockflow: could not deactivate %s: %s", shipment.name, exc)
                continue
            shipment.state = "deactivated"

    def action_send(self):
        if self.filtered(lambda s: s.purchase_order_id.state == "cancel"):
            raise UserError(self.env._("A shipment of a cancelled purchase order cannot be sent."))
        self._send()

    def _commit(self):
        if not odoo_module.current_test:
            self.env.cr.commit()

    @api.model
    def _cron_sync(self):
        if not self._sharing_enabled():
            return
        config = self._config()
        params = self.env["ir.config_parameter"].sudo()
        # Another token or environment is another account with its own position: start over there.
        key = hashlib.sha256(f"{config['environment']}:{config['token']}".encode()).hexdigest()[:16]
        previous_key = params.get_param(PARAM + "polling_key")
        if previous_key != key:
            if previous_key:
                self.search([("state", "in", LIVE_STATES), ("purchase_order_id.state", "!=", "cancel")]).write(
                    {"state": "draft"}
                )
                params.set_param(PARAM + "polling_seeded", "False")
            params.set_param(PARAM + "polling_key", key)

        self.search([("state", "in", ("draft", "error"))])._sendable()._send()
        self.search([("state", "in", LIVE_STATES), ("purchase_order_id.state", "=", "cancel")])._deactivate()
        self._commit()

        open_shipments = self.search([("state", "in", ("sent", "tracking"))])
        if not open_shipments:
            return
        since = None
        if params.get_param(PARAM + "polling_seeded") != "True":
            since = (min(open_shipments.mapped("create_date")) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            # Dockflow moves its position on with every page, so each page is stored before the next is asked for.
            for page in self._client().iter_pages(since=since):
                self._apply_records(page)
                params.set_param(PARAM + "polling_seeded", "True")
                self._commit()
        except dockflow_api.DockflowApiError as exc:
            _logger.warning("Dockflow: polling failed: %s", exc)

    @api.model
    def _apply_records(self, records):
        grouped = mapping.group_by_tradeflow(records)
        if not grouped:
            return
        sync_receipt_date = self._config()["sync_receipt_date"]
        for shipment in self.search([("name", "in", list(grouped)), ("state", "!=", "deactivated")]):
            try:
                with self.env.cr.savepoint():
                    shipment._apply(grouped[shipment.name], sync_receipt_date)
            except Exception:
                _logger.exception("Dockflow: could not apply the update for %s", shipment.name)

    def _apply(self, records, sync_receipt_date):
        values, containers, events = mapping.shipment_values(records)
        if self.last_updated and values["last_updated"] and values["last_updated"] < self.last_updated:
            return
        previous_eta = self.eta
        values["state"] = "tracking" if values.pop("is_active") else "deactivated"
        values["container_ids"] = [Command.clear()] + [Command.create(c) for c in containers]
        values["event_ids"] = [Command.clear()] + [Command.create(e) for e in events]
        self.write(values)
        order = self.purchase_order_id
        if previous_eta and self.eta and abs(self.eta - previous_eta) > ETA_SHIFT_TO_REPORT:
            order.message_post(body=self.env._(
                "Dockflow: the ETA of %(shipment)s (%(reference)s) moved from %(old)s to %(new)s (UTC).",
                shipment=self.name, reference=self.reference,
                old=fields.Datetime.to_string(previous_eta), new=fields.Datetime.to_string(self.eta),
            ))
        if sync_receipt_date and order.dockflow_eta:
            order.picking_ids.filtered(
                lambda p: p.picking_type_code == "incoming" and p.state not in ("done", "cancel")
            ).write({"scheduled_date": order.dockflow_eta})


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
