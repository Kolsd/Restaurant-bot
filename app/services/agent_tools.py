"""
Tool definitions for Claude's native tool_use API.

These replace the legacy {action, items, reply} JSON output format.
Claude now generates natural reply text directly and calls tools for actions.

Usage:
    from app.services.agent_tools import TOOLS_SALON, TOOLS_EXTERNAL, ALL_TOOLS
"""

# ---------------------------------------------------------------------------
# Individual tool definitions
# ---------------------------------------------------------------------------

_PLACE_ORDER = {
    "name": "place_order",
    "description": (
        "Add items to the table's cart and send the order to the kitchen. "
        "Use this when the customer wants to order food or drinks at their table. "
        "Only available in dine-in (salon) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "List of items the customer wants to order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the menu item exactly as it appears in the menu."
                        },
                        "qty": {
                            "type": "integer",
                            "description": "Quantity to order.",
                            "minimum": 1
                        }
                    },
                    "required": ["name", "qty"]
                },
                "minItems": 1
            },
            "notes": {
                "type": "string",
                "description": "Optional special instructions (e.g. allergies, cooking preferences, customizations)."
            }
        },
        "required": ["items"]
    }
}

_REQUEST_BILL = {
    "name": "request_bill",
    "description": (
        "Request the bill/check for the table. "
        "Use this when the customer asks for the check, wants to pay, or says they are ready to leave. "
        "Only available in dine-in (salon) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "separate_bill": {
                "type": "boolean",
                "description": "Whether the customer wants separate checks for the table. Defaults to false.",
                "default": False
            }
        },
        "required": []
    }
}

_CALL_WAITER = {
    "name": "call_waiter",
    "description": (
        "Alert a waiter to come to the table for non-billing assistance. "
        "Use this when the customer needs help that is not placing an order or requesting the bill "
        "(e.g. napkins, utensils, a question about the menu, a spill). "
        "Only available in dine-in (salon) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief description of why the waiter is needed."
            }
        },
        "required": ["reason"]
    }
}

_CREATE_DELIVERY_ORDER = {
    "name": "create_delivery_order",
    "description": (
        "Create a delivery order to be sent to the customer's address. "
        "Use this when the customer wants food delivered to their location. "
        "Only available in external (delivery/pickup) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "List of items the customer wants to order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the menu item exactly as it appears in the menu."
                        },
                        "qty": {
                            "type": "integer",
                            "description": "Quantity to order.",
                            "minimum": 1
                        }
                    },
                    "required": ["name", "qty"]
                },
                "minItems": 1
            },
            "address": {
                "type": "string",
                "description": "Full delivery address provided by the customer."
            },
            "payment_method": {
                "type": "string",
                "description": "Payment method chosen by the customer (e.g. 'efectivo', 'transferencia', 'tarjeta').",
                "enum": ["efectivo", "transferencia", "tarjeta", "nequi", "daviplata", "wompi"]
            },
            "notes": {
                "type": "string",
                "description": "Optional special instructions or order notes."
            },
            "branch_id": {
                "type": "integer",
                "description": "ID of the branch that will fulfill the order. Defaults to 0 (nearest/default branch).",
                "default": 0
            }
        },
        "required": ["items", "address", "payment_method"]
    }
}

_CREATE_PICKUP_ORDER = {
    "name": "create_pickup_order",
    "description": (
        "Create a pickup order for the customer to collect at the restaurant. "
        "Use this when the customer wants to order for pickup (to-go / para llevar). "
        "Only available in external (delivery/pickup) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "List of items the customer wants to order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the menu item exactly as it appears in the menu."
                        },
                        "qty": {
                            "type": "integer",
                            "description": "Quantity to order.",
                            "minimum": 1
                        }
                    },
                    "required": ["name", "qty"]
                },
                "minItems": 1
            },
            "payment_method": {
                "type": "string",
                "description": "Payment method chosen by the customer (e.g. 'efectivo', 'transferencia', 'tarjeta').",
                "enum": ["efectivo", "transferencia", "tarjeta", "nequi", "daviplata", "wompi"]
            },
            "notes": {
                "type": "string",
                "description": "Optional special instructions or order notes."
            },
            "branch_id": {
                "type": "integer",
                "description": "ID of the branch where the customer will pick up. Defaults to 0 (nearest/default branch).",
                "default": 0
            }
        },
        "required": ["items", "payment_method"]
    }
}

_CHANGE_PAYMENT_METHOD = {
    "name": "change_payment_method",
    "description": (
        "Change the payment method on the customer's existing pending order. "
        "Use this when the customer has already placed an order and wants to change how they will pay. "
        "Only available in external (delivery/pickup) mode."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "payment_method": {
                "type": "string",
                "description": "The new payment method the customer wants to use (e.g. 'efectivo', 'transferencia', 'tarjeta').",
                "enum": ["efectivo", "transferencia", "tarjeta", "nequi", "daviplata", "wompi"]
            }
        },
        "required": ["payment_method"]
    }
}

_MAKE_RESERVATION = {
    "name": "make_reservation",
    "description": (
        "Make a table reservation at the restaurant. "
        "Use this when the customer wants to book a table for a future date and time. "
        "Available in both dine-in (salon) and external modes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name under which the reservation will be made."
            },
            "date": {
                "type": "string",
                "description": "Reservation date in YYYY-MM-DD format.",
                "pattern": "^\\d{4}-\\d{2}-\\d{2}$"
            },
            "time": {
                "type": "string",
                "description": "Reservation time in HH:MM format (24-hour).",
                "pattern": "^\\d{2}:\\d{2}$"
            },
            "guests": {
                "type": "integer",
                "description": "Number of guests in the party.",
                "minimum": 1
            },
            "notes": {
                "type": "string",
                "description": "Optional special requests or notes for the reservation (e.g. birthday, high chair needed)."
            }
        },
        "required": ["name", "date", "time", "guests"]
    }
}

_END_SESSION = {
    "name": "end_session",
    "description": (
        "Close the current session and end the conversation flow. "
        "Use this when the customer explicitly says goodbye, wants to end the chat, "
        "or when the interaction is naturally complete. "
        "Available in both dine-in (salon) and external modes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

# ---------------------------------------------------------------------------
# Exported tool lists by mode
# ---------------------------------------------------------------------------

TOOLS_SALON: list[dict] = [
    _PLACE_ORDER,
    _REQUEST_BILL,
    _CALL_WAITER,
    _MAKE_RESERVATION,
    _END_SESSION,
]
"""Tools available in dine-in (salon/table) mode."""

TOOLS_EXTERNAL: list[dict] = [
    _CREATE_DELIVERY_ORDER,
    _CREATE_PICKUP_ORDER,
    _CHANGE_PAYMENT_METHOD,
    _MAKE_RESERVATION,
    _END_SESSION,
]
"""Tools available in external (delivery/pickup) mode."""

# ---------------------------------------------------------------------------
# Lookup dict: tool name → definition
# ---------------------------------------------------------------------------

ALL_TOOLS: dict[str, dict] = {
    tool["name"]: tool
    for tool in [
        _PLACE_ORDER,
        _REQUEST_BILL,
        _CALL_WAITER,
        _CREATE_DELIVERY_ORDER,
        _CREATE_PICKUP_ORDER,
        _CHANGE_PAYMENT_METHOD,
        _MAKE_RESERVATION,
        _END_SESSION,
    ]
}
"""Maps every tool name to its definition dict for O(1) lookup."""
