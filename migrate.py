import argparse
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/hairsalon")
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PostgreSQL migrations runner")
    parser.add_argument(
        "--to",
        dest="target",
        default=None,
        help="Target version, e.g. 0001_init_schema",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned actions without changing DB",
    )
    return parser.parse_args()


def ensure_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    conn.commit()


def applied_versions(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations ORDER BY version")
        rows = cur.fetchall()
    return [row["version"] for row in rows]


def apply_migration(conn, version: str, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (version,))
    conn.commit()


def rollback_migration(conn, version: str, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
    conn.commit()


def discover_migrations(migrations_dir: Path) -> dict[str, dict[str, Path]]:
    migrations: dict[str, dict[str, Path]] = {}
    for path in migrations_dir.glob("*.sql"):
        name = path.name
        if name.endswith(".down.sql"):
            version = name.removesuffix(".down.sql")
            migrations.setdefault(version, {})["down"] = path
            continue
        if name.endswith(".up.sql"):
            version = name.removesuffix(".up.sql")
            migrations.setdefault(version, {})["up"] = path
            continue
        # Backward compatibility: plain .sql treated as "up"
        version = path.stem
        if not version.endswith(".down") and not version.endswith(".up"):
            migrations.setdefault(version, {})["up"] = path
    return dict(sorted(migrations.items(), key=lambda item: item[0]))


def build_plan(
    all_versions: list[str], applied: list[str], target: str | None
) -> list[tuple[str, str]]:
    """
    Return list of actions:
    - ("up", version)
    - ("down", version)
    """
    applied_set = set(applied)
    if target is None:
        return [("up", v) for v in all_versions if v not in applied_set]

    if target not in all_versions:
        raise RuntimeError(f"Целевая версия '{target}' не найдена в migrations.")

    target_idx = all_versions.index(target)
    target_set = set(all_versions[: target_idx + 1])

    current_versions = [v for v in all_versions if v in applied_set]
    if set(current_versions) == target_set:
        return []

    if len(current_versions) <= len(target_set):
        return [("up", v) for v in all_versions if v in target_set and v not in applied_set]

    to_rollback = [v for v in current_versions if v not in target_set]
    to_rollback.reverse()
    return [("down", v) for v in to_rollback]


def main() -> None:
    args = parse_args()
    if not DATABASE_URL:
        raise RuntimeError("Не указан DATABASE_URL в переменных окружения.")
    if not MIGRATIONS_DIR.exists():
        raise RuntimeError("Папка migrations не найдена.")

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    try:
        ensure_migrations_table(conn)
        done = applied_versions(conn)
        migration_map = discover_migrations(MIGRATIONS_DIR)
        if not migration_map:
            print("Миграции не найдены.")
            return

        versions = list(migration_map.keys())
        plan = build_plan(versions, done, args.target)
        if not plan:
            print("Нечего применять/откатывать.")
            return

        for direction, version in plan:
            paths = migration_map[version]
            if direction == "up":
                if "up" not in paths:
                    raise RuntimeError(f"Для версии '{version}' не найден up-скрипт.")
                if args.dry_run:
                    print(f"PLAN UP {version} <- {paths['up'].name}")
                    continue
                sql = paths["up"].read_text(encoding="utf-8")
                apply_migration(conn, version, sql)
                print(f"APPLIED {version}")
            else:
                if "down" not in paths:
                    raise RuntimeError(
                        f"Для версии '{version}' не найден down-скрипт. "
                        f"Откат невозможен."
                    )
                if args.dry_run:
                    print(f"PLAN DOWN {version} <- {paths['down'].name}")
                    continue
                sql = paths["down"].read_text(encoding="utf-8")
                rollback_migration(conn, version, sql)
                print(f"ROLLED BACK {version}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
