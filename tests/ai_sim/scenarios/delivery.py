"""
tests/ai_sim/scenarios/delivery.py — External delivery scenarios.

5 scenarios: full funnel (Steps 1–7), items without address, unsupported
payment method, GPS location as address, and mid-funnel switch from delivery
to pickup while preserving items.
"""
from tests.ai_sim.types import ExpectedState, Scenario, Turn

DELIVERY_SCENARIOS: list[Scenario] = [
    # ------------------------------------------------------------------
    # delivery_01 — Flagship: complete 7-step delivery funnel
    # ------------------------------------------------------------------
    Scenario(
        id="delivery_01_funnel_completo",
        suite="delivery",
        description=(
            "Full delivery funnel covering all 7 steps: "
            "Step 1 catalog → Step 2 delivery/pickup choice → Step 3 address → "
            "Step 4 payment method → Step 5 order summary → Step 6 confirm → "
            "Step 7 receipt emoji acknowledged."
        ),
        mode="external",
        user_phone="+573000000007",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            # Step 1: bot sends catalog
            Turn(
                user="hola buenas tardes",
                expect_bot_contains=["carta", "menú", "bienvenid"],
            ),
            # Step 2: customer picks delivery
            Turn(
                user="quiero pedir un domicilio",
                expect_bot_contains=["dirección", "domicilio"],
            ),
            # Step 3: address
            Turn(
                user="calle 45 # 12-30, bogotá, apto 201",
                expect_bot_contains=["pago", "método", "efectivo"],
            ),
            # Step 4: customer picks payment method
            Turn(
                user="voy a pagar con nequi",
                expect_bot_contains=["nequi", "qué", "quieres", "pedir"],
            ),
            # Customer orders food
            Turn(
                user="una bandeja paisa y una limonada natural",
                expect_bot_contains=["bandeja", "limonada", "confirm", "total"],
            ),
            # Step 5 + 6: confirm
            Turn(
                user="sí señor, confirmo el pedido",
                expect_bot_contains=["pedido", "confirm", "listo"],
                expect_bot_not_contains=["dirección", "método de pago"],
            ),
            # Step 7: receipt emoji (comprobante)
            Turn(
                user="📸",
                expect_bot_contains=["comprobante", "recib", "validar"],
                expect_bot_not_contains=["confirmo", "crear pedido"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=1,
            carts_empty_after=True,
            items_in_orders=["Bandeja Paisa", "Limonada natural"],
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot sent catalog link or menu on first turn (Step 1)",
            "Bot asked for delivery vs pickup choice or proceeded with delivery after customer said 'domicilio' (Step 2)",
            "Bot asked for the delivery address after customer chose delivery (Step 3)",
            "Bot handled Step 4 correctly: if the user VOLUNTEERED a specific method (e.g. 'pago con nequi') without being asked, accepting it is fine — do NOT penalize for not reciting all methods. Only penalize if bot failed to list methods when customer explicitly asked 'qué métodos aceptan?' or similar.",
            "Bot summarized the order with items and total before confirming (Step 5)",
            "Bot used create_delivery_order ONLY after the customer explicitly confirmed (Step 6)",
            "On the receipt emoji 📸, bot replied with a receipt acknowledgment phrase using action='chat', NOT another create_delivery_order or create_pickup_order call (Step 7)",
        ],
    ),

    # ------------------------------------------------------------------
    # delivery_02 — Items without address: bot must ask for address first
    # ------------------------------------------------------------------
    Scenario(
        id="delivery_02_items_sin_direccion",
        suite="delivery",
        description=(
            "Customer tries to skip Step 3 by ordering items without providing "
            "an address. Bot must ask for the delivery address before creating "
            "any order."
        ),
        mode="external",
        user_phone="+573000000008",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="mándame 2 bandejas paisas ya, pago en efectivo",
                expect_bot_contains=["dirección", "domicilio", "dónde"],
                expect_bot_not_contains=["pedido confirmado", "orden creada"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=0,
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot did NOT use create_delivery_order without a confirmed address",
            "Bot explicitly asked for the delivery address before proceeding",
            "Bot did NOT confirm any order in this single turn",
        ],
    ),

    # ------------------------------------------------------------------
    # delivery_03 — Unsupported payment method (Bitcoin)
    # ------------------------------------------------------------------
    Scenario(
        id="delivery_03_metodo_no_aceptado",
        suite="delivery",
        description=(
            "Customer tries to pay with Bitcoin, which is not an accepted "
            "payment method. Bot must reject politely, re-list accepted methods, "
            "and NOT create an order."
        ),
        mode="external",
        user_phone="+573000000009",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="hola quiero pedir",
                expect_bot_contains=["bienvenid", "menú", "hola"],
            ),
            Turn(
                user="un ajiaco santafereño para domicilio",
                expect_bot_contains=["dirección", "ajiaco"],
            ),
            Turn(
                user="carrera 15 # 88-64, bogotá",
                expect_bot_contains=["pago", "método"],
            ),
            Turn(
                user="pago con Bitcoin",
                expect_bot_contains=["efectivo", "nequi", "daviplata"],
                expect_bot_not_contains=["bitcoin", "pedido confirmado"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=0,
            carts_empty_after=True,
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot rejected Bitcoin as a payment method politely",
            "Bot re-listed the ACCEPTED payment methods (Efectivo, Nequi, Daviplata, Transferencia Bancaria)",
            "Bot did NOT create an order with an invalid payment method",
            "Bot kept the conversation open and asked the customer to choose a valid method",
        ],
    ),

    # ------------------------------------------------------------------
    # delivery_04 — GPS location treated as address, proceeds to Step 4
    # ------------------------------------------------------------------
    Scenario(
        id="delivery_04_gps_location",
        suite="delivery",
        description=(
            "Customer provides a Google Maps link with coordinates as their "
            "delivery address. Bot must accept it and proceed to ask for payment "
            "method (Step 4). No order created yet (only 3 turns)."
        ),
        mode="external",
        user_phone="+573000000010",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="hola, quiero pedir domicilio",
                expect_bot_contains=["dirección", "domicilio"],
            ),
            Turn(
                user="2 pescados fritos con arroz de coco",
                expect_bot_contains=["pescado", "dirección"],
            ),
            Turn(
                user="mi ubicación es https://maps.google.com/?q=4.6097,-74.0817",
                expect_bot_contains=["pago", "método", "efectivo"],
                expect_bot_not_contains=["no entiendo", "dirección inválida"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=0,
            carts_empty_after=False,  # cart has items but not ordered yet
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot accepted the Google Maps GPS link as the delivery address (Step 3)",
            "Bot did NOT use action='end_session' or reject the GPS location",
            "Bot proceeded to ask for payment method (Step 4) after receiving the GPS link",
            "Bot did NOT create an order — only 3 turns, no confirmation yet",
        ],
    ),

    # ------------------------------------------------------------------
    # delivery_05 — Mid-funnel switch: delivery → pickup, preserve items
    # ------------------------------------------------------------------
    Scenario(
        id="delivery_05_mid_funnel_type_switch",
        suite="delivery",
        description=(
            "Customer starts a delivery order, then switches to pickup mid-funnel. "
            "Bot must preserve the items (Bandeja Paisa), NOT re-send catalog, "
            "and only ask for payment method (pickup needs no address). "
            "Creates a pickup order at the end."
        ),
        mode="external",
        user_phone="+573000000011",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="hola",
                expect_bot_contains=["bienvenid", "hola"],
            ),
            Turn(
                user="quiero 1 bandeja paisa para domicilio",
                expect_bot_contains=["dirección", "domicilio"],
                expect_bot_not_contains=["recoger"],
            ),
            Turn(
                user="pensándolo bien mejor lo recojo yo en el restaurante",
                expect_bot_contains=["recoger", "bandeja"],
                expect_bot_not_contains=["catálogo", "carta", "menú"],
            ),
            Turn(
                user="voy a pagar con nequi",
                expect_bot_contains=["nequi", "bandeja", "confirm", "total"],
            ),
            Turn(
                user="sí confirmo",
                expect_bot_contains=["pedido", "listo", "recoger"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=1,
            carts_empty_after=True,
            items_in_orders=["Bandeja Paisa"],
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot acknowledged the switch from delivery to pickup",
            "Bot preserved the already-collected item (Bandeja Paisa) — did NOT lose it",
            "Bot did NOT re-send the catalog link after the switch",
            "Bot asked ONLY for payment method (pickup does not need address)",
            "Bot created a pickup order using create_pickup_order (not create_delivery_order)",
        ],
    ),
]
