🚀 Comandos y Entorno
Server: uvicorn app.main:app --reload --port 8000

Tests: pytest | pytest tests/test_file.py -v

Deploy: Railway (4 workers, uvloop, sin estado compartido)

Variables Críticas: DATABASE_URL, ANTHROPIC_API_KEY, META_APP_SECRET, ADMIN_KEY

🏗️ Arquitectura de Capas (Estricta)
Routes (app/routes/): Solo validación HTTP y retorno de respuestas. NO lógica de negocio. NO SQL.

Services (app/services/): Orquestación, APIs externas (Claude, Wompi), lógica compleja. Retorna dicts/lists.

Database (app/services/database.py): Único lugar para SQL. asyncpg puro. CERO ORMs.

🔒 Reglas de Seguridad y Datos
SQL: PROHIBIDO f-strings. USAR siempre parámetros ($1, $2).

Auth: Tokens de 72h. Passwords con bcrypt. Usuarios (Email/Pass) vs Staff (Nombre/PIN).

XSS: En JS usar textContent, nunca innerHTML.

DB: Pool máx 20 conexiones. JSONB se auto-codifica.

🧠 Lógica de Negocio y AI
Zonas Horarias: El backend confía ciegamente en tz_offset (minutos) enviado por el frontend.

Multi-tenancy: Identificación por bot_number. Configuración en columna features (JSONB).

Modos IA:

Salón: Con table_context, permite acción order.

Externo: Embudo estricto (Catálogo -> Modalidad -> Datos -> Pago).

Dashboard: Todas las consultas de filtros retornan: (branch_id, bot_number, start_date, end_date).

👥 Staff, Turnos y Propinas (Fase 6)
Staff: Tabla independiente de users. Operativos usan PIN.

Turnos: Una sola fila activa por staff_id (índice único donde clock_out IS NULL).

Propinas: 10% voluntario en table_checks.tip_amount. No es base gravable para factura DIAN.

Reparto: Pool calculado por período y distribuido porcentualmente por rol (Mesero/Cocina/Bar).

🎨 Frontend (Vanilla JS)
Formateo de moneda con Intl.NumberFormat usando rb_restaurant de localStorage.

Rutas por rol: mesero.html, caja.html, kitchen.html, domiciliario.html, bar.html, dashboard.html.

Instrucciones para Claude Code:
NO leas nada en la carpeta docs/.

NO expliques cambios.

Ve directo al archivo mencionado.

Valida siempre que el servidor arranque tras modificar rutas o modelos.