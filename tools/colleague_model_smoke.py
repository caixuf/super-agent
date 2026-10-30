#!/usr/bin/env python3
"""Run an opt-in, synthetic, single-colleague model smoke check.

The administrator must explicitly supply a trusted ``module:callable`` model
adapter. Its contract is ``adapter(messages, **options)`` and its result must
match agentcore.analysis_runner's documented model response. Only synthetic
source is sent to that adapter; the adapter's own credential and network
handling remain its operator's responsibility. This script does not exercise a
platform, background queue, running agent, or persistent conversation.

The provider adapter is arbitrary trusted code. It may perform network calls or
write its own logs; this script cannot make it safe or prove it is a real model.
Provider stdout and stderr are discarded by the supervising process. No model
answer, path, exception message, credential or provider identifier is printed.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_ADAPTER = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z', re.ASCII)
_CHECKS = ('adapter_loaded', 'model_called', 'report_validated', 'member_bound', 'signed_relay')


def _empty():
    return {name: False for name in _CHECKS}


def _worker(adapter_name, root):
    """Executed only by the supervised child; returns booleans, no details."""
    from agentcore.colleague_readonly_dialogue import ReadOnlyDialogue
    from agentcore.colleague_readonly_runtime import ReadOnlyColleagueRuntime
    from agentcore.colleague_registry import Actor, Registry, Supervisor

    checks = _empty()
    try:
        module, symbol = adapter_name.split(':', 1)
        model = getattr(importlib.import_module(module), symbol)
        if not callable(model):
            return checks
        checks['adapter_loaded'] = True
        private = Path(root)
        sources = private / 'sources'
        sources.mkdir(mode=0o700)
        source = sources / 'sample.py'
        source.write_text('def sample():\n    return 42\n', encoding='utf-8')
        state = private / 'runtime'
        state.mkdir(mode=0o700)
        registry = Registry(private / 'registry.sqlite', profile_id='team', primary_id='leader')
        leader, supervisor = Actor('team', 'leader'), Supervisor('team')
        registry.define_role(actor=leader, role_id='developer', label='Developer', duty='Read only',
                             boundaries=['No changes'], request_id='role-1')
        registry.hire(actor=leader, member_id='developer', role_id='developer',
                      display_name='Colleague', request_id='hire-1')
        # This is a synthetic status in a private registry, not a running worker.
        registry.observe_runtime(supervisor=supervisor, member_id='developer', generation=1, state='running')
        owner = {'profile': 'leader', 'session': 'smoke-session'}

        def tracked_model(messages, **options):
            checks['model_called'] = True
            return model(messages, **options)

        runtime = ReadOnlyColleagueRuntime(registry, supervisor, state_dir=state,
            policy={'allowed_roots': [str(sources)]}, model=tracked_model,
            authorize_owner=lambda candidate: candidate == owner)
        runtime.run(owner=owner, request_id='smoke-1', member_id='developer', generation=1,
                    goal='Explain the return value of sample using a cited source line.',
                    sources=[{'path': str(source)}])
        result = runtime.inspect_result(owner=owner, request_id='smoke-1')
        checks['report_validated'] = bool(result and result.get('checked_result') is True
                                          and result.get('result', {}).get('claim_count', 0) > 0
                                          and result.get('result', {}).get('verification') == 'citation_traceability_only')
        checks['member_bound'] = bool(result and result.get('owner') == owner
                                       and result.get('member_id') == 'developer' and result.get('generation') == 1)
        presented = ReadOnlyDialogue(runtime, owner=owner).present('smoke-1')
        checks['signed_relay'] = bool(presented.get('state') == 'completed'
                                      and presented.get('sender') == 'primary_relay'
                                      and presented.get('member_id') == 'developer'
                                      and '主 Bot 转交' in presented.get('text', '')
                                      and 'Colleague' in presented.get('text', ''))
    except Exception:
        # Never expose exception strings or raw provider output.
        pass
    return checks


def _main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model-adapter', required=True, help='Trusted explicit module:callable')
    parser.add_argument('--timeout', type=int, default=120, help='Overall timeout in seconds (1–600)')
    args = parser.parse_args()
    if not _ADAPTER.fullmatch(args.model_adapter) or not 1 <= args.timeout <= 600:
        parser.error('invalid adapter or timeout')
    with tempfile.TemporaryDirectory(prefix='colleague-smoke-') as directory:
        # Child output is discarded even if a dependency prints sensitive data.
        # No credentials are accepted as command-line arguments.
        result_path = Path(directory) / 'checks.json'
        env = os.environ.copy()
        env['COLLEAGUE_SMOKE_RESULT'] = str(result_path)
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', args.model_adapter],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, start_new_session=True)
        timed_out = False
        try:
            proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        checks = _empty()
        if not timed_out and proc.returncode == 0:
            try:
                received = json.loads(result_path.read_text(encoding='utf-8'))
                if isinstance(received, dict) and set(received) == set(_CHECKS) and all(type(v) is bool for v in received.values()):
                    checks = received
            except (OSError, ValueError):
                pass
        result = {'ok': all(checks.values()), 'timed_out': timed_out, **checks}
        print(json.dumps(result, sort_keys=True))
        return 0 if result['ok'] else 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        # Private protocol, started by _main; never send its detailed output to
        # the terminal. The directory was created by TemporaryDirectory.
        try:
            target = Path(os.environ['COLLEAGUE_SMOKE_RESULT'])
            target.write_text(json.dumps(_worker(sys.argv[2], target.parent)), encoding='utf-8')
        except Exception:
            sys.exit(1)
    else:
        sys.exit(_main())
