"""Read-only resident-memory footprint report for the long-term memory engine.

Why this exists
---------------
The engine's resident cost is dominated by two things that are invisible in a
character-budget report:

* the **on-disk** store (one packed float32 vector per fact), and
* the **in-memory** copy the resident recall service keeps (``read_view`` copies
  the source database into ``:memory:``).

A vector is stored **twice**: once as a ``fact_embeddings`` BLOB and once inside
the ``vec_facts`` shadow chunk table. That duplication is what makes the store
grow at roughly twice the rate the fact count suggests, and it is why the
resident copy is large.

What this module does
---------------------
It *measures* and *classifies* - it never deletes, rewrites, or vacuums.
Reported items are counts and byte sizes only: no fact text, no vector values,
no file paths beyond the database the caller already named.

Output is deterministic for a given database so it can be used as a monitor
signal (unchanged output means "nothing to do", which is what a scheduled
guard needs).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Tables whose pages are attributed to vector storage. ``vec_facts`` is the
#: sqlite-vec virtual table; its payload lives in ``vec_facts_vector_chunks*``
#: shadow tables, which is why the index is charged separately from the BLOB
#: table that holds the authoritative vector.
VECTOR_BLOB_TABLE = 'fact_embeddings'
VECTOR_INDEX_PREFIX = 'vec_facts'


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_bytes(conn: sqlite3.Connection) -> Dict[str, int]:
    """Bytes per table/index, from SQLite's own ``dbstat`` virtual table.

    ``dbstat`` is a compile-time option; when it is unavailable we report an
    empty mapping rather than guessing sizes from row counts.
    """
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name").fetchall()
    except sqlite3.Error:
        return {}
    return {row[0]: int(row[1] or 0) for row in rows}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return int(conn.execute('SELECT COUNT(*) FROM "%s"' % table.replace('"', '""')).fetchone()[0])
    except sqlite3.Error:
        return 0


def _existing_tables(conn: sqlite3.Connection) -> List[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")]


def _bucket(mib: float) -> str:
    """Coarse size bucket - the unit a monitor can compare across runs.

    Exact byte counts change on every write; a bucket only changes when the
    footprint crosses a threshold, so a scheduled job stays quiet until it
    matters.
    """
    if mib <= 0:
        return '0'
    for edge in (64, 128, 256, 512, 1024, 2048, 4096):
        if mib <= edge:
            return '<=%d' % edge
    return '>4096'


def _dormant_vector_count(conn: sqlite3.Connection) -> int:
    """Vectors attached to facts that recall will never return.

    Recall filters on ``status = 'active'``. A vector whose fact is retired still
    occupies a packed slot in both the BLOB table and the vector index, and is
    copied into the resident view on every service start — so retired facts cost
    resident memory long after they stopped being answerable.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM fact_embeddings e LEFT JOIN working_fact f"
            " ON f.fact_id = e.fact_id WHERE f.fact_id IS NULL OR f.status <> 'active'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0] or 0)


def report(database: Path, *, view_budget_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Measure the store's footprint. Read-only; raises on an unreadable store."""
    path = Path(database).resolve(strict=True)
    conn = _connect(path)
    try:
        sizes = _table_bytes(conn)
        tables = set(_existing_tables(conn))
        blob_bytes = sizes.get(VECTOR_BLOB_TABLE, 0)
        index_bytes = sum(size for name, size in sizes.items()
                          if name.startswith(VECTOR_INDEX_PREFIX))
        vector_bytes = blob_bytes + index_bytes
        total_bytes = sum(sizes.values())
        facts = _row_count(conn, 'working_fact')
        embeddings = _row_count(conn, VECTOR_BLOB_TABLE) if VECTOR_BLOB_TABLE in tables else 0
        file_bytes = path.stat().st_size
        dormant = _dormant_vector_count(conn)
        view_fit = None
        if view_budget_bytes:
            view_fit = 'fits' if file_bytes <= view_budget_bytes else 'exceeds'
        return {
            'database_bytes': file_bytes,
            'database_mib_bucket': _bucket(file_bytes / 1024 / 1024),
            'allocated_bytes': total_bytes,
            'fact_count': facts,
            'embedding_count': embeddings,
            'vector_bytes': vector_bytes,
            'vector_bytes_per_embedding': (vector_bytes // embeddings) if embeddings else 0,
            'vector_storage_is_duplicated': bool(blob_bytes and index_bytes),
            'vector_share_of_store': (round(vector_bytes / total_bytes, 4) if total_bytes else 0.0),
            'resident_view_budget_bytes': view_budget_bytes,
            'resident_view_fit': view_fit,
            'dormant_embedding_count': dormant,
            'dormant_share_of_embeddings': (round(dormant / embeddings, 4) if embeddings else 0.0),
            'tables': dict(sorted(sizes.items(), key=lambda item: -item[1])),
        }
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--database', required=True, type=Path,
                        help='Path to the memory store database')
    parser.add_argument('--view-budget-mib', type=int, default=None,
                        help='Resident view budget in MiB, to check whether the copy still fits')
    parser.add_argument('--json', action='store_true', help='Emit JSON instead of a text report')
    args = parser.parse_args(argv)
    try:
        result = report(args.database,
                         view_budget_bytes=args.view_budget_mib * 1024 * 1024
                         if args.view_budget_mib else None)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0
    print('store      %.1f MiB (bucket %s)'
          % (result['database_bytes'] / 1024 / 1024, result['database_mib_bucket']))
    print('facts      %d, embeddings %d' % (result['fact_count'], result['embedding_count']))
    print('vectors    %.1f MiB (%.1f%% of store), duplicated=%s'
          % (result['vector_bytes'] / 1024 / 1024,
             result['vector_share_of_store'] * 100,
             result['vector_storage_is_duplicated']))
    print('dormant    %d of %d embeddings (%.1f%%) belong to retired facts'
          % (result['dormant_embedding_count'], result['embedding_count'],
             result['dormant_share_of_embeddings'] * 100))
    if result['resident_view_fit'] is not None:
        print('view       %s (budget %d MiB)'
              % (result['resident_view_fit'], args.view_budget_mib))
    for name, size in list(result['tables'].items())[:6]:
        print('  %-32s %8.1f MiB' % (name, size / 1024 / 1024))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
