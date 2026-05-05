# Post-migration Monitoring Queries — Wave 2 (Org/Location)

> **Nota:** Estas queries se usaron durante la migración Wave-2 (0034-0038, abril 2026). La migración está **completada y validada**. Las queries se mantienen como referencia defensiva: si en algún momento sospechás drift de schema o leak cross-tenant, son verificaciones útiles. **En condiciones normales, todas deben retornar 0 filas.**

Estas queries se corren en psql (con `DATABASE_URL_ADMIN`) para verificar el estado de la migración Org/Location.

## Query 1 — Orphan org_id check (DEBE retornar 0)

```sql
SELECT 'attendance_deductions' AS tbl, COUNT(*) AS nulls FROM attendance_deductions WHERE org_id IS NULL UNION ALL
SELECT 'billing_log',           COUNT(*) FROM billing_log           WHERE org_id IS NULL UNION ALL
SELECT 'carts',                 COUNT(*) FROM carts                 WHERE org_id IS NULL UNION ALL
SELECT 'contract_templates',    COUNT(*) FROM contract_templates    WHERE org_id IS NULL UNION ALL
SELECT 'conversations',         COUNT(*) FROM conversations         WHERE org_id IS NULL UNION ALL
SELECT 'customer_profiles',     COUNT(*) FROM customer_profiles     WHERE org_id IS NULL UNION ALL
SELECT 'dish_recipes',          COUNT(*) FROM dish_recipes          WHERE org_id IS NULL UNION ALL
SELECT 'fiscal_invoices',       COUNT(*) FROM fiscal_invoices       WHERE org_id IS NULL UNION ALL
SELECT 'fiscal_resolution',     COUNT(*) FROM fiscal_resolution     WHERE org_id IS NULL UNION ALL
SELECT 'inventory',             COUNT(*) FROM inventory             WHERE org_id IS NULL UNION ALL
SELECT 'loyalty_customers',     COUNT(*) FROM loyalty_customers     WHERE org_id IS NULL UNION ALL
SELECT 'loyalty_ledger',        COUNT(*) FROM loyalty_ledger        WHERE org_id IS NULL UNION ALL
SELECT 'marketing_messages_log',COUNT(*) FROM marketing_messages_log WHERE org_id IS NULL UNION ALL
SELECT 'menu_availability',     COUNT(*) FROM menu_availability     WHERE org_id IS NULL UNION ALL
SELECT 'menu_events',           COUNT(*) FROM menu_events           WHERE org_id IS NULL UNION ALL
SELECT 'nps_responses',         COUNT(*) FROM nps_responses         WHERE org_id IS NULL UNION ALL
SELECT 'nps_waiting',           COUNT(*) FROM nps_waiting           WHERE org_id IS NULL UNION ALL
SELECT 'occupancy_snapshots',   COUNT(*) FROM occupancy_snapshots   WHERE org_id IS NULL UNION ALL
SELECT 'orders',                COUNT(*) FROM orders                WHERE org_id IS NULL UNION ALL
SELECT 'overtime_requests',     COUNT(*) FROM overtime_requests     WHERE org_id IS NULL UNION ALL
SELECT 'payroll_runs',          COUNT(*) FROM payroll_runs          WHERE org_id IS NULL UNION ALL
SELECT 'staff',                 COUNT(*) FROM staff                 WHERE org_id IS NULL UNION ALL
SELECT 'staff_deduction_items', COUNT(*) FROM staff_deduction_items WHERE org_id IS NULL UNION ALL
SELECT 'staff_schedules',       COUNT(*) FROM staff_schedules       WHERE org_id IS NULL UNION ALL
SELECT 'staff_shifts',          COUNT(*) FROM staff_shifts          WHERE org_id IS NULL UNION ALL
SELECT 'subscription_usage',    COUNT(*) FROM subscription_usage    WHERE org_id IS NULL UNION ALL
SELECT 'table_orders',          COUNT(*) FROM table_orders          WHERE org_id IS NULL UNION ALL
SELECT 'table_sessions',        COUNT(*) FROM table_sessions        WHERE org_id IS NULL UNION ALL
SELECT 'time_slot_discounts',   COUNT(*) FROM time_slot_discounts   WHERE org_id IS NULL UNION ALL
SELECT 'waiter_alerts',         COUNT(*) FROM waiter_alerts         WHERE org_id IS NULL UNION ALL
SELECT 'webauthn_challenges',   COUNT(*) FROM webauthn_challenges   WHERE org_id IS NULL UNION ALL
SELECT 'weekly_reports',        COUNT(*) FROM weekly_reports        WHERE org_id IS NULL
ORDER BY nulls DESC;
```

## Query 2 — Primary location invariant (DEBE retornar 0)

```sql
SELECT org_id, COUNT(*) AS primary_count
FROM locations
WHERE is_primary = true
GROUP BY org_id
HAVING COUNT(*) <> 1;
```

## Query 3 — Verify no auto-populate triggers (DEBE retornar 0)

```sql
SELECT event_object_table AS table_name, trigger_name
FROM information_schema.triggers
WHERE trigger_name LIKE 'trg_auto_org_location_%'
  AND trigger_schema = 'public';
```

## Query 4 — Verify no legacy restaurant_id columns

```sql
SELECT table_name, column_name
FROM information_schema.columns
WHERE column_name  = 'restaurant_id'
  AND table_schema = 'public'
ORDER BY table_name;
-- EXPECTED post-0038: 0 rows.
```

## Query 5 — Verify no tenant_isolation policies (DEBE retornar 0)

```sql
SELECT tablename, policyname
FROM pg_policies
WHERE policyname  = 'tenant_isolation'
  AND schemaname  = 'public';
```

## Query 6 — Cross-org leak test (under mesio_app role)

```sql
SET LOCAL ROLE mesio_app;
SELECT set_config('app.org_id', '1', true);   -- replace 1 with a real org_id
SELECT COUNT(*) FROM orders;                   -- should return only org 1's orders
SELECT COUNT(*) FROM orders WHERE org_id <> 1; -- MUST be 0
```

## Alerta continua (recomendada)

```bash
# Cron horario con psql
psql $DATABASE_URL_ADMIN -c "
  SELECT SUM(nulls) FROM (
    SELECT COUNT(*) AS nulls FROM orders WHERE org_id IS NULL
    UNION ALL
    SELECT COUNT(*) FROM staff WHERE org_id IS NULL
  ) sub
" | grep -v "^0$" && curl -X POST $ALERT_WEBHOOK_URL \
  -d '{"text":"[CRITICAL] org_id IS NULL detected in production tables"}'
```

O agregar un check custom en `services/alerts.py`.
