"""Durable turn capture awaiting the host's terminal verdict.

post_llm_call supplies text, but does not certify success. Only on_session_end
may mark a captured turn completed. An interrupted capture remains unconfirmed.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def is_automated_turn(turn_id):
    """Host cron IDs identify scheduler instructions, not human preferences."""
    return isinstance(turn_id, str) and ':cron:' in turn_id


class TurnSpool:
    def __init__(self, root, *, profile, user_id, agent_id):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError('absolute spool path required')
        self.identity = dict(profile=profile, user_id=user_id, agent_id=agent_id)
        if not all(isinstance(x, str) and x.strip() for x in self.identity.values()):
            raise ValueError('explicit profile and principal required')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, session_id, turn_id):
        if not all(isinstance(x, str) and 0 < len(x) <= 512 for x in (session_id, turn_id)):
            raise ValueError('session and turn identity required')
        key = json.dumps([self.identity, session_id, turn_id], sort_keys=True)
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    @contextmanager
    def _locked(self, path):
        with open(path.with_suffix('.lock'), 'a') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _save(self, path, record):
        fd, name = tempfile.mkstemp(dir=self.root, prefix='.turn-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def capture(self, *, session_id, turn_id, user_message, assistant_reply):
        if is_automated_turn(turn_id):
            return 'discarded'
        path = self._path(session_id, turn_id)
        text = {'user_message': str(user_message or '')[:8000],
                'assistant_reply': str(assistant_reply or '')[:8000]}
        with self._locked(path):
            if path.exists():
                previous = json.loads(path.read_text())
                if any(previous[k] != v for k, v in text.items()):
                    raise ValueError('turn_capture_conflict')
                return previous['state']
            record = dict(self.identity, session_id=session_id, turn_id=turn_id,
                          **text, state='awaiting_verdict', occurred_at=time.time())
            self._save(path, record)
        return record['state']

    def finalize(self, *, session_id, turn_id, completed=False, failed=False, interrupted=False):
        path = self._path(session_id, turn_id)
        with self._locked(path):
            if not path.exists():
                return 'missing_capture'
            record = json.loads(path.read_text())
            state = 'completed' if completed is True and not failed and not interrupted else 'discarded'
            if record['state'] == 'enqueued' and state == 'completed':
                return 'enqueued'
            if record['state'] != 'awaiting_verdict':
                if record['state'] != state:
                    raise ValueError('turn_verdict_conflict')
                return state
            record['state'] = state
            self._save(path, record)
            return state

    def ingest(self, enqueue):
        """Move completed captures into the canonical turn queue; retry is idempotent.

        Corrupt, foreign-principal and structurally invalid records are isolated
        by renaming them out of the ``*.json`` scan set. A single bad record must
        not terminate the runtime or block later valid turns.
        """
        counts = {'enqueued': 0, 'unconfirmed': 0, 'discarded': 0,
                  'quarantined': 0}
        for path in sorted(self.root.glob('*.json')):
            try:
                with self._locked(path):
                    record = json.loads(path.read_text())
                    if not isinstance(record, dict):
                        raise ValueError('spool_record_invalid')
                    required = ('session_id', 'turn_id', 'user_message',
                                'assistant_reply', 'state')
                    if any(key not in record for key in required):
                        raise ValueError('spool_record_incomplete')
                    if (not isinstance(record['session_id'], str)
                            or not isinstance(record['turn_id'], str)
                            or not isinstance(record['user_message'], str)
                            or not isinstance(record['assistant_reply'], str)
                            or not isinstance(record['state'], str)):
                        raise ValueError('spool_record_invalid')
                    if any(record.get(k) != v for k, v in self.identity.items()):
                        raise ValueError('spool_principal_mismatch')
                    state = record['state']
                    if state == 'completed' and is_automated_turn(record.get('turn_id')):
                        record['state'] = 'discarded'
                        record['discard_reason'] = 'automated_turn'
                        self._save(path, record)
                        counts['discarded'] += 1
                        continue
                    if state != 'completed':
                        if state == 'awaiting_verdict':
                            counts['unconfirmed'] += 1
                        elif state == 'discarded':
                            counts['discarded'] += 1
                        elif state != 'enqueued':
                            raise ValueError('spool_state_invalid')
                        continue
                    result = enqueue(**{k: v for k, v in record.items() if k != 'state'},
                                     status='completed', source='hermes_terminal_spool')
                    if not isinstance(result, dict) or result.get('ok') is not True:
                        raise RuntimeError('turn_enqueue_failed')
                    record['state'] = 'enqueued'
                    self._save(path, record)
                    counts['enqueued'] += 1
            except RuntimeError:
                raise
            except (OSError, ValueError, TypeError):
                # The reason is a fixed code. Never log the record body.
                quarantine = path.with_name(path.name + '.quarantine')
                try:
                    os.replace(path, quarantine)
                    os.chmod(quarantine, 0o600)
                except OSError:
                    pass
                counts['quarantined'] += 1
        return counts
