# 🍽️ Proyecto Mesio - Mapa de Arquitectura para IA

**Descripción del Proyecto:** Mesio es un bot y plataforma backend basada en Inteligencia Artificial para la gestión de restaurantes en Colombia. Maneja toma de pedidos por WhatsApp, atención al cliente con IA, facturación electrónica, sistema CRM, reservas y control de mesas.
**Stack Tecnológico:** Python, FastAPI, PostgreSQL (asyncpg), Integración con Meta (WhatsApp API) / Twilio, Pasarela Wompi. No se usa ORM (SQLDb directo).

## 📂 Estructura de Directorios y Archivos Principales

### 1. Raíz del Proyecto
* `requirements.txt`: Dependencias de Python.
* `railway.toml` / `Procfile`: Archivos de configuración de despliegue en Railway/Heroku.

### 2. `app/` (Código Principal)
* `main.py`: Punto de entrada de la aplicación. Configura FastAPI, Middlewares, CORS, inicializa la base de datos, tareas programadas (scheduler) y monta todas las rutas (routers) de la API.

#### 2.1 `app/routes/` (Endpoints y Controladores)
Aquí están las rutas a las que accede el frontend o servicios externos.
* `chat.py`: Maneja los Webhooks de Meta y Twilio para recibir mensajes de WhatsApp. Incluye *rate limiting*, verificación de firmas y envío de la respuesta al usuario en segundo plano usando IA.
* `crm.py`: Sistema CRM de ventas/prospectos. Rutas para CRUD de prospectos, añadir notas, subir archivos CSV, mover etapas (kanban) y envíos masivos/manuales de plantillas de WhatsApp (Templates Meta).
* `dashboard.py`: Maneja el inicio de sesión, creación de restaurantes/sucursales, gestión de usuarios/equipo, renderizado de las vistas HTML (`/dashboard`, `/crm`, etc.) y procesamiento de menús en PDF o Imagen.
* `billing.py`: Endpoints para configurar credenciales y emitir facturas electrónicas con proveedores (Siigo, Alegra, Loggro).
* `orders.py`: Gestión de pedidos (órdenes), vista de carritos y webhook de Wompi para confirmación de pagos.
* `tables.py`: Gestión de pedidos en la mesa del restaurante (flujo en sitio).
* `stats.py`: Endpoints para estadísticas y métricas del restaurante.

#### 2.2 `app/services/` (Lógica de Negocio y Base de Datos)
Aquí reside la inteligencia y la conexión de datos de la aplicación.
* `database.py`: ¡Archivo crucial! Contiene el pool de conexiones asíncronas (`asyncpg`) y **TODAS** las consultas SQL crudas del sistema (restaurantes, órdenes, mesas, CRM, reservas, usuarios, sesiones).
* `agent.py`: Conecta con los modelos de IA (Anthropic/Claude o OpenAI) para procesar el lenguaje natural y generar las respuestas del bot.
* `auth.py`: Lógica de seguridad, hashing de contraseñas y creación/verificación de tokens JWT.
* `billing.py`: Clientes HTTP y lógica dura para conectarse a las APIs de Siigo, Alegra y Loggro.
* `orders.py`: Funciones auxiliares para calcular subtotales de carritos y limpiarlos.
* `scheduler.py`: Tareas recurrentes en segundo plano (ej. limpieza de base de datos, recordatorios).

#### 2.3 `app/static/` (Frontend)
* Contiene los archivos HTML, CSS y JS de la interfaz de usuario: `dashboard.html`, `crm.html`, `mesero.html`, `caja.html`, `billing.html`, `login.html`, etc.

#### 2.4 Directorios Auxiliares
* `app/migrations/`: Scripts puntuales para añadir columnas o tablas nuevas a la base de datos (ej. `crm_migrations.py`).
* `tests/`: Pruebas automatizadas del sistema (`conftest.py`, `test_billing.py`, etc.).