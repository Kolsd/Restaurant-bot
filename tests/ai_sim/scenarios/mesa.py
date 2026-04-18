"""
tests/ai_sim/scenarios/mesa.py — Dine-in (salon) scenarios.

6 scenarios covering: full flow with sub-order, split check, no-duplicate
sub-order, waiter assistance, reservation with relative date, and delivery
request rejected from inside the restaurant.
"""
from tests.ai_sim.types import ExpectedState, Scenario, Turn

MESA_SCENARIOS: list[Scenario] = [
    # ------------------------------------------------------------------
    # mesa_01 — Flagship: full dine-in flow with sub-order and bill
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_01_flujo_completo",
        suite="mesa",
        description=(
            "Full dine-in flow: greeting → order drink → add food → "
            "sub-order (extra item, must NOT duplicate) → request bill with cash. "
            "Exercises place_order, sub-order append, request_bill."
        ),
        mode="table",
        user_phone="+573000000001",
        bot_number="+57TESTBOT1",
        table_hint="1",
        turns=[
            Turn(
                user="hola buenas",
                expect_bot_contains=["bienvenid", "mesa"],
            ),
            Turn(
                user="una limonada natural porfavor",
                expect_bot_contains=["limonada", "confirm"],
                expect_bot_not_contains=["dirección", "domicilio"],
            ),
            Turn(
                user="sip",
                expect_bot_contains=["pedido", "listo", "orden"],
            ),
            Turn(
                user="también quiero una bandeja paisa",
                expect_bot_contains=["bandeja", "confirm"],
            ),
            Turn(
                user="si claro",
                expect_bot_contains=["bandeja", "listo", "orden"],
            ),
            Turn(
                user="y nos ponen una cerveza club colombia mas",
                expect_bot_contains=["cerveza", "confirm"],
            ),
            Turn(
                user="dale",
                expect_bot_contains=["cerveza"],
            ),
            Turn(
                user="la cuenta por favor, vamos a pagar en efectivo",
                expect_bot_contains=["cuenta", "mesero", "efectivo"],
                expect_bot_not_contains=["nequi", "daviplata"],
            ),
        ],
        expected_state=ExpectedState(
            table_orders_count=None,  # at least one order committed
            items_in_orders=["Limonada natural", "Bandeja Paisa", "Cerveza Club Colombia"],
            waiter_alerts_types=["bill"],
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot greeted mentioning the restaurant name or welcoming to the table",
            "Bot used place_order tool (not create_delivery_order or create_pickup_order)",
            "Bot did NOT ask for a delivery address or payment method during the meal",
            "Bot used request_bill action when the customer asked for the check",
            "Sub-order (Cerveza) did NOT duplicate Limonada or Bandeja Paisa in the items[] array — each tool call contained ONLY the newly added item(s)",
            "Bot confirmed the FIRST order before placing it. Sub-orders (additional items added AFTER the initial place_order) do NOT require a separate confirmation turn — the confirmation guard in agent.py:1043 intentionally skips the awaiting_confirmation step when has_order=True. Do NOT penalize sub-orders that are placed immediately after the customer adds more items.",
            "The scenario ends right after the bot shows the tip menu. It is ACCEPTABLE for the final turn to be the tip prompt — do NOT penalize the bot for not completing the full checkout state machine (payment method, factura, split) within the given turn budget.",
        ],
    ),

    # ------------------------------------------------------------------
    # mesa_02 — Split check: 3 friends want separate bills
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_02_split_check",
        suite="mesa",
        description=(
            "Table of 3 friends, multiple items ordered, then they request "
            "separate bills. Bot must acknowledge the split intent and start "
            "the in-bot checkout flow (request_bill creates a waiter alert, then "
            "bot guides the customer through split → tip → payment method for "
            "each check). A 'bill' waiter_alert must exist."
        ),
        mode="table",
        user_phone="+573000000002",
        bot_number="+57TESTBOT1",
        table_hint="2",
        turns=[
            Turn(
                user="hola somos 3, queremos pedir",
                expect_bot_contains=["bienvenid", "mesa", "hola"],
            ),
            Turn(
                user="queremos 3 ajaicos santafereños y 3 aguas",
                expect_bot_contains=["ajiaco", "agua", "confirm"],
            ),
            Turn(
                user="sii todo está bien",
                expect_bot_contains=["pedido", "orden", "listo"],
            ),
            Turn(
                user="queremos pagar por separado, somos 3 personas",
                expect_bot_contains=["separad", "mesero", "cuenta"],
            ),
        ],
        expected_state=ExpectedState(
            waiter_alerts_types=["bill"],
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot acknowledged the split bill request (either via request_bill tool OR by entering the checkout state machine with split_count detected)",
            "Bot did NOT try to create a delivery or pickup order — stayed in salon mode",
            "SYSTEM DESIGN NOTE: The real bot intentionally handles the split INSIDE the checkout state machine — it computes per-person amounts, asks for tip, then walks each diner through payment. Do NOT penalize the bot for handling the split in-bot instead of deferring everything to a human waiter. A bill waiter_alert IS created at checkout start (visible in DB snapshot as waiter_alerts_types=['bill']) so staff is still notified.",
            "Bot did NOT ask for a delivery address or pickup branch",
        ],
    ),

    # ------------------------------------------------------------------
    # mesa_03 — Sub-order must NOT duplicate items already ordered
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_03_sub_orden_sin_duplicar",
        suite="mesa",
        description=(
            "Critical no-duplicate rule: customer orders Bandeja Paisa, confirms, "
            "then adds a Cerveza. The second place_order tool call items[] must "
            "contain ONLY Cerveza — never the already-confirmed Bandeja Paisa."
        ),
        mode="table",
        user_phone="+573000000003",
        bot_number="+57TESTBOT1",
        table_hint="3",
        turns=[
            Turn(
                user="buenas, una bandeja paisa",
                expect_bot_contains=["bandeja", "confirm"],
            ),
            Turn(
                user="sí perfecto",
                expect_bot_contains=["pedido", "listo", "orden"],
            ),
            Turn(
                user="oiga y también una cerveza club colombia",
                expect_bot_contains=["cerveza", "confirm"],
            ),
            Turn(
                user="sí",
                expect_bot_contains=["cerveza"],
            ),
        ],
        expected_state=ExpectedState(
            items_in_orders=["Bandeja Paisa", "Cerveza Club Colombia"],
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "When adding Cerveza Club Colombia as a sub-order, the bot's place_order "
            "tool call 'items' array contained ONLY Cerveza Club Colombia — it NEVER "
            "repeated Bandeja Paisa (which was already confirmed). Repeating items "
            "would double-charge the customer.",
        ],
    ),

    # ------------------------------------------------------------------
    # mesa_04 — Waiter assistance (no food order, just call_waiter)
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_04_mesero_servilletas",
        suite="mesa",
        description=(
            "Customer needs waiter assistance: requests more napkins and reports "
            "spilled water. Bot must use call_waiter (real codebase stores this "
            "with alert_type='waiter'), NOT request_bill, and must NOT create a "
            "food order."
        ),
        mode="table",
        user_phone="+573000000004",
        bot_number="+57TESTBOT1",
        table_hint="4",
        turns=[
            Turn(
                user="por favor traigan más servilletas",
                expect_bot_contains=["mesero", "aviso", "servilleta"],
                expect_bot_not_contains=["cuenta", "pago"],
            ),
            Turn(
                user="también se derramó un poco de agua aquí en la mesa",
                expect_bot_contains=["mesero", "enseguida", "aviso"],
            ),
        ],
        expected_state=ExpectedState(
            table_orders_count=0,
            waiter_alerts_count=None,  # at least 1
            waiter_alerts_types=["waiter"],  # real codebase uses 'waiter' alert_type for assistance (tables_repo.py:826)
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot used call_waiter (NOT request_bill) — note: waiter_alerts.type='waiter' in the DB snapshot is the CORRECT value (the real codebase uses 'waiter' as the type for call_waiter). Do NOT flag type='waiter' as wrong.",
            "No food order was created (no place_order tool was called)",
            "Bot confirmed the waiter was notified about the napkins and spilled water",
            "Bot did NOT ask what the customer wants to eat",
            "It is acceptable if only a single waiter_alert row exists — some LLMs batch both requests into one acknowledgement; do NOT penalize this.",
        ],
    ),

    # ------------------------------------------------------------------
    # mesa_05 — Reservation with relative date: bot must ask for concrete date
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_05_reserva_fecha_relativa",
        suite="mesa",
        description=(
            "Customer says 'mañana' (tomorrow) — bot must NOT call make_reservation "
            "with a literal 'mañana' string. Must ask for a specific calendar date. "
            "Then customer says 'para el 20 de diciembre' and confirms."
        ),
        mode="table",
        user_phone="+573000000005",
        bot_number="+57TESTBOT1",
        table_hint="1",
        turns=[
            Turn(
                user="quiero reservar para mañana a las 7pm, somos 4, soy Carlos",
                expect_bot_contains=["fecha", "específic", "cuál", "día"],
                expect_bot_not_contains=["reserva confirmada", "reserva creada"],
            ),
            Turn(
                user="para el 20 de diciembre",
                expect_bot_contains=["diciembre", "confirm", "4 person"],
            ),
            Turn(
                user="sí confirmo",
                expect_bot_contains=["reserva", "confirmada", "Carlos"],
            ),
        ],
        expected_state=ExpectedState(
            reservations_count=1,
            reservations_status="pending",
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot did NOT use make_reservation on the first turn when the customer said 'mañana' — a relative date is ambiguous and cannot be stored as a concrete date",
            "Bot explicitly asked for a specific calendar date (not just re-asked about guests or name)",
            "Bot created the reservation ONLY after the customer provided '20 de diciembre' AND confirmed",
            "Bot did NOT show a raw YYYY-MM-DD timestamp to the customer in the replies",
            "Bot acknowledged the customer's name (Carlos) and party size (4) in the confirmation",
            "DB snapshot note: reservations.date is a TEXT column. If it contains ANY ISO-like string (e.g. '2024-12-20') or any non-null value that matches the intent, treat the reservation as correctly created — do NOT penalize for date format rendering.",
        ],
    ),

    # ------------------------------------------------------------------
    # mesa_06 — Delivery request from inside the restaurant → must reject
    # ------------------------------------------------------------------
    Scenario(
        id="mesa_06_pide_domicilio_en_mesa",
        suite="mesa",
        description=(
            "Customer at table 3 asks whether deliveries are available and wants "
            "to send food to a relative. Bot must reject the delivery request with "
            "the mesa-only policy and NOT start the external funnel."
        ),
        mode="table",
        user_phone="+573000000006",
        bot_number="+57TESTBOT1",
        table_hint="3",
        turns=[
            Turn(
                user="oye hacen domicilios? quiero mandarle algo a mi mamá en la casa",
                expect_bot_contains=["mesa", "canal", "solo"],
                expect_bot_not_contains=["dirección", "enviar", "catálogo", "menu"],
            ),
        ],
        expected_state=ExpectedState(
            table_orders_count=0,
            orders_count=0,
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot refused to process a delivery request from inside the restaurant (table context)",
            "Bot replied with the mesa-only channel policy (something like 'este canal es para pedidos en mesa')",
            "Bot did NOT send a catalog link or start the delivery funnel",
            "Bot did NOT use create_delivery_order",
            "Bot was polite about the refusal and offered to take a dine-in order instead",
        ],
    ),
]
