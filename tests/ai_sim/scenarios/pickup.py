"""
tests/ai_sim/scenarios/pickup.py — Pickup (recoger en restaurante) scenarios.

4 scenarios: full pickup funnel, missing payment method, item change
(replace not add), and time estimate question during pickup flow.
"""
from tests.ai_sim.types import ExpectedState, Scenario, Turn

PICKUP_SCENARIOS: list[Scenario] = [
    # ------------------------------------------------------------------
    # pickup_01 — Flagship: complete pickup funnel, no address asked
    # ------------------------------------------------------------------
    Scenario(
        id="pickup_01_funnel_completo",
        suite="pickup",
        description=(
            "Full pickup flow: greeting → catalog → customer picks pickup → "
            "items → payment method → confirmation. "
            "Bot must NOT ask for an address at any point."
        ),
        mode="external",
        user_phone="+573000000012",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="hola buenas",
                expect_bot_contains=["bienvenid", "menú", "hola"],
            ),
            Turn(
                user="quiero recoger un pedido en el restaurante",
                expect_bot_contains=["recoger", "qué", "pedir"],
                expect_bot_not_contains=["dirección", "domicilio"],
            ),
            Turn(
                user="un pescado frito con arroz de coco y una limonada",
                expect_bot_contains=["pescado", "limonada", "pago", "método"],
                expect_bot_not_contains=["dirección"],
            ),
            Turn(
                user="pago con nequi",
                expect_bot_contains=["nequi", "confirm", "total"],
            ),
            Turn(
                user="sí todo bien, confirmo",
                expect_bot_contains=["pedido", "listo", "recoger"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=1,
            carts_empty_after=True,
            items_in_orders=["Pescado frito con arroz de coco", "Limonada natural"],
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot offered delivery/pickup choice or proceeded with pickup after customer said 'recoger'",
            "Bot did NOT ask for a delivery address at any point during the pickup flow",
            "Bot listed payment methods when asking how the customer will pay",
            "Bot summarized the order and total before confirming",
            "Bot used create_pickup_order (not create_delivery_order) after confirmation",
            "System design note: For online payments (Nequi/Daviplata/Transferencia) the order is INTENTIONALLY created with status='pendiente' BEFORE the proof photo arrives — the bot requests proof in its reply text AFTER the tool fires. Do NOT flag this ordering as a violation.",
        ],
    ),

    # ------------------------------------------------------------------
    # pickup_02 — No payment method provided: bot must ask before ordering
    # ------------------------------------------------------------------
    Scenario(
        id="pickup_02_sin_metodo_pago",
        suite="pickup",
        description=(
            "Customer tries to skip the payment method step in pickup flow. "
            "Bot must ask for payment method before creating the order."
        ),
        mode="external",
        user_phone="+573000000013",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="quiero recoger 1 ajiaco santafereño ya, lo confirmo",
                expect_bot_contains=["pago", "método", "cómo"],
                expect_bot_not_contains=["pedido creado", "orden lista", "confirmad"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=0,
            carts_empty_after=False,  # item may be in cart waiting for payment
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot asked for payment method before creating the pickup order",
            "Bot did NOT use create_pickup_order on this single turn without a payment method",
            "Bot did NOT confirm an order that has no payment method",
        ],
    ),

    # ------------------------------------------------------------------
    # pickup_03 — Item change: replace Bandeja with Ajiaco, then confirm
    # ------------------------------------------------------------------
    Scenario(
        id="pickup_03_cambio_de_opinion",
        suite="pickup",
        description=(
            "Customer orders Bandeja Paisa for pickup, then changes mind and "
            "wants Ajiaco Santafereño instead. Bot must replace (not add). "
            "Final order must contain Ajiaco and NOT Bandeja Paisa."
        ),
        mode="external",
        user_phone="+573000000014",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="quiero recoger una bandeja paisa",
                expect_bot_contains=["bandeja", "pago", "dirección", "confirm"],
            ),
            Turn(
                user="mejor cámbialo a 1 ajiaco santafereño, no quiero la bandeja",
                expect_bot_contains=["ajiaco", "bandeja"],
            ),
            Turn(
                user="sí así está bien, pago en daviplata",
                expect_bot_contains=["daviplata", "ajiaco", "confirm", "total"],
            ),
            Turn(
                user="confirmo",
                expect_bot_contains=["pedido", "listo", "recoger"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=1,
            carts_empty_after=True,
            items_in_orders=["Ajiaco"],
            items_not_in_orders=["Bandeja Paisa"],
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot handled the item replacement correctly (removed Bandeja Paisa, added Ajiaco Santafereño)",
            "Final committed order contains Ajiaco Santafereño, NOT Bandeja Paisa",
            "Bot did NOT add Ajiaco on top of Bandeja (which would be 2 items) — it replaced",
            "System design note: For online payments (Nequi/Daviplata/Transferencia) the order is INTENTIONALLY created with status='pendiente' BEFORE the proof photo arrives — the bot requests proof in its reply text AFTER the tool fires. Do NOT flag this ordering as a violation.",
        ],
    ),

    # ------------------------------------------------------------------
    # pickup_04 — Full flow + customer asks for time estimate
    # ------------------------------------------------------------------
    Scenario(
        id="pickup_04_tiempo_estimado",
        suite="pickup",
        description=(
            "Standard pickup flow completed, then customer asks how long it will "
            "take. Bot should give a reasonable time estimate or refer to the "
            "restaurant. Order is completed correctly."
        ),
        mode="external",
        user_phone="+573000000015",
        bot_number="+57TESTBOT1",
        table_hint=None,
        turns=[
            Turn(
                user="hola quiero recoger 2 obleas con arequipe",
                expect_bot_contains=["oblea", "pago", "método"],
            ),
            Turn(
                user="pago en transferencia bancaria",
                expect_bot_contains=["transferencia", "oblea", "confirm", "total"],
            ),
            Turn(
                user="sí confirmo el pedido",
                expect_bot_contains=["pedido", "listo", "recoger"],
            ),
            Turn(
                user="cuánto tarda para tenerlo listo?",
                expect_bot_contains=["minuto", "tiempo", "listo", "restaurante"],
            ),
        ],
        expected_state=ExpectedState(
            orders_count=1,
            carts_empty_after=True,
            items_in_orders=["Oblea con arequipe"],
            tokens_used_gt_zero=True,
        ),
        judge_criteria=[
            "Bot gave a reasonable time estimate (e.g. 15–30 minutes) or deferred to the restaurant staff",
            "Bot completed the pickup flow correctly before the time question",
            "Bot used create_pickup_order after confirmation",
            "Bot did NOT ask for a delivery address",
            "MANDATORY SYSTEM-DESIGN RULE (overrides all other concerns): For online payments (Nequi/Daviplata/Transferencia), the order is CORRECTLY created with status='pendiente' BEFORE the proof photo is requested — this is the intended flow. DO NOT list 'order created before proof' as a critical_failure. DO NOT claim the bot violated proof ordering. The bot requests proof in its reply AFTER the tool fires; this IS the correct sequence. Only flag a critical failure here if the bot NEVER asked for proof at all.",
        ],
    ),
]
