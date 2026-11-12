"""agentcore.task_card_bridge — Decoupled task-bound lifecycle card bridge.

Binds messaging cards strictly to durable task IDs, decoupling card state from
transient frontdesk conversation turns. Provides in-place card updates across
the task's entire lifecycle (queued -> running -> progressive delivery -> terminal).
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import fcntl
from functools import wraps
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from agentcore.cardkit2_renderer import build_cardkit2_payload, seal_unclosed_markdown_fences
from agentcore.delivery_sanitizer import sanitize_and_extract_artifacts, sanitize_card_text

logger = logging.getLogger(__name__)
_MAX_THROTTLE_ENTRIES = 1024

# Zero-emoji engineering indicators conforming to Super Agent UX discipline.
# Running / progressive user-visible phrase is {runner}在进行. CardKit may split
# the header as "{runner} · 在进行". Do not invent nicknames; never paint a
# nameless generic-only running header (bare 执行中 / 处理中 / 进行中).
STATUS_LABELS = {
    "queued": "排队中",
    "running": "在进行",
    "progressive": "在进行",
    "completed": "处理完成",
    "failed": "执行未通过",
    "cancelled": "已取消",
}

STATUS_HEADERS = {
    "queued": ("wathet", "排队中"),
    "running": ("wathet", "在进行"),
    "progressive": ("wathet", "在进行"),
    "completed": ("indigo", "处理完成"),
    "failed": ("carmine", "执行未通过"),
    "cancelled": ("carmine", "已取消"),
}

# Non-running verdict words. Headers still carry the same runner name.
STATUS_VERDICT_WORDS = {
    "queued": "排队中",
    "stalled": "无响应",
    "lost": "进程已不在",
    "completed": "处理完成",
    "done": "处理完成",
    "failed": "执行未通过",
    "cancelled": "已取消",
    "starting": "启动中",
    "canceling": "取消中",
}


def resolve_runner_name(*candidates: Any) -> str:
    """First non-blank runner identity from catalog / card / payload. No nicknames."""
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text:
            return text[:80]
    return ""


def named_status_phrase(status: str, *name_candidates: Any) -> str:
    """User-visible status phrase. Running / progressive: {name}在进行."""
    name = resolve_runner_name(*name_candidates)
    key = str(status or "").strip().lower()
    if key in ("running", "progressive"):
        return ("%s在进行" % name) if name else ""
    word = STATUS_VERDICT_WORDS.get(key, "")
    if name and word:
        return "%s · %s" % (name, word)
    return name or word


def _serialize_task(method):
    """Serialize mutations and sends across bridge instances with bounded lock files."""
    @wraps(method)
    def wrapped(self, task_id, *args, **kwargs):
        if not isinstance(task_id, str) or not task_id or len(task_id) > 512:
            raise ValueError('task_id required')
        lock_dir = self.root / 'locks'
        lock_dir.mkdir(mode=0o700, exist_ok=True)
        stripe = hashlib.sha256(task_id.encode('utf-8')).digest()[0] % 64
        fd = os.open(lock_dir / ('%02x.lock' % stripe),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('task_card_lock_timeout') from None
                    time.sleep(min(.05, remaining))
            return method(self, task_id, *args, **kwargs)
        finally:
            os.close(fd)
    return wrapped


def _render_progress_bar(progress: float, width: int = 12) -> str:
    """Render a clean text-based progress bar."""
    p = max(0.0, min(1.0, float(progress)))
    filled = int(round(p * width))
    empty = width - filled
    return f"[{'■' * filled}{'□' * empty}]"


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


_sanitize_card_text = sanitize_card_text


def _truncate_summary_cleanly(text: str, limit: int = 1500, target: int = 1200) -> str:
    """Truncate long summaries safely without leaving dangling code fences or cut sentences.

    Prioritizes paragraph boundary (\\n\\n > 700), then single newline (\\n > 700),
    then Chinese sentence punctuations (。！？； > 700), seals any unclosed code fences,
    and attaches the overflow note.
    """
    clean_sum = _sanitize_card_text(text or "")
    if len(clean_sum) <= limit:
        return clean_sum

    candidate = clean_sum[:target].rstrip()
    cut_idx = -1

    last_para = candidate.rfind("\n\n")
    if last_para > 700:
        cut_idx = last_para
    else:
        last_nl = candidate.rfind("\n")
        if last_nl > 700:
            cut_idx = last_nl
        else:
            last_punct = -1
            for p in ("。", "！", "？", "；"):
                pos = candidate.rfind(p)
                if pos > last_punct:
                    last_punct = pos
            if last_punct > 700:
                cut_idx = last_punct + 1

    if cut_idx > 0:
        truncated = clean_sum[:cut_idx].rstrip()
    else:
        truncated = candidate

    truncated = seal_unclosed_markdown_fences(truncated)
    return f"{truncated}\n\n*(长文本详细内容已作为附件发送至会话)*"


class TaskCardBridge:
    """Manages the decoupled lifecycle of task-bound cards."""

    def __init__(self, root: Path | str, *, profile: str, sender: Optional[Any] = None,
                 min_update_interval: float = 1.2, lock_timeout: float = 30.0):
        if not _finite_number(lock_timeout) or lock_timeout <= 0:
            raise ValueError('lock_timeout must be positive and finite')
        if not _finite_number(min_update_interval) or min_update_interval < 0:
            raise ValueError('min_update_interval must be nonnegative and finite')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile = profile
        self.sender = sender
        self.lock_timeout = float(lock_timeout)
        self.min_update_interval = float(min_update_interval)
        self._last_patched: OrderedDict[str, float] = OrderedDict()
        self._throttle_lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""
                CREATE TABLE IF NOT EXISTS task_cards (
                    task_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    route TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    thread_id TEXT NOT NULL DEFAULT '',
                    message_id TEXT,
                    role TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    phase TEXT,
                    progress REAL DEFAULT 0.0,
                    elapsed REAL DEFAULT 0.0,
                    summary TEXT,
                    error_text TEXT NOT NULL DEFAULT '',
                    artifacts_json TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finalized INTEGER NOT NULL DEFAULT 0,
                    patch_state TEXT NOT NULL DEFAULT 'idle',
                    create_state TEXT NOT NULL DEFAULT 'idle'
                )
            """)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(task_cards)')}
            if 'patch_state' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN patch_state TEXT NOT NULL DEFAULT 'idle'")
                # The legacy schema recorded no send outcome. Do not infer that
                # an existing message has no in-flight or ambiguous update.
                conn.execute("UPDATE task_cards SET patch_state=CASE "
                             "WHEN message_id IS NOT NULL AND message_id<>'' THEN 'unknown' "
                             "WHEN finalized=1 THEN 'unsent' ELSE 'idle' END")
            if 'progress_patch_state' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN progress_patch_state TEXT NOT NULL DEFAULT 'idle'")
            if 'terminal_retries' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN terminal_retries INTEGER NOT NULL DEFAULT 0")
            if 'thread_id' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''")
            if 'error_text' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN error_text TEXT NOT NULL DEFAULT ''")
            if 'create_state' not in columns:
                conn.execute("ALTER TABLE task_cards ADD COLUMN create_state TEXT NOT NULL DEFAULT 'idle'")
                # Older rows have no durable record of an initial send. A missing
                # message ID does not prove that no create request reached the host.
                conn.execute("UPDATE task_cards SET create_state=CASE WHEN message_id IS NOT NULL "
                             "AND message_id<>'' THEN 'sent' ELSE 'unknown' END")
            conn.execute('CREATE INDEX IF NOT EXISTS task_card_terminal_health '
                         'ON task_cards(profile, finalized, patch_state, updated_at)')

    @contextmanager
    def db(self):
        db_path = self.root / "task_cards.db"
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _send_patch(self, task_id: str, address: dict, message_id: str, payload: dict, *, is_terminal: bool = False) -> dict:
        """Complete a write-ahead patch; uncertain results remain fenced across restarts."""
        try:
            result = self.sender(address=address, message_id=message_id, card_payload=payload)
        except Exception as exc:
            logger.warning('TaskCardBridge patch outcome unknown for %s (%s)', task_id, type(exc).__name__)
            result = {'ok': False, 'error': 'patch_outcome_unknown', 'error_type': type(exc).__name__}
        if not isinstance(result, dict):
            result = {'ok': False, 'error': 'patch_outcome_unknown'}
        reported_id = result.get('message_id')
        not_sent = result.get('not_sent')
        if ((not_sent is not None and type(not_sent) is not bool)
                or (not_sent is True and (result.get('ok') is not False or reported_id is not None))):
            result = {'ok': False, 'error': 'patch_outcome_unknown'}
            reported_id = None
        confirmed = (result.get('ok') is True and result.get('not_sent') is not True
                     and reported_id == message_id)
        definitely_unsent = (result.get('ok') is False and result.get('not_sent') is True
                             and reported_id is None)
        if result.get('ok') is True and not confirmed:
            result = {'ok': False, 'error': 'patch_receipt_mismatch'}
        state = 'idle' if confirmed else ('unsent' if definitely_unsent else 'unknown')
        now = time.time()
        with self.db() as conn:
            if is_terminal:
                if confirmed:
                    conn.execute("UPDATE task_cards SET finalized=1, patch_state='idle', terminal_retries=0, updated_at=? "
                                 "WHERE task_id=? AND profile=? AND patch_state='sending'",
                                 (now, task_id, self.profile))
                else:
                    # Increment terminal retry count
                    conn.execute("UPDATE task_cards SET terminal_retries=terminal_retries+1, updated_at=? "
                                 "WHERE task_id=? AND profile=? AND patch_state='sending'",
                                 (now, task_id, self.profile))
                    retries = conn.execute("SELECT terminal_retries FROM task_cards WHERE task_id=? AND profile=?",
                                           (task_id, self.profile)).fetchone()[0]
                    if retries >= 3:
                        conn.execute("UPDATE task_cards SET finalized=1, patch_state='degraded', updated_at=? "
                                     "WHERE task_id=? AND profile=? AND patch_state='sending'",
                                     (now, task_id, self.profile))
                    else:
                        conn.execute("UPDATE task_cards SET finalized=0, patch_state=?, updated_at=? "
                                     "WHERE task_id=? AND profile=? AND patch_state='sending'",
                                     (state, now, task_id, self.profile))
            else:
                conn.execute("UPDATE task_cards SET progress_patch_state=?, "
                             "patch_state=CASE WHEN finalized=1 THEN patch_state ELSE ? END, "
                             "updated_at=? WHERE task_id=? AND profile=? "
                             "AND (progress_patch_state='sending' OR patch_state='sending')",
                             (state, state, now, task_id, self.profile))
        if confirmed:
            with self._throttle_lock:
                self._last_patched[task_id] = time.monotonic()
                self._last_patched.move_to_end(task_id)
                while len(self._last_patched) > _MAX_THROTTLE_ENTRIES:
                    self._last_patched.popitem(last=False)
        return result

    def build_card_payload(
        self,
        task_id: str,
        role_name: str,
        goal: str,
        status: str,
        *,
        phase: str = "",
        progress: float = 0.0,
        elapsed: float = 0.0,
        summary: str = "",
        artifacts: Optional[List[str]] = None,
        error_msg: str = "",
        hero_img_key: Optional[str] = None,
        docx_url: str = "",
        display_label: str = "",
        role_id: str = "",
    ) -> Dict[str, Any]:
        """Construct the canonical CardKit 2.0 industrial card payload bound to the task.

        Delegates to ``agentcore.cardkit2_renderer.build_cardkit2_payload`` as the
        single source of truth (myai_colorful icon, wathet/indigo/carmine palette,
        turquoise-50/blue-50/red-50 main column_set, and grey-50 footer).
        """
        template, status_suffix = STATUS_HEADERS.get(
            status, ("wathet", STATUS_VERDICT_WORDS.get(status, _sanitize_card_text(status) or "在进行"))
        )
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        clean_role = resolve_runner_name(
            _sanitize_card_text(role_name),
            _sanitize_card_text(display_label),
            _sanitize_card_text(role_id),
        )

        # Trim goal for clean card presentation
        clean_goal = _sanitize_card_text(goal or "").replace("\r\n", "\n")
        if len(clean_goal) > 280:
            clean_goal = clean_goal[:277] + "..."

        # Modern, human-centric metadata block
        meta_lines = [
            f"**任务** `{_sanitize_card_text(task_id)}`  |  **角色** {clean_role}",
        ]
        if clean_goal:
            goal_quoted = "\n> ".join(clean_goal.splitlines())
            meta_lines.append(f"> **目标**：\n> {goal_quoted}")
        else:
            meta_lines.append("> **目标**：*通用协助*")

        sections = ["\n\n".join(meta_lines)]

        is_terminal = status in ("completed", "failed", "cancelled")
        is_failed = status in ("failed", "cancelled")

        if status in ("queued", "running", "progressive"):
            progress_lines = []
            if phase:
                progress_lines.append(f"**阶段**：{_sanitize_card_text(phase)}")
            if progress > 0.0:
                bar = _render_progress_bar(progress)
                progress_lines.append(f"**进度**：`{bar}` **{int(progress * 100)}%**")
            if elapsed > 0.0:
                progress_lines.append(f"**耗时**：{elapsed:.1f}s")
            if status == "queued":
                progress_lines.append("*排队中：等待空闲 Worker 调度...*")
            elif clean_role:
                progress_lines.append("*%s在进行，文字结论将优先直达。*" % clean_role)
            else:
                progress_lines.append("*正在持续执行中，文字结论将优先直达。*")
            sections.append("\n\n".join(progress_lines))

        elif status == "completed":
            comp_lines = []
            if summary:
                clean_sum = _truncate_summary_cleanly(summary)
                comp_lines.append(f"**执行结论**：\n\n{clean_sum}")
            if docx_url:
                safe_url = _sanitize_card_text(docx_url).strip()
                if re.match(r"^https?://[^\s()<>]+$", safe_url):
                    comp_lines.append(f"**在线文档**：[点击打开完整云文档]({safe_url})")
            if artifacts:
                art_items = ["**交付制品**："]
                for art in artifacts:
                    art_items.append(f"- `{Path(art).name}`")
                comp_lines.append("\n".join(art_items))
            if elapsed > 0.0:
                comp_lines.append(f"**总耗时**：{elapsed:.1f}s")
            sections.append("\n\n".join(comp_lines))

        elif status in ("failed", "cancelled"):
            fail_lines = []
            fail_desc = _sanitize_card_text(error_msg or summary or "任务未达到预期终态或已取消")
            fail_lines.append(f"**未达成说明**：\n\n{fail_desc}")
            if elapsed > 0.0:
                fail_lines.append(f"**耗时**：{elapsed:.1f}s")
            sections.append("\n\n".join(fail_lines))

        time_part = now_str.split(" ")[1] if " " in now_str else now_str
        footer_notes = [
            f"{'已完成' if status == 'completed' else (status_suffix if is_failed else status_suffix)}",
            f"更新于 {time_part}",
        ]

        card_text = "\n\n────────────────\n\n".join(sections)

        return build_cardkit2_payload(
            card_text,
            is_terminal=is_terminal,
            is_failed=is_failed,
            title=clean_role,
            subtitle="Super Agent 企业智能助手",
            elapsed=elapsed if elapsed > 0.0 else None,
            icon_token="myai_colorful",
            status_suffix=status_suffix,
            color_template=template,
            footer_notes=footer_notes,
            hero_img_key=hero_img_key,
        )

    @_serialize_task
    def bind_task(
        self,
        task_id: str,
        origin: Dict[str, Any],
        goal: str,
        role: str,
        display_name: str,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record task-to-card binding metadata."""
        now = time.time()
        route = str(origin.get("route") or "")
        chat_id = str(origin.get("chat") or "")
        channel = str(origin.get("channel") or "feishu")
        thread_id = str(origin.get('thread') or '')

        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("""
                INSERT OR IGNORE INTO task_cards (
                    task_id, profile, route, chat_id, channel, thread_id, message_id,
                    role, display_name, goal, status, phase, progress, elapsed,
                    summary, artifacts_json, created_at, updated_at, finalized
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', '已提交排队', 0.0, 0.0, NULL, NULL, ?, ?, 0)
            """, (task_id, self.profile, route, chat_id, channel, thread_id, message_id,
                  role, display_name, goal, now, now))
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM task_cards WHERE task_id=?', (task_id,)).fetchone()
            binding = (row['profile'], row['route'], row['chat_id'], row['channel'], row['thread_id'],
                       row['role'], row['display_name'], row['goal'])
            if binding != (self.profile, route, chat_id, channel, thread_id, role, display_name, goal):
                return {'ok': False, 'error': 'binding_conflict'}
            current_id = row['message_id']
            if message_id and current_id and message_id != current_id:
                return {'ok': False, 'error': 'message_id_conflict'}
            if message_id and not current_id:
                conn.execute("UPDATE task_cards SET message_id=?, create_state='sent', updated_at=? WHERE task_id=?",
                             (message_id, now, task_id))
                current_id = message_id
            elif message_id and current_id:
                conn.execute("UPDATE task_cards SET create_state='sent' WHERE task_id=?", (task_id,))

        card_payload = self.build_card_payload(
            task_id, display_name, goal, row['status'], phase=row['phase'] or '',
            progress=row['progress'] or 0.0, elapsed=row['elapsed'] or 0.0,
            summary=row['summary'] or '', artifacts=json.loads(row['artifacts_json'] or '[]'),
            error_msg=row['error_text'] or '', role_id=role,
        )
        return {"ok": True, "task_id": task_id, "message_id": current_id, "card_payload": card_payload}

    @_serialize_task
    def create_initial(self, task_id: str, creator: Any) -> Dict[str, Any]:
        """Create one card; fence ambiguous sends until an operator resolves them.

        The creator accepts address, card_payload and delivery_key keyword arguments.
        It must return an explicit message_id on success or not_sent=True only if
        no create request could have reached the platform.
        """
        if not callable(creator):
            raise ValueError('creator required')
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM task_cards WHERE task_id=? AND profile=?',
                               (task_id, self.profile)).fetchone()
            if row is None:
                return {'ok': False, 'error': 'task_not_found'}
            if row['message_id']:
                return {'ok': True, 'message_id': row['message_id'], 'already_created': True}
            if row['create_state'] not in ('idle', 'unsent'):
                return {'ok': False, 'error': 'previous_create_unknown'}
            address = {'profile': self.profile, 'route': row['route'],
                       'chat': row['chat_id'], 'channel': row['channel']}
            if row['thread_id']:
                address['thread'] = row['thread_id']
            payload = self.build_card_payload(
                task_id, row['display_name'], row['goal'], row['status'], phase=row['phase'] or '',
                progress=row['progress'] or 0.0, elapsed=row['elapsed'] or 0.0,
                summary=row['summary'] or '', artifacts=json.loads(row['artifacts_json'] or '[]'),
                error_msg=row['error_text'] or '', role_id=row['role'])
            conn.execute("UPDATE task_cards SET create_state='sending' WHERE task_id=? AND profile=?",
                         (task_id, self.profile))
        delivery_key = hashlib.sha256((self.profile + '\0' + task_id).encode('utf-8')).hexdigest()
        try:
            result = creator(address=address, card_payload=payload, delivery_key=delivery_key)
        except Exception as exc:
            logger.warning('TaskCardBridge create outcome unknown for %s (%s)', task_id, type(exc).__name__)
            result = None
        message_id = result.get('message_id') if isinstance(result, dict) else None
        not_sent = result.get('not_sent') if isinstance(result, dict) else None
        valid_flag = not_sent is None or type(not_sent) is bool
        confirmed = (isinstance(result, dict) and result.get('ok') is True
                     and valid_flag and not_sent is not True
                     and isinstance(message_id, str) and bool(message_id.strip()) and len(message_id) <= 512)
        unsent = (isinstance(result, dict) and result.get('ok') is False
                  and not_sent is True and message_id is None)
        with self.db() as conn:
            if confirmed:
                conn.execute("UPDATE task_cards SET message_id=?, create_state='sent', "
                             "patch_state=CASE WHEN patch_state IN ('idle','unsent') THEN 'idle' ELSE patch_state END, "
                             "updated_at=? WHERE task_id=? AND profile=? AND create_state='sending'",
                             (message_id, time.time(), task_id, self.profile))
            else:
                conn.execute("UPDATE task_cards SET create_state=? WHERE task_id=? AND profile=? "
                             "AND create_state='sending'",
                             ('unsent' if unsent else 'unknown', task_id, self.profile))
        if confirmed:
            return {'ok': True, 'message_id': message_id}
        return {'ok': False, 'not_sent': True, 'error': 'create_not_sent'} if unsent else {
            'ok': False, 'error': 'create_outcome_unknown'}

    @_serialize_task
    def set_message_id(self, task_id: str, message_id: str) -> bool:
        """Associate a platform message once; never silently replace an existing ID."""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError('message_id required')
        with self.db() as conn:
            result = conn.execute("UPDATE task_cards SET message_id=?, create_state='sent', updated_at=? WHERE task_id=? "
                                  "AND profile=? AND (message_id IS NULL OR message_id='' OR message_id=?)",
                                  (message_id, time.time(), task_id, self.profile, message_id))
            return result.rowcount == 1

    @_serialize_task
    def update_progress(
        self,
        task_id: str,
        *,
        status: str = "running",
        phase: str = "",
        progress: float = 0.0,
        elapsed: float = 0.0,
        force_sync: bool = False,
    ) -> Dict[str, Any]:
        """Update live execution progress and patch the card in place."""
        now = time.time()
        if status not in ('queued', 'running', 'progressive'):
            return {'ok': False, 'error': 'invalid_progress_status'}
        if (not _finite_number(progress) or not 0 <= progress <= 1
                or not _finite_number(elapsed) or elapsed < 0):
            return {'ok': False, 'error': 'invalid_progress_values'}

        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT role, display_name, goal, message_id, route, chat_id, channel, finalized, patch_state, thread_id, progress_patch_state FROM task_cards WHERE task_id=? AND profile=?",
                (task_id, self.profile)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task_not_found"}
            if row[7]:
                return {'ok': False, 'error': 'already_finalized'}
            with self._throttle_lock:
                last_t = self._last_patched.get(task_id)
            if not force_sync and last_t is not None and (time.monotonic() - last_t) < self.min_update_interval:
                return {"ok": True, "skipped_throttle": True}
            card_payload = self.build_card_payload(
                task_id, row[1], row[2], status, phase=phase, progress=progress, elapsed=elapsed,
                role_id=row[0])

            conn.execute("""
                UPDATE task_cards
                SET status=?, phase=?, progress=?, elapsed=?, updated_at=?
                WHERE task_id=? AND profile=?
            """, (status, phase, progress, elapsed, now, task_id, self.profile))
            will_patch = bool(row[3] and self.sender and callable(self.sender))
            blocked = will_patch and (row[8] not in ('idle', 'unsent') or row[10] not in ('idle', 'unsent'))
            if will_patch and not blocked:
                conn.execute("UPDATE task_cards SET patch_state='sending', progress_patch_state='sending' WHERE task_id=? AND profile=?",
                             (task_id, self.profile))

        role, display_name, goal, message_id, route, chat_id, channel, _, _, thread_id, _ = row
        patch_result = None
        if blocked:
            patch_result = {'ok': False, 'error': 'previous_patch_unknown'}
        elif will_patch:
            address = {"route": route, "chat": chat_id, "channel": channel, "profile": self.profile}
            if thread_id:
                address['thread'] = thread_id
            patch_result = self._send_patch(task_id, address, message_id, card_payload, is_terminal=False)

        return {"ok": True, "task_id": task_id, "card_payload": card_payload, "patch_result": patch_result}

    @_serialize_task
    def finalize(
        self,
        task_id: str,
        *,
        status: str = "completed",
        summary: str = "",
        artifacts: Optional[List[str]] = None,
        elapsed: float = 0.0,
        error_msg: str = "",
        hero_img_key: Optional[str] = None,
        docx_url: str = "",
    ) -> Dict[str, Any]:
        """Finalize the card upon terminal outcome (turn-green contract)."""
        if status not in ('completed', 'failed', 'cancelled'):
            return {'ok': False, 'error': 'invalid_terminal_status'}
        if not _finite_number(elapsed) or elapsed < 0:
            return {'ok': False, 'error': 'invalid_elapsed'}
        if artifacts is not None and (not isinstance(artifacts, list) or len(artifacts) > 8
                or any(not isinstance(path, str) or not path or len(path) > 4096 for path in artifacts)):
            return {'ok': False, 'error': 'invalid_artifacts'}
        now = time.time()
        with self.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(
                "SELECT role, display_name, goal, message_id, route, chat_id, channel, "
                "finalized, patch_state, thread_id, status, summary, artifacts_json, "
                "elapsed, error_text, terminal_retries, updated_at "
                "FROM task_cards WHERE task_id=? AND profile=?",
                (task_id, self.profile)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "task_not_found"}
            updated_at = float(row[16] or 0.0)
            is_stale_sending = (row[8] == 'sending' and (now - updated_at) > 30.0)
            is_terminal_already = row[10] in ('completed', 'failed', 'cancelled')
            if is_terminal_already:
                same = (row[10] == status and (row[11] or '') == (summary or '')
                        and json.loads(row[12] or '[]') == (artifacts or [])
                        and (row[14] or '') == (error_msg or ''))
                if not same:
                    return {'ok': False, 'error': 'already_finalized'}
                if row[7] and row[8] == 'idle':
                    return {'ok': True, 'finalized': True, 'already_finalized': True}
                if row[7] and row[8] == 'degraded':
                    return {'ok': True, 'finalized': True, 'degraded': True}
                if row[8] == 'sending' and not is_stale_sending:
                    return {'ok': True, 'finalized': bool(row[7]),
                            'patch_result': {'ok': False, 'error': 'previous_patch_unknown'}}
            card_payload = self.build_card_payload(
                task_id, row[1], row[2], status, elapsed=elapsed,
                summary=summary, artifacts=artifacts, error_msg=error_msg,
                hero_img_key=hero_img_key, docx_url=docx_url, role_id=row[0])

            will_patch = bool(row[3] and self.sender and callable(self.sender))
            blocked = will_patch and row[8] == 'sending' and not is_stale_sending
            if will_patch and not blocked:
                conn.execute("""
                    UPDATE task_cards
                    SET status=?, summary=?, error_text=?, artifacts_json=?, elapsed=?, updated_at=?, patch_state='sending'
                    WHERE task_id=? AND profile=?
                """, (status, summary, error_msg, json.dumps(artifacts or []), elapsed, now, task_id, self.profile))
            elif not will_patch:
                conn.execute("""
                    UPDATE task_cards
                    SET status=?, summary=?, error_text=?, artifacts_json=?, elapsed=?, updated_at=?, finalized=1, patch_state='unsent'
                    WHERE task_id=? AND profile=?
                """, (status, summary, error_msg, json.dumps(artifacts or []), elapsed, now, task_id, self.profile))

        role, display_name, goal, message_id, route, chat_id, channel, _, patch_state, thread_id = row[:10]
        patch_result = None
        if blocked:
            patch_result = {'ok': False, 'error': 'previous_patch_unknown'}
        elif will_patch:
            address = {"route": route, "chat": chat_id, "channel": channel, "profile": self.profile}
            if thread_id:
                address['thread'] = thread_id
            patch_result = self._send_patch(task_id, address, message_id, card_payload, is_terminal=True)

        with self._throttle_lock:
            self._last_patched.pop(task_id, None)

        with self.db() as conn:
            final_row = conn.execute("SELECT finalized, patch_state, terminal_retries FROM task_cards WHERE task_id=? AND profile=?",
                                     (task_id, self.profile)).fetchone()
            is_finalized = bool(final_row[0]) if final_row else False
            is_degraded = (final_row[1] == 'degraded') if final_row else False

        res = {"ok": True, "task_id": task_id, "finalized": is_finalized, "card_payload": card_payload, "patch_result": patch_result}
        if is_degraded:
            res["degraded"] = True
        return res

    def get_card(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Query stored card status and metadata."""
        with self.db() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM task_cards WHERE task_id=? AND profile=?",
                               (task_id, self.profile)).fetchone()
            if not row:
                return None
            return dict(row)

    def terminal_health(self, *, stale_before: float) -> Dict[str, int]:
        """Count fenced terminal patches without exposing task contents or addresses."""
        if not _finite_number(stale_before):
            raise ValueError('stale_before must be finite')
        with self.db() as conn:
            conn.execute('BEGIN')
            unknown = conn.execute("SELECT COUNT(*) FROM task_cards WHERE profile=? "
                                   "AND (status IN ('completed', 'failed', 'cancelled') OR finalized=1) "
                                   "AND patch_state='unknown'", (self.profile,)).fetchone()[0]
            stale = conn.execute("SELECT COUNT(*) FROM task_cards WHERE profile=? "
                                 "AND (status IN ('completed', 'failed', 'cancelled') OR finalized=1) "
                                 "AND patch_state='sending' AND updated_at<?",
                                 (self.profile, stale_before)).fetchone()[0]
        return {'unknown': unknown, 'stale_sending': stale}
