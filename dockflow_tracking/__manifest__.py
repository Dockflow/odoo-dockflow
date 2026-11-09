{
    "name": "Ocean Container Tracking",
    "version": "20.0.1.0.0",
    "summary": "Track containers, bills of lading and bookings on purchase orders, with ETA and milestones from Dockflow",
    "description": """
Requires a Dockflow account (dockflow.com). After you opt in, the module sends
the bill of lading, booking or container numbers you enter on purchase orders,
plus the purchase order reference, to the Dockflow API. It receives status,
ports, ETD/ETA, ATD/ATA and milestones back. No other data is sent.
""",
    "author": "Dockflow",
    "license": "LGPL-3",
    "category": "Inventory/Purchase",
    "depends": ["purchase_stock"],
    "data": [
        "security/ir.access.csv",
        "data/ir_cron.xml",
        "views/dockflow_shipment_views.xml",
        "views/purchase_order_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "support": "support@dockflow.com",
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
}
