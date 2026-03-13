# ============================================================
# CONFIGURACIÓN DEL RESTAURANTE - Editar con datos reales
# ============================================================

RESTAURANT_INFO = {
    "name": "La Trattoria Italiana",
    "address": "Av. Insurgentes Sur 1234, Col. del Valle, CDMX",
    "phone": "+52 55 1234 5678",
    "whatsapp": "+52 55 1234 5678",
    "hours": {
        "lunes": "13:00 - 22:00",
        "martes": "13:00 - 22:00",
        "miércoles": "13:00 - 22:00",
        "jueves": "13:00 - 23:00",
        "viernes": "13:00 - 23:30",
        "sábado": "12:00 - 23:30",
        "domingo": "12:00 - 21:00",
    },
    "instagram": "@latrattoria",
    "google_maps": "https://maps.google.com/?q=La+Trattoria+Italiana"
}

MENU = {
    "Entradas": [
        {"name": "Bruschetta al Pomodoro", "price": 120, "description": "Pan tostado con tomate fresco, albahaca y aceite de oliva", "orders": 340, "vegetarian": True},
        {"name": "Carpaccio de Res", "price": 185, "description": "Finas láminas de res con rúcula, parmesano y limón", "orders": 210, "vegetarian": False},
        {"name": "Burrata Fresca", "price": 210, "description": "Burrata cremosa con tomates cherry y pesto", "orders": 520, "vegetarian": True},
        {"name": "Calamares Fritos", "price": 165, "description": "Calamares crujientes con salsa aioli y limón", "orders": 280, "vegetarian": False},
    ],
    "Pastas": [
        {"name": "Spaghetti Carbonara", "price": 195, "description": "Pasta con huevo, panceta, parmesano y pimienta negra", "orders": 680, "vegetarian": False},
        {"name": "Penne Arrabiata", "price": 165, "description": "Pasta con salsa de tomate picante y ajo", "orders": 310, "vegetarian": True},
        {"name": "Fettuccine Alfredo", "price": 180, "description": "Pasta cremosa con mantequilla y parmesano", "orders": 420, "vegetarian": True},
        {"name": "Lasagna della Nonna", "price": 220, "description": "Lasagna tradicional con boloñesa y bechamel", "orders": 590, "vegetarian": False},
        {"name": "Tagliatelle al Tartufo", "price": 285, "description": "Pasta fresca con crema de trufa negra", "orders": 195, "vegetarian": True},
    ],
    "Pizzas": [
        {"name": "Margherita Clásica", "price": 175, "description": "Tomate, mozzarella fresca y albahaca", "orders": 820, "vegetarian": True},
        {"name": "Prosciutto e Funghi", "price": 225, "description": "Jamón serrano, champiñones y mozzarella", "orders": 645, "vegetarian": False},
        {"name": "Quattro Formaggi", "price": 240, "description": "Mozzarella, gorgonzola, parmesano y brie", "orders": 480, "vegetarian": True},
        {"name": "Diavola", "price": 215, "description": "Salami picante, chiles y mozzarella", "orders": 390, "vegetarian": False},
        {"name": "Vegetariana", "price": 195, "description": "Pimientos, berenjenas, zucchini y aceitunas", "orders": 275, "vegetarian": True},
    ],
    "Postres": [
        {"name": "Tiramisú Clásico", "price": 110, "description": "Con mascarpone, café y cacao", "orders": 560, "vegetarian": True},
        {"name": "Panna Cotta", "price": 95, "description": "Con coulis de frutos rojos", "orders": 320, "vegetarian": True},
        {"name": "Gelato Artesanal", "price": 85, "description": "3 sabores a elección: vainilla, chocolate, pistache", "orders": 410, "vegetarian": True},
    ],
    "Bebidas": [
        {"name": "Limonada Italiana", "price": 65, "description": "Limón, menta y agua mineral", "orders": 480, "vegetarian": True},
        {"name": "Agua Mineral", "price": 35, "description": "500ml", "orders": 300, "vegetarian": True},
        {"name": "Café Espresso", "price": 45, "description": "Grano 100% arábica", "orders": 390, "vegetarian": True},
        {"name": "Vino de la Casa", "price": 95, "description": "Copa de tinto o blanco", "orders": 520, "vegetarian": True},
    ]
}

# Platos más pedidos (calculado automáticamente)
def get_top_dishes(top_n=5):
    all_dishes = []
    for category, dishes in MENU.items():
        for dish in dishes:
            all_dishes.append({**dish, "category": category})
    return sorted(all_dishes, key=lambda x: x["orders"], reverse=True)[:top_n]

# Reservaciones en memoria (en producción usar base de datos)
reservations = []

def add_reservation(name: str, date: str, time: str, guests: int, phone: str, notes: str = ""):
    reservation = {
        "id": len(reservations) + 1,
        "name": name,
        "date": date,
        "time": time,
        "guests": guests,
        "phone": phone,
        "notes": notes,
        "status": "confirmada"
    }
    reservations.append(reservation)
    return reservation

def get_reservations():
    return reservations
