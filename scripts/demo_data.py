"""Demo restaurant data: a Colombian carta and the feature set it runs with.

Shared by the AI simulator (tests/ai_sim/seed.py) and the demo seed. Kept
from scripts/setup_demo.py when that WhatsApp-era script was deleted
(2026-09-25).
"""

DEMO_MENU = {
    "categories": [
        {
            "name": "Entradas",
            "items": [
                {
                    "name": "Empanadas",
                    "description": "Empanadas de pipián y carne con ají de maní",
                    "price": 8000,
                    "sku": "ENT-001",
                    "available": True,
                },
                {
                    "name": "Patacones con hogao",
                    "description": "Patacones fritos con hogao casero y suero costeño",
                    "price": 12000,
                    "sku": "ENT-002",
                    "available": True,
                },
                {
                    "name": "Ceviche de camarón",
                    "description": "Camarón fresco marinado en limón, cilantro y tomate",
                    "price": 18000,
                    "sku": "ENT-003",
                    "available": True,
                },
            ],
        },
        {
            "name": "Platos Fuertes",
            "items": [
                {
                    "name": "Bandeja Paisa",
                    "description": "Frijoles, chicharrón, carne molida, chorizo, morcilla, arepa, arroz y huevo",
                    "price": 28000,
                    "sku": "PF-001",
                    "available": True,
                },
                {
                    "name": "Ajiaco Santafereño",
                    "description": "Sopa bogotana con tres tipos de papa, pollo, guasca y crema de leche",
                    "price": 22000,
                    "sku": "PF-002",
                    "available": True,
                },
                {
                    "name": "Pescado frito con arroz de coco",
                    "description": "Mojarra roja frita con arroz de coco y patacón",
                    "price": 25000,
                    "sku": "PF-003",
                    "available": True,
                },
                {
                    "name": "Lomo de res en salsa de champiñones",
                    "description": "Lomo fino a la plancha con salsa cremosa de champiñones y papas al vapor",
                    "price": 32000,
                    "sku": "PF-004",
                    "available": True,
                },
            ],
        },
        {
            "name": "Bebidas",
            "items": [
                {
                    "name": "Limonada natural",
                    "description": "Limonada fría con panela o azúcar",
                    "price": 6000,
                    "sku": "BEB-001",
                    "available": True,
                },
                {
                    "name": "Jugo de lulo",
                    "description": "Jugo natural de lulo colombiano en agua o leche",
                    "price": 7000,
                    "sku": "BEB-002",
                    "available": True,
                },
                {
                    "name": "Agua",
                    "description": "Agua mineral o del tiempo",
                    "price": 4000,
                    "sku": "BEB-003",
                    "available": True,
                },
                {
                    "name": "Cerveza Club Colombia",
                    "description": "Botella 330ml fría",
                    "price": 8000,
                    "sku": "BEB-004",
                    "available": True,
                },
            ],
        },
        {
            "name": "Postres",
            "items": [
                {
                    "name": "Tres leches",
                    "description": "Bizcocho esponjoso bañado en tres tipos de leche con crema chantilly",
                    "price": 12000,
                    "sku": "POS-001",
                    "available": True,
                },
                {
                    "name": "Oblea con arequipe",
                    "description": "Oblea crujiente con arequipe, mermelada y coco rallado",
                    "price": 8000,
                    "sku": "POS-002",
                    "available": True,
                },
            ],
        },
    ]
}

DEMO_FEATURES = {
    "payment_methods": [
        {"name": "Efectivo", "enabled": True},
        {"name": "Nequi", "enabled": True},
        {"name": "Daviplata", "enabled": True},
        {"name": "Transferencia Bancaria", "enabled": True},
    ],
    "payment_instructions": {
        "Nequi": "Enviar al 300-123-4567",
        "Daviplata": "Enviar al 300-123-4567",
        "Transferencia Bancaria": "Banco de Bogotá - Cuenta de Ahorros 123456789",
    },
    "bot_active": True,
    "domicilio_active": True,
    "recoger_active": True,
    "upsell_active": True,
    "currency": "COP",
    "locale": "es-CO",
    "timezone": "America/Bogota",
}

