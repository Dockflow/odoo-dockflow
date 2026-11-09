"""Translation between Odoo shipments and Dockflow v3 records.

Plain Python with no Odoo import. Dates come out as naive UTC datetimes,
which is how Odoo stores Datetime fields.
"""

from datetime import datetime, timezone

REFERENCE_FIELDS = {
    "bill_of_lading": "bill_of_lading_reference",
    "booking": "booking_reference",
    "container": "container_reference",
}
# Without these flags Dockflow merges references, so a corrected one would sit next to the old one.
COMPLETE_FLAGS = {
    "bill_of_lading": "bill_of_lading_references_complete",
    "booking": "booking_references_complete",
    "container": "container_references_complete",
}

# Dockflow's name for a leg whose vessel is not known yet.
PLACEHOLDER_VESSEL = "Unknown vessel"

# The ports a UAT mock journey needs. Only used when a test scenario is set.
MOCK_PORTS = ("BEANR", "USLAX")


def parse_datetime(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def downstream_payload(tradeflow_reference, reference_type, reference, order_name, mock_scenario=None):
    payload = {"tradeflow_reference": tradeflow_reference}
    for kind, field in REFERENCE_FIELDS.items():
        payload[field] = [reference.strip()] if kind == reference_type else []
        payload[COMPLETE_FLAGS[kind]] = True
    payload["custom_references"] = {"odoo_purchase_order": order_name}
    payload["active"] = True
    if mock_scenario:
        payload["mock_tracking"] = {"scenario": mock_scenario}
        payload["port_of_loading"], payload["port_of_discharge"] = MOCK_PORTS
    return payload


def group_by_tradeflow(records):
    """Records per tradeflow reference, one per split, keeping the newest version of each."""
    newest = {}
    for record in records:
        reference = record.get("tradeflow_reference")
        if not reference:
            continue
        key = (reference, _split_number(record))
        stamp = (record.get("metadata") or {}).get("last_updated") or ""
        if key not in newest or stamp > ((newest[key].get("metadata") or {}).get("last_updated") or ""):
            newest[key] = record
    grouped = {}
    for (reference, _split), record in newest.items():
        grouped.setdefault(reference, []).append(record)
    return grouped


def _split_number(record):
    return (record.get("shipment") or {}).get("split_shipment_number") or 1


def shipment_values(records):
    """Shipment, container and event values from all records of one tradeflow.

    The first split carries the voyage; containers and events come from every split.
    """
    records = sorted(records, key=_split_number)
    main = records[0]
    shipment = main.get("shipment") or {}
    metadata = main.get("metadata") or {}
    vessel = (shipment.get("current_vessel") or {}).get("name")
    # The PO waits for the last split, so the latest arrival is the one that counts.
    arrivals = [d for d in (parse_datetime((r.get("shipment") or {}).get("arrival_event_date")) for r in records) if d]
    stamps = [d for d in (parse_datetime((r.get("metadata") or {}).get("last_updated")) for r in records) if d]
    values = {
        "carrier_name": metadata.get("carrier_name") or False,
        "is_active": bool(metadata.get("is_active", True)),
        "last_updated": max(stamps) if stamps else None,
        "bill_of_lading": main.get("bill_of_lading") or False,
        "booking": main.get("booking") or False,
        "port_of_loading": shipment.get("port_of_loading") or False,
        "port_of_discharge": shipment.get("port_of_discharge") or False,
        "transshipment_ports": ", ".join(shipment.get("transshipment_ports") or []) or False,
        "etd": parse_datetime(shipment.get("departure_event_date")),
        "etd_is_actual": bool(shipment.get("departure_is_actual")),
        "eta": max(arrivals) if arrivals else None,
        "eta_is_actual": all(bool((r.get("shipment") or {}).get("arrival_is_actual")) for r in records),
        "vessel": vessel if vessel and vessel != PLACEHOLDER_VESSEL else False,
    }
    containers, events, seen = [], [], set()
    for record in records:
        for container in record.get("containers") or []:
            if container.get("reference") in seen:
                continue
            seen.add(container.get("reference"))
            milestone = container.get("latest_actual_milestone") or {}
            containers.append({
                "reference": container.get("reference"),
                "iso_type": container.get("iso_type") or False,
                "eta": parse_datetime(container.get("arrival_at_pod_event_date")),
                "eta_is_actual": bool(container.get("arrival_at_pod_is_actual")),
                "latest_milestone": milestone.get("code") or False,
                "latest_milestone_date": parse_datetime(milestone.get("event_date")),
                "latest_milestone_location": milestone.get("location_reference") or False,
            })
        for event in record.get("events") or []:
            events.append({
                "code": event.get("code") or False,
                "message": event.get("message") or False,
                "event_date": parse_datetime(event.get("event_date")),
                "is_actual": bool(event.get("actual")),
                "location": event.get("location_reference") or False,
                "container_reference": event.get("container_reference") or False,
            })
    events.sort(key=lambda e: (e["event_date"] or datetime.max, e["code"] or ""))
    return values, containers, events
