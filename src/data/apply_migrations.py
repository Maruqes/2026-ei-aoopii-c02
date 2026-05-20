from __future__ import annotations

import argparse
import os
from pathlib import Path

from repository import connect


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def apply_migrations(database_url: str) -> None:
    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migrations:
        raise RuntimeError(f"No migrations found in {MIGRATIONS_DIR}")

    conn = connect(database_url)
    try:
        cur = conn.cursor()
        for migration in migrations:
            sql = migration.read_text(encoding="utf-8").strip()
            if sql:
                cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply Postgres SQL migrations.")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Postgres connection URL. Defaults to DATABASE_URL.",
    )
    args = parser.parse_args()

    if not args.database_url:
        raise SystemExit("DATABASE_URL is required")

    apply_migrations(args.database_url)


if __name__ == "__main__":
    main()
