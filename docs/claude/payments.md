# Pagos: Wompi per-restaurant (webhook roto) y Bold

> Movido verbatim desde CLAUDE.md (2026-09-12) para no cargarlo en cada turno.

## Configuración Wompi per-restaurant

Cada restaurante guarda sus propias credenciales Wompi en `organizations.features.wompi`:

```json
{
  "wompi": {
    "public_key": "pub_test_... o pub_prod_...",
    "integrity_secret": "test_integrity_... o prod_integrity_..."
  }
}
```

**Por qué per-restaurant:** cada cliente tiene su propia cuenta Wompi (su comercio, su NIT, su dispersion bancaria). No tiene sentido que toda la plataforma cobre a la cuenta de Mesio. Multi-tenant SaaS: cada tenant configura sus propias llaves.

**Cómo se configura:** admin/owner abre `/settings → Métodos de pago → Datáfono Wompi`, activa el toggle Wompi y aparece el bloque de credenciales. Pega la `public_key` y la `integrity_secret` desde su dashboard Wompi (Configuración → Llaves API). Guarda.

**Seguridad del secret:**
- `GET /api/settings` retorna `wompi.integrity_secret_set: bool` y `wompi.integrity_secret_last4: str` — el plaintext NUNCA sale del servidor.
- En el form, el input es type="password" con placeholder `•••• •••• •••• {last4}`. El admin solo ve el plaintext en el momento de tipearlo.
- `POST /api/settings`: si el `integrity_secret` viene vacío o como string masked (`xxxx_*`), el backend preserva el valor existente (admin puede actualizar solo la `public_key` sin re-tipear el secret). Si viene un valor distinto, lo reemplaza.

**Resolución en runtime:**
- `app/services/orders.py::_wompi_credentials_from_restaurant(restaurant)` extrae el par `(pk, integrity_secret)` desde `restaurant.features.wompi`. Acepta `features` como dict o JSON string (asyncpg variabilidad).
- `generate_wompi_payment_link(order_id, amount, currency, public_key=None, integrity_secret=None)`: si los kwargs vienen seteados los usa; si vienen None, cae a las env vars `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` (fallback legacy). Cada credencial se evalúa de forma independiente.
- `generate_deposit_link(reservation_id, amount, currency, restaurant=None)`: mismo patrón para depósitos de reserva.
- Si ni el restaurant config ni env vars proveen `integrity_secret` → `RuntimeError`. Los call sites del bot (`create_order`, `agent.py reserve flow`) capturan ese error y caen al flujo de comprobante manual (Nequi/Bancolombia).

**Migración:** las env vars `WOMPI_PUBLIC_KEY` y `WOMPI_INTEGRITY_SECRET` siguen siendo válidas como fallback durante la transición. Se pueden remover de Railway una vez que TODOS los restaurantes activos hayan configurado sus credenciales en `/settings`. Hasta entonces, los restaurantes sin config explícita seguirán cobrando a la cuenta global.

### ⚠️ Estado (2026-09-11): Wompi APAGADO al lanzamiento + webhook roto (P0 pendiente)

**Decisión PM:** Wompi queda OFF al inicio. El PM no tiene RUT para abrir cuenta (ni siquiera sandbox). Recordar que en el modelo per-restaurant **el comercio es cada restaurante** (su RUT, su cuenta, sus llaves), no Mesio — la falta de RUT del PM bloquea *probar*, no el producto. El cobro al lanzamiento es datáfono/efectivo vía mesero ("traer datáfono"). Bold es la alternativa en evaluación (muchos restaurantes ya tienen su portal/QR de Bold).

**Mientras Wompi esté OFF:** confirmar en Railway que `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` NO estén seteadas. Si lo están, el bot genera links de pago contra la cuenta global, el cliente paga, y el pago nunca se confirma (ver bug abajo) — el cliente pagó y el restaurante no se entera.

**Bug P0 del webhook — arreglar ANTES de reactivar Wompi.** `POST /payment/wompi-webhook` (`app/routes/orders_routes.py`) nunca puede verificar un evento real de Wompi:
1. **Fórmula equivocada.** El código calcula `sha256(cuerpo_crudo + WOMPI_EVENTS_SECRET)`. Wompi firma con SHA256 de: los valores de los campos listados en `signature.properties` (rutas tipo `transaction.id` dentro de `data`, en ese orden) + `timestamp` + el **secreto de eventos** del comercio; se compara contra `signature.checksum` (también viene en el header `X-Event-Checksum`). Prueba lógica: el cuerpo del evento *contiene* `signature.checksum`, así que no puede ser el hash del cuerpo. Confirmado por la doc oficial (https://docs.wompi.co/en/docs/colombia/eventos/) y por una integración de terceros en producción.
2. **Secreto único global.** `WOMPI_EVENTS_SECRET` es una sola env var, pero el secreto de eventos es **por cuenta de comercio**. `features.wompi` solo guarda `public_key` + `integrity_secret`.
3. **Por qué los tests pasan:** `tests/e2e/test_delivery_wompi_callback_lifecycle.py` firma sus payloads con la misma fórmula equivocada — el código se prueba contra sí mismo.

**Especificación del arreglo:** implementar el algoritmo documentado iterando `signature.properties` del payload en cada evento (la doc advierte que las propiedades cambian: NUNCA fijar la lista en el código); agregar `events_secret` a `features.wompi` (enmascarado en `GET /api/settings` igual que `integrity_secret`); resolver el restaurante del evento vía `data.transaction.reference` → pedido / check de mesa / depósito `dep_` → org, verificar con el secreto de ese org y caer al env global solo como fallback; no actuar sobre el payload antes de verificar; reescribir el firmador del e2e con el algoritmo real. **El ejemplo resuelto de la doc NO sirve como test ancla**: su checksum (`3476DDA5…`) es decorativo — la concatenación documentada da `5A18EC5E…` y ningún orden alternativo lo reproduce. El ancla real es capturar un evento de una cuenta sandbox (requiere RUT de algún comercio de prueba).

### Bold — evaluado 2026-09-11, DIFERIDO (primera pasarela a integrar cuando se active el cobro con pasarela)

**Decisión PM:** al lanzamiento el cobro de mesa va por el mesero, SIN pasarela (el comensal elige en el chat "lo mío / toda la mesa" y "tarjeta / efectivo"; el mesero recibe el monto exacto, cobra en el datáfono que tenga el restaurante y caja marca pagado vía `pay_check`). Bold es la pasarela prevista para después porque muchos restaurantes ya tienen sus datáfonos/QR. Igual que Wompi: el comercio es cada restaurante con su propia cuenta y llaves.

- **Llaves** (por comercio, versión pruebas y producción): "llave de identidad" (pública, header `Authorization: x-api-key <llave_de_identidad>`) y "llave secreta" (privada). Panel bold.co → Integraciones → Llaves de integración. Las llaves de PRUEBA solo aparecen después de que Bold aprueba una solicitud; no hay sandbox autoservicio, y la doc no publica si aceptan persona natural sin RUT.
- **Push al datáfono — el mejor encaje para "traer datáfono"** (https://developers.bold.co/api-integrations/integration): base `https://integrations.api.bold.co`; `GET /payments/payment-methods`, `GET /payments/binded-terminals`, `POST /payments/app-checkout` con `amount{currency,total_amount,taxes,tip_amount}`, `payment_method`, `terminal_model`, `terminal_serial`, `reference`, `user_email`; resultado por webhook. Solo datáfonos Smart / Smart Pro habilitados en "Conexiones API". El sandbox exige un SmartPro físico y simula resultados por monto (111.111 fondos insuficientes, 222.222 PIN inválido, 999.999 rechazo general).
- **Link de pago + QR Bre-B** (https://developers.bold.co/pagos-en-linea/api-link-de-pagos): `POST /online/link/v1` (`amount_type` CLOSE, `amount`, `reference` ≤60, `expiration_date` en nanosegundos Unix, `callback_url`) → `payment_link` + `url`. `GET /online/link/v1/{payment_link}` da el `status` (ACTIVE/PROCESSING/PAID/REJECTED/CANCELLED/EXPIRED): sirve de respaldo si se pierde un webhook.
- **Webhooks** (https://developers.bold.co/webhook): header `x-bold-signature` = HMAC-SHA256 hex del cuerpo crudo **codificado en Base64**, con la llave secreta del comercio; en modo pruebas la llave es VACÍA. Eventos SALE_APPROVED / SALE_REJECTED / VOID_APPROVED / VOID_REJECTED; identificar el pago por `metadata.reference`. Resolver el restaurante desde la referencia antes de verificar y no actuar sobre nada sin verificar. Lección de Wompi: fijar como test ancla un evento REAL capturado del sandbox, nunca solo firmas generadas por nuestro propio código.

