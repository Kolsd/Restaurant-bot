"""
Tool definitions for Claude's native tool_use API.

These replace the legacy {action, items, reply} JSON output format.
Claude now generates natural reply text directly and calls tools for actions.

Usage:
    from app.services.agent_tools import TOOLS_SALON, ALL_TOOLS

Delivery/pickup order-creation tools (create_delivery_order, create_pickup_order,
change_payment_method, cancel_order, notify_arrival) were removed in chunk 9 of
the web delivery wave (docs/claude/delivery-web.md): ordering moved entirely to
the web channel, and WhatsApp customers now get a deterministic reply instead of
an LLM tool-use loop, so these tools must never be handed to the model again.
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

_MAKE_RESERVATION = {
    "name": "make_reservation",
    "description": (
        "Make a table reservation at the restaurant. "
        "Use this when the customer wants to book a table for a future date and time."
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
        "or when the interaction is naturally complete."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

_SEND_DISH_CARD = {
    "name": "send_dish_card",
    "description": (
        "Muestra en el chat la tarjeta del plato: foto, nombre, precio y descripción corta. "
        "Úsalo SOLO cuando el cliente pide ver o recomendaciones de un plato específico "
        "Y el restaurante tiene imágenes configuradas. "
        "Si el plato no tiene foto disponible, NO uses esta tool — responde con texto normal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dish_name": {
                "type": "string",
                "description": "Nombre exacto del plato tal como aparece en el menú",
            },
            "caption": {
                "type": "string",
                "description": (
                    "Texto opcional de 1-2 líneas. "
                    "Si se omite, se usa la descripción del plato."
                ),
            },
        },
        "required": ["dish_name"],
    },
}

_CANCEL_RESERVATION = {
    "name": "cancel_reservation",
    "description": (
        "Cancel the customer's upcoming reservation. "
        "Only cancellable when the reservation status is 'pending' or 'confirmed' "
        "AND the reservation date is today or in the future. "
        "If the reservation is already in the past, or status is 'cancelled' / 'no_show', "
        "do NOT call this tool — inform the customer there is nothing to cancel. "
        "Do NOT auto-refund deposits; if a deposit was paid, inform the customer it will "
        "be kept as credit and the restaurant will contact them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional cancellation reason provided by the customer (free text, max 500 chars)."
            }
        },
        "required": []
    }
}

_REMEMBER_CUSTOMER_PREFERENCE = {
    "name": "remember_customer_preference",
    "description": (
        "Save a preference the customer just mentioned in their message (dietary "
        "restriction, allergy, dislike, favorite dish, or a noteworthy preference). "
        "Call this ONLY when the customer explicitly states something worth remembering "
        "for next time. Do NOT invent preferences. Do NOT call more than 3 times per conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "enum": ["dietary", "allergies", "dislikes", "favorite_dish", "notes"],
                "description": "Category of the preference."
            },
            "value": {
                "type": "string",
                "description": "The preference value, exactly as the customer described it (max 200 chars)."
            },
            "reason": {
                "type": "string",
                "description": "The exact phrase from the customer that motivated saving this. Required to prevent hallucinated preferences."
            }
        },
        "required": ["key", "value", "reason"]
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
    _CANCEL_RESERVATION,
    _END_SESSION,
    _REMEMBER_CUSTOMER_PREFERENCE,
    # cache_control on the LAST tool caches all tools in this list.
    # Anthropic prompt caching for tools: the cache breakpoint is set on
    # the last tool entry, so all preceding tools are included in the cache.
    {**_SEND_DISH_CARD, "cache_control": {"type": "ephemeral"}},
]
"""Tools available in dine-in (salon/table) mode. Since chunk 9 of the web
delivery wave, this is also the only tool list the agent ever uses — the old
"external" (delivery/pickup) mode and its order-creation tools were removed."""

# ---------------------------------------------------------------------------
# Lookup dict: tool name → definition
# ---------------------------------------------------------------------------

ALL_TOOLS: dict[str, dict] = {
    tool["name"]: tool
    for tool in [
        _PLACE_ORDER,
        _REQUEST_BILL,
        _CALL_WAITER,
        _MAKE_RESERVATION,
        _CANCEL_RESERVATION,
        _END_SESSION,
        _REMEMBER_CUSTOMER_PREFERENCE,
        _SEND_DISH_CARD,
    ]
}
"""Maps every tool name to its definition dict for O(1) lookup."""
