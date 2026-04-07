"""
Mesio repository layer — thin transactional wrappers over asyncpg.

Exports:
    InsufficientStockError
    OrderCommitError
    commit_order_transaction  (delivery / pickup orders)

Sub-modules:
    orders_repo    — delivery/pickup order transactions (Fase 1)
    sessions_repo  — table session management (Fase 4)
    inventory_repo — inventory, stock deduction, escandallos (Fase 6)
"""

from app.repositories.orders_repo import (
    InsufficientStockError,
    OrderCommitError,
    commit_order_transaction,
)

__all__ = [
    "InsufficientStockError",
    "OrderCommitError",
    "commit_order_transaction",
]
