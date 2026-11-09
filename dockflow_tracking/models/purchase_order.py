from odoo import api, fields, models


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    dockflow_shipment_ids = fields.One2many("dockflow.shipment", "purchase_order_id", string="Shipments")
    dockflow_shipment_count = fields.Integer(compute="_compute_dockflow_eta")
    dockflow_eta = fields.Datetime(
        "ETA", compute="_compute_dockflow_eta", store=True,
        help="Latest ETA of the shipments that Dockflow is tracking for this order.",
    )

    @api.depends("dockflow_shipment_ids.eta", "dockflow_shipment_ids.state")
    def _compute_dockflow_eta(self):
        for order in self:
            order.dockflow_shipment_count = len(order.dockflow_shipment_ids)
            etas = order.dockflow_shipment_ids.filtered(lambda s: s.state != "deactivated" and s.eta).mapped("eta")
            order.dockflow_eta = max(etas) if etas else False

    def button_cancel(self):
        result = super().button_cancel()
        self.dockflow_shipment_ids.filtered(lambda s: s.state in ("sent", "tracking", "error"))._deactivate()
        return result

    def action_view_dockflow_shipments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Shipments"),
            "res_model": "dockflow.shipment",
            "view_mode": "list,form",
            "domain": [("purchase_order_id", "=", self.id)],
            "context": {"default_purchase_order_id": self.id},
        }
