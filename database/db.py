import pandas as pd
from sqlalchemy import create_engine, text
from config.settings import DB_URL
from utils.logger import logger

engine = create_engine(DB_URL, pool_size=5, max_overflow=10, pool_pre_ping=True)


def get_engine():
    return engine


def get_connection():
    return engine.connect()


def execute_sql(sql: str, params: dict = None):
    with engine.connect() as conn:
        result = conn.execute(text(sql), params or {})
        conn.commit()
        return result


def read_sql(query: str, params: dict = None) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def write_df(df: pd.DataFrame, table: str, if_exists: str = "append"):
    df.to_sql(table, engine, if_exists=if_exists, index=False, method="multi")


def upsert_candles(df: pd.DataFrame, table: str = "minute_candles"):
    """Insert candles, ignoring rows that already exist (by timestamp+symbol)."""
    if df.empty:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import Table, MetaData
    meta = MetaData()
    meta.reflect(bind=engine, only=[table])
    tbl = meta.tables[table]
    rows = df.to_dict(orient="records")
    inserted = 0
    with engine.begin() as conn:
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start:chunk_start + 500]
            stmt = pg_insert(tbl).values(chunk).on_conflict_do_nothing(
                index_elements=["timestamp", "symbol"]
            )
            result = conn.execute(stmt)
            inserted += result.rowcount
    return inserted


def init_db():
    """
    Run schema.sql to create all tables (and, if available, TimescaleDB
    hypertables). Each statement runs in and commits its own transaction —
    NOT one transaction for the whole file — because Postgres aborts an
    entire transaction on the first error and refuses every statement after
    it until a rollback. Doing this in one shared transaction (the previous
    behaviour) meant a single failed statement silently discarded every
    table this call was supposed to create, including ones that appeared to
    run cleanly before it.

    TimescaleDB is optional, not required: if the `timescaledb` extension
    isn't installed on this Postgres server, `create_hypertable(...)` calls
    fail and are skipped — the preceding `CREATE TABLE IF NOT EXISTS` for
    that same table already succeeded as a plain table in its own
    transaction, which is all this app actually needs to function.
    """
    import os
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r") as f:
        sql = f.read()

    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            conn.commit()
            logger.info("TimescaleDB extension enabled — tables below will be hypertables.")
        except Exception:
            conn.rollback()
            logger.warning(
                "TimescaleDB extension not available on this Postgres server — "
                "continuing with plain tables (fully functional, just without "
                "hypertable partitioning/compression)."
            )

        created, skipped = 0, 0
        for statement in sql.split(";"):
            stmt = statement.strip()
            if not stmt:
                continue
            try:
                conn.execute(text(stmt))
                conn.commit()
                created += 1
            except Exception as e:
                conn.rollback()  # clear the aborted-transaction state for the NEXT statement
                skipped += 1
                if "create_hypertable" in stmt.lower():
                    logger.debug(f"Hypertable conversion skipped (TimescaleDB unavailable): {e}")
                else:
                    logger.warning(f"Schema statement failed (not a hypertable call — check this one): {e}")

    logger.info(f"Database schema initialized: {created} statements applied, {skipped} skipped.")