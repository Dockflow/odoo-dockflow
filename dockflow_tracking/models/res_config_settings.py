from odoo import fields, models
from odoo.exceptions import UserError

from ..lib import dockflow_api


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    dockflow_opt_in = fields.Boolean(
        "Share shipment references with Dockflow", config_parameter="dockflow_tracking.opt_in",
    )
    dockflow_token = fields.Char("Dockflow API token", config_parameter="dockflow_tracking.token")
    dockflow_environment = fields.Selection(
        [("prod", "Production"), ("uat", "UAT (test)")],
        string="Dockflow environment", default="prod", config_parameter="dockflow_tracking.environment",
    )
    dockflow_sync_receipt_date = fields.Boolean(
        "Move receipt dates with the ETA", config_parameter="dockflow_tracking.sync_receipt_date",
    )
    dockflow_mock_scenario = fields.Char(
        "UAT test scenario", config_parameter="dockflow_tracking.mock_scenario",
        help="UAT only: Dockflow simulates a journey with this scenario, for example ocean_basic.",
    )

    def action_dockflow_check_connection(self):
        self.ensure_one()
        try:
            dockflow_api.DockflowClient(self.dockflow_token or "", self.dockflow_environment).check_connection()
        except dockflow_api.DockflowApiError as exc:
            raise UserError(str(exc)) from exc
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"type": "success", "message": self.env._("The connection with Dockflow works.")},
        }
