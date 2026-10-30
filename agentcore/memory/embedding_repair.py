"""Bounded repair of facts blocking a journal-backed memory view.

Dry-run by default. The caller must configure the memory engine and explicitly
select its primary embedder. No fallback model or existing embedding cache is
used. This module does not schedule itself or advance a view checkpoint.
"""
import math
import base64
import json
import os
import sqlite3
import stat
import uuid
from pathlib import Path

from . import change_journal, memory_store
from .config import get_config




def _read_connection():
    """Open existing storage read-only without schema initialization."""
    path = get_config().db_path().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        from . import vec_index
        vec_index.load_into(conn)
        return conn
    except BaseException:
        conn.close()
        raise


def _archive_batch(directory, *, identity, epoch, candidates, index_rows, index_exists):
    """Durably retain original embeddings without exposing fact text."""
    root = Path(directory)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise ValueError('archive directory must be private and owned by caller')
    entries = []
    for fid, text, previous in candidates:
        embedding = None
        if previous is not None:
            old_model, dim, vector, text_hash, updated_at = previous
            embedding = {'model': old_model, 'dim': dim, 'vector_b64': base64.b64encode(vector).decode('ascii'),
                         'text_hash': text_hash, 'updated_at': updated_at}
        index = index_rows.get(fid)
        entries.append({'fact_id': fid, 'fact_text_hash': memory_store._text_hash(text),
                        'embedding': embedding,
                        'index_b64': base64.b64encode(index).decode('ascii') if index is not None else None})
    payload = {'version': 1, 'source_identity': list(identity), 'journal_epoch': epoch,
               'index_exists': bool(index_exists), 'entries': entries}
    path = root / ('embedding-repair-' + uuid.uuid4().hex + '.json')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False, sort_keys=True)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return str(path)


def repair_pending_batch(*, after_seq: int, user_id: str, model: str,
                         event_limit: int = 200, max_facts: int = 4,
                         apply: bool = False, scan_after_seq: int = None,
                         archive_dir=None, _checkpoint=None) -> dict:
    if type(after_seq) is not int or after_seq < 0 or not user_id:
        raise ValueError('invalid repair scope')
    if type(event_limit) is not int or not 1 <= event_limit <= 200:
        raise ValueError('invalid event limit')
    if type(max_facts) is not int or not 1 <= max_facts <= 32:
        raise ValueError('invalid fact limit')
    if model != memory_store.DEFAULT_EMBED_MODEL:
        raise ValueError('embedding model mismatch')
    if scan_after_seq is None:
        scan_after_seq = after_seq
    if type(scan_after_seq) is not int or scan_after_seq < after_seq:
        raise ValueError('invalid scan position')
    cfg = get_config()
    if apply and _checkpoint is not None and archive_dir is None:
        raise ValueError('private archive directory required for checkpoint repair')
    if apply and not callable(cfg.embed_fn):
        raise ValueError('primary embedding backend unavailable')
    conn = _read_connection()
    try:
        with change_journal._transaction(conn):
            meta = change_journal._validate(conn)
            if _checkpoint is not None:
                record = _checkpoint.record
                if (_checkpoint._load() != record or record['binding'] != _checkpoint.binding
                        or record['epoch'] != meta['epoch']
                        or record['source_identity'] != list(change_journal._identity(conn))
                        or record['schema_version'] != conn.execute('PRAGMA schema_version').fetchone()[0]
                        or record['seq'] != after_seq
                        or not meta['floor'] <= after_seq <= meta['latest']
                        or change_journal._anchor(conn, meta, after_seq) != record['token']):
                    raise ValueError('repair checkpoint changed or mismatched')
            if after_seq < meta['floor'] or after_seq > meta['latest']:
                raise ValueError('sequence outside retained journal')
            if scan_after_seq > meta['latest']:
                raise ValueError('scan position beyond journal')
            events = conn.execute(
                'SELECT seq,fact_id FROM memory_view_changes WHERE seq>? ORDER BY seq LIMIT ?',
                (scan_after_seq, event_limit)).fetchall()
            ids = list(dict.fromkeys(row['fact_id'] for row in events))
            candidates = []
            other_user_candidates = 0
            for fid in ids:
                row = conn.execute(
                    'SELECT f.user_id,f.subject,f.statement,e.fact_id AS embedding_id,e.model,e.dim,e.vector,e.text_hash,e.updated_at FROM working_fact f '
                    'LEFT JOIN fact_embeddings e ON e.fact_id=f.fact_id '
                    "WHERE f.fact_id=? AND f.status='active'", (fid,)
                ).fetchone()
                if not row:
                    continue
                text = ((row['subject'] or '') + ' ' + (row['statement'] or '')).strip()
                if row['model'] != model or row['text_hash'] != memory_store._text_hash(text):
                    if row['user_id'] == user_id:
                        previous = ((row['model'], row['dim'], row['vector'], row['text_hash'], row['updated_at'])
                                    if row['embedding_id'] is not None else None)
                        candidates.append((fid, text, previous))
                    else:
                        other_user_candidates += 1
            selected = candidates[:max_facts]
            index_rows = {}
            index_exists = False
            if apply and selected:
                index_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='vec_facts'").fetchone()
                if index_exists:
                    for fid, _, _ in selected:
                        indexed = conn.execute('SELECT embedding FROM vec_facts WHERE fact_id=?', (fid,)).fetchone()
                        index_rows[fid] = indexed[0] if indexed else None
                if archive_dir is not None:
                    source_identity = change_journal._identity(conn)
        result = {'ok': True, 'dry_run': not apply, 'events_scanned': len(events),
                  'scanned_through': events[-1]['seq'] if events else scan_after_seq,
                  'candidate_count': len(candidates), 'other_user_candidates': other_user_candidates,
                  'journal_remaining': meta['max_events'] - (meta['latest'] - meta['floor']),
                  'selected': len(selected),
                  'remaining_in_scanned_batch': max(0, len(candidates) - len(selected)),
                  'repaired': 0, 'failed': 0, 'changed': 0}
        if not apply:
            result['next_scan_after_seq'] = result['scanned_through'] if not candidates and not other_user_candidates else None
            return result
        # Each successful embedding write creates another journal event. Keep
        # headroom for ordinary writers and stop before consuming the reserve.
        reserve = min(32, max(1, meta['max_events'] // 4))
        if selected and result['journal_remaining'] <= reserve + len(selected):
            result.update(ok=False, error='journal_capacity_low', next_scan_after_seq=None)
            return result
        if archive_dir is not None and selected:
            result['archive'] = _archive_batch(archive_dir, identity=source_identity, epoch=meta['epoch'],
                                               candidates=selected, index_rows=index_rows,
                                               index_exists=index_exists)
        for fid, text, previous in selected:
            current = conn.execute('SELECT floor,latest,max_events FROM memory_view_meta WHERE singleton=1').fetchone()
            if not current or current['max_events'] - (current['latest'] - current['floor']) <= reserve:
                result['failed'] += 1
                result['error'] = 'journal_capacity_low'
                break
            if not text or len(text) > 8192:
                result['failed'] += 1
                continue
            try:
                # Direct call intentionally avoids memory_store's cache and
                # its fallback chain, whose model identity may be different.
                vectors = cfg.embed_fn([text])
                if not vectors or len(vectors) != 1 or len(vectors[0]) != cfg.embed_dim:
                    raise ValueError('invalid vector dimensions')
                vector = [float(v) for v in vectors[0]]
                if not all(math.isfinite(v) for v in vector):
                    raise ValueError('non-finite vector')
                memory_store._persist_fact_embedding(
                    fid, text, vector, model=model, expected_user_id=user_id, expected_embedding=previous,
                    expected_index=index_rows.get(fid) if index_exists else memory_store._EMBEDDING_UNGUARDED)
                row = conn.execute(
                    'SELECT f.subject,f.statement,f.status,e.model,e.text_hash,e.vector '
                    'FROM working_fact f LEFT JOIN fact_embeddings e ON f.fact_id=e.fact_id '
                    'WHERE f.fact_id=? AND f.user_id=?', (fid, user_id)).fetchone()
                if not row or row['status'] != 'active' or ((row['subject'] or '') + ' ' +
                        (row['statement'] or '')).strip() != text:
                    result['changed'] += 1
                elif (row['model'] == model and row['text_hash'] == memory_store._text_hash(text)
                      and row['vector'] == memory_store._pack_vector(vector)):
                    result['repaired'] += 1
                else:
                    result['failed'] += 1
            except Exception:  # report only counts; model errors may contain private data
                result['failed'] += 1
        result['ok'] = not (result['failed'] or result['changed'] or other_user_candidates)
        result['batch_complete'] = result['ok'] and result['remaining_in_scanned_batch'] == 0
        result['next_scan_after_seq'] = result['scanned_through'] if result['batch_complete'] else None
        return result
    finally:
        conn.close()


def repair_from_checkpoint(*, checkpoint_path, user_id: str, model: str,
                           event_limit: int = 200, max_facts: int = 4,
                           apply: bool = False, scan_after_seq: int = None, archive_dir=None) -> dict:
    """Validate the private recovery witness before inspecting its next batch.

    The checkpoint is never modified. Re-read it on each invocation; its scan
    boundary is not permission to advance the serving view's cursor.
    """
    from .recovery_checkpoint import RecoveryCheckpoint, principal

    if principal()['user_id'] != user_id:
        raise ValueError('repair principal mismatch')
    checkpoint = RecoveryCheckpoint(checkpoint_path)
    conn = _read_connection()
    try:
        checkpoint.validate(conn)
    finally:
        conn.close()
    return repair_pending_batch(after_seq=checkpoint.record['seq'], user_id=user_id,
                                model=model, event_limit=event_limit,
                                max_facts=max_facts, apply=apply, scan_after_seq=scan_after_seq,
                                archive_dir=archive_dir, _checkpoint=checkpoint)



def repair_cycle(*, checkpoint_path, user_id: str, model: str, archive_dir,
                 start_after_seq=None, max_batches: int = 8,
                 max_facts_per_batch: int = 4, event_limit: int = 200,
                 time_budget_seconds: float = 30.0, pause_seconds: float = 0.25) -> dict:
    """Run an explicitly requested, bounded series of archived repair batches.

    The time budget is checked between batches; it cannot interrupt an
    embedder already in progress. No background scheduler is started.
    """
    import time
    if type(max_batches) is not int or not 1 <= max_batches <= 8:
        raise ValueError('invalid batch budget')
    if type(max_facts_per_batch) is not int or not 1 <= max_facts_per_batch <= 4:
        raise ValueError('invalid per-batch budget')
    if not 0 < time_budget_seconds <= 30 or not 0 <= pause_seconds <= 5:
        raise ValueError('invalid time budget')
    deadline = time.monotonic() + time_budget_seconds
    cursor = start_after_seq
    result = {'ok': True, 'batches': 0, 'repaired': 0, 'failed': 0,
              'other_user_candidates': 0, 'archives': [], 'next_scan_after_seq': cursor,
              'stop_reason': 'batch_budget'}
    for _ in range(max_batches):
        if time.monotonic() >= deadline:
            result['stop_reason'] = 'time_budget'
            break
        try:
            batch = repair_from_checkpoint(checkpoint_path=checkpoint_path, user_id=user_id,
                model=model, archive_dir=archive_dir, apply=True, scan_after_seq=cursor,
                event_limit=event_limit, max_facts=max_facts_per_batch)
        except Exception as exc:
            # Earlier batches may already have committed. Preserve their
            # archive paths and counters; do not claim a full rollback.
            result['ok'] = False
            result['error_type'] = type(exc).__name__
            result['stop_reason'] = 'batch_exception'
            break
        result['batches'] += 1
        result['repaired'] += batch['repaired']
        result['failed'] += batch['failed'] + batch['changed']
        result['other_user_candidates'] += batch['other_user_candidates']
        if 'archive' in batch:
            result['archives'].append(batch['archive'])
        if not batch['ok']:
            result['ok'] = False
            result['stop_reason'] = batch.get('error', 'batch_incomplete')
            break
        next_cursor = batch['next_scan_after_seq']
        if next_cursor is not None:
            cursor = next_cursor
        # A None cursor on a successful batch means more selected candidates
        # remain in this same page: scan that page again before moving on.
        result['next_scan_after_seq'] = cursor
        if not batch['events_scanned']:
            result['stop_reason'] = 'end_of_journal'
            break
        if pause_seconds:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(pause_seconds, remaining))
    return result


def main(argv=None) -> int:
    import argparse
    import importlib
    import json

    parser = argparse.ArgumentParser(description='Inspect or repair a bounded pending memory batch')
    parser.add_argument('--bootstrap', required=True, help='memory configuration module:callable')
    parser.add_argument('--checkpoint', required=True, help='private recall recovery checkpoint')
    parser.add_argument('--user', required=True)
    parser.add_argument('--model', required=True, help='configured embedding identity')
    parser.add_argument('--event-limit', type=int, default=200)
    parser.add_argument('--max-facts', type=int, default=4)
    parser.add_argument('--scan-after-seq', type=int, help='temporary scan cursor, never the checkpoint')
    parser.add_argument('--archive-dir', help='existing private directory for original vector records')
    parser.add_argument('--cycle', action='store_true', help='run up to eight bounded repair batches')
    parser.add_argument('--apply', action='store_true', help='write regenerated vectors')
    args = parser.parse_args(argv)
    try:
        module, symbol = args.bootstrap.split(':', 1)
        getattr(importlib.import_module(module), symbol)()
        if args.cycle:
            if not args.apply or not args.archive_dir:
                raise ValueError('cycle requires apply and archive directory')
            result = repair_cycle(checkpoint_path=args.checkpoint, user_id=args.user,
                                  model=args.model, archive_dir=args.archive_dir,
                                  start_after_seq=args.scan_after_seq,
                                  event_limit=args.event_limit, max_facts_per_batch=args.max_facts)
        else:
            result = repair_from_checkpoint(checkpoint_path=args.checkpoint, user_id=args.user,
                                            model=args.model, event_limit=args.event_limit,
                                            max_facts=args.max_facts, apply=args.apply,
                                            scan_after_seq=args.scan_after_seq, archive_dir=args.archive_dir)
    except Exception as exc:  # never print private content or backend errors
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
