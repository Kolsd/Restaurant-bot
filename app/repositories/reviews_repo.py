"""
Reviews repository — public review management extending NPS responses.

Covers:
  - Public review listing and toggling
  - Owner reply management
  - Review summary aggregation
  - Occupancy snapshot persistence and querying
  - Table turn time statistics
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessors — break circular import with app.services.database.
def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


async def db_verify_review_ownership(nps_id: int, bot_number: str) -> bool:
    """Return True if the NPS response exists and belongs to this bot_number."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM nps_responses WHERE id = $1 AND bot_number = $2",
            nps_id, bot_number,
        )
    return row is not None


async def db_verify_review_ownership_by_org(nps_id: int) -> bool:
    """Return True if the NPS response exists within the current tenant scope.

    Wave-2: RLS scopes the query to the caller's org_id — no need to pass
    org_id explicitly.  This replaces the bot_number check for multi-location
    orgs where branch reviews have a different bot_number than the matriz.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM nps_responses WHERE id = $1",
            nps_id,
        )
    return row is not None


async def db_get_public_reviews(bot_number: str, limit: int = 50) -> list[dict]:
    """Return public reviews for a restaurant ordered by most recent."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, phone, score, comment, feedback, is_public,
                   customer_name, owner_reply, owner_reply_at, created_at
            FROM nps_responses
            WHERE is_public = TRUE AND bot_number = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            bot_number,
            limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_public_reviews_by_org(limit: int = 50) -> list[dict]:
    """Return public reviews for the current tenant org (all locations).

    Wave-2: RLS scopes the query to the caller's org_id — reviews from all
    sedes of the org are visible to any admin of that org.
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, phone, score, comment, feedback, is_public,
                   customer_name, owner_reply, owner_reply_at, created_at
            FROM nps_responses
            WHERE is_public = TRUE
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_review_summary_by_org() -> dict:
    """Aggregate review stats across all locations of the current tenant org.

    Wave-2: replaces the bot_number-filtered db_get_review_summary for the
    dashboard reviews page so that the matriz admin sees all-org stats.
    """
    async with _tenant_connection() as conn:
        summary_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_reviews,
                COALESCE(AVG(score), 0)::float AS avg_score
            FROM nps_responses
            WHERE is_public = TRUE
            """,
        )
        dist_rows = await conn.fetch(
            """
            SELECT score, COUNT(*)::int AS cnt
            FROM nps_responses
            WHERE is_public = TRUE
            GROUP BY score
            ORDER BY score
            """,
        )

    distribution = {str(i): 0 for i in range(1, 6)}
    for r in dist_rows:
        distribution[str(r["score"])] = r["cnt"]

    return {
        "total_reviews": summary_row["total_reviews"] if summary_row else 0,
        "avg_score": round(summary_row["avg_score"], 2) if summary_row else 0.0,
        "score_distribution": distribution,
    }


async def db_set_review_public(
    nps_id: int,
    is_public: bool = True,
    customer_name: str = "",
) -> dict | None:
    """Toggle public visibility and optionally set a display name for the review."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE nps_responses
            SET is_public = $1, customer_name = $2
            WHERE id = $3
            RETURNING *
            """,
            is_public,
            customer_name,
            nps_id,
        )
    return _serialize(dict(row)) if row else None


async def db_add_owner_reply(nps_id: int, reply_text: str) -> dict | None:
    """Save owner reply to a review with timestamp."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE nps_responses
            SET owner_reply = $1, owner_reply_at = NOW()
            WHERE id = $2
            RETURNING *
            """,
            reply_text,
            nps_id,
        )
    return _serialize(dict(row)) if row else None


async def db_get_review(nps_id: int) -> dict | None:
    """Fetch a review row by id (RLS-scoped to the current tenant)."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM nps_responses WHERE id = $1",
            nps_id,
        )
    return _serialize(dict(row)) if row else None


async def db_mark_review_reply_delivered(nps_id: int) -> bool:
    """Mark the WhatsApp delivery of the owner reply as completed.

    Sets owner_reply_delivered_at = NOW(). Returns True if a row was updated.
    Tenant-scoped via RLS.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE nps_responses
            SET owner_reply_delivered_at = NOW()
            WHERE id = $1
            RETURNING id
            """,
            nps_id,
        )
    return row is not None


async def db_get_review_summary(bot_number: str) -> dict:
    """
    Aggregate stats for all public reviews of a restaurant:
    total_reviews, avg_score, score_distribution (count per score 1-5).
    """
    async with _tenant_connection() as conn:
        summary_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_reviews,
                COALESCE(AVG(score), 0)::float AS avg_score
            FROM nps_responses
            WHERE is_public = TRUE AND bot_number = $1
            """,
            bot_number,
        )
        dist_rows = await conn.fetch(
            """
            SELECT score, COUNT(*)::int AS cnt
            FROM nps_responses
            WHERE is_public = TRUE AND bot_number = $1
            GROUP BY score
            ORDER BY score
            """,
            bot_number,
        )

    distribution = {str(i): 0 for i in range(1, 6)}
    for r in dist_rows:
        distribution[str(r["score"])] = r["cnt"]

    return {
        "total_reviews": summary_row["total_reviews"] if summary_row else 0,
        "avg_score": round(summary_row["avg_score"], 2) if summary_row else 0.0,
        "score_distribution": distribution,
    }


async def db_save_occupancy_snapshot(
    restaurant_id: int,
    branch_id: Optional[int],
    total_tables: int,
    occupied_tables: int,
    total_capacity: int,
    seated_guests: int,
) -> None:
    """Persist a point-in-time occupancy snapshot."""
    async with _tenant_connection() as conn:
        await conn.execute(
            """
            INSERT INTO occupancy_snapshots
                (org_id, branch_id, total_tables, occupied_tables,
                 total_capacity, seated_guests)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            restaurant_id,
            branch_id,
            total_tables,
            occupied_tables,
            total_capacity,
            seated_guests,
        )


async def db_get_occupancy_stats(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    branch_id: Optional[int] = None,
) -> dict:
    """
    Aggregate occupancy stats over a time window.
    Returns avg_occupancy_rate (occupied/total), avg_utilization (seated/capacity),
    and snapshot_count.
    """
    async with _tenant_connection() as conn:
        if branch_id is not None:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS snapshot_count,
                    COALESCE(AVG(
                        CASE WHEN total_tables > 0
                             THEN occupied_tables::float / total_tables
                             ELSE 0 END
                    ), 0)::float AS avg_occupancy_rate,
                    COALESCE(AVG(
                        CASE WHEN total_capacity > 0
                             THEN seated_guests::float / total_capacity
                             ELSE 0 END
                    ), 0)::float AS avg_utilization
                FROM occupancy_snapshots
                WHERE org_id = $1
                  AND branch_id = $2
                  AND snapshot_at >= $3::timestamptz
                  AND snapshot_at <= ($4::date + interval '1 day')::timestamptz
                """,
                restaurant_id,
                branch_id,
                period_start,
                period_end,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS snapshot_count,
                    COALESCE(AVG(
                        CASE WHEN total_tables > 0
                             THEN occupied_tables::float / total_tables
                             ELSE 0 END
                    ), 0)::float AS avg_occupancy_rate,
                    COALESCE(AVG(
                        CASE WHEN total_capacity > 0
                             THEN seated_guests::float / total_capacity
                             ELSE 0 END
                    ), 0)::float AS avg_utilization
                FROM occupancy_snapshots
                WHERE org_id = $1
                  AND snapshot_at >= $2::timestamptz
                  AND snapshot_at <= ($3::date + interval '1 day')::timestamptz
                """,
                restaurant_id,
                period_start,
                period_end,
            )

    if not row:
        return {"snapshot_count": 0, "avg_occupancy_rate": 0.0, "avg_utilization": 0.0}

    return {
        "snapshot_count": row["snapshot_count"],
        "avg_occupancy_rate": round(row["avg_occupancy_rate"] * 100, 1),  # as percentage
        "avg_utilization": round(row["avg_utilization"] * 100, 1),        # as percentage
    }


async def db_get_turn_time_stats(
    bot_number: str,
    period_start: str,
    period_end: str,
    branch_id: Optional[int] = None,
) -> dict:
    """
    Calculate table turn time statistics from closed table sessions.
    Returns avg_minutes, min_minutes, max_minutes, and session_count.
    """
    async with _tenant_connection() as conn:
        if branch_id is not None:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS session_count,
                    COALESCE(AVG(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS avg_minutes,
                    COALESCE(MIN(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS min_minutes,
                    COALESCE(MAX(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS max_minutes
                FROM table_sessions ts
                JOIN restaurant_tables rt ON ts.table_id = rt.id
                WHERE ts.bot_number = $1
                  AND ts.closed_at IS NOT NULL
                  AND ts.started_at >= $2::timestamptz
                  AND ts.started_at <= ($3::date + interval '1 day')::timestamptz
                  AND rt.branch_id = $4
                """,
                bot_number,
                period_start,
                period_end,
                branch_id,
            )
        else:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)::int AS session_count,
                    COALESCE(AVG(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS avg_minutes,
                    COALESCE(MIN(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS min_minutes,
                    COALESCE(MAX(
                        EXTRACT(EPOCH FROM (ts.closed_at - ts.started_at)) / 60
                    ), 0)::float AS max_minutes
                FROM table_sessions ts
                WHERE ts.bot_number = $1
                  AND ts.closed_at IS NOT NULL
                  AND ts.started_at >= $2::timestamptz
                  AND ts.started_at <= ($3::date + interval '1 day')::timestamptz
                """,
                bot_number,
                period_start,
                period_end,
            )

    if not row:
        return {"session_count": 0, "avg_minutes": 0.0, "min_minutes": 0.0, "max_minutes": 0.0}

    return {
        "session_count": row["session_count"],
        "avg_minutes": round(row["avg_minutes"], 1),
        "min_minutes": round(row["min_minutes"], 1),
        "max_minutes": round(row["max_minutes"], 1),
    }
