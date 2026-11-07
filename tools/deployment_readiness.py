#!/usr/bin/env python3
"""Report deployment readiness without printing credentials or sending messages."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path
import subprocess
import sys

import yaml


class _ConfigLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ValueError('invalid configuration mapping')
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_ConfigLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _section(mapping, key):
    value = mapping.get(key, {})
    if not isinstance(value, dict):
        raise ValueError('invalid configuration section')
    return value


def _service_active(name):
    try:
        result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', name],
                                timeout=8, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True if result.returncode == 0 else False if result.returncode in (3, 4) else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _routed_memory(home, profile, bridge, localvec):
    """Saved configuration checks only; never opens memory or starts a worker."""
    failed = [(name, False, 'routed memory binding unavailable; legacy fallback not checked')
              for name in ('memory-identity-policy', 'memory-recall',
                           'memory-ingestion-worker', 'memory-store-alignment')]
    try:
        policy = bridge['identity_routes_file']
        if (not isinstance(policy, str) or not Path(policy).is_absolute()
                or policy != localvec.get('identity_routes_file')
                or bridge.get('enabled') is not True
                or bridge.get('engine_root') != localvec.get('engine_root')
                or bridge.get('embed_url') != localvec.get('embed_url')):
            return failed
        engine = Path(bridge['engine_root'])
        endpoint = bridge['embed_url']
        if not engine.is_absolute() or not isinstance(endpoint, str) or not endpoint.strip():
            return failed
        # Use this checker's installed adapter, not code from a configured path.
        spec = importlib.util.spec_from_file_location(
            '_readiness_memory_identity', Path(__file__).resolve().parents[1] / 'runtime/hermes_memory_identity.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        routes = module.IdentityRouter(policy, profile=profile).runtimes(home=home)
        provider = home / 'plugins/localvec'
        recall_ok = (provider.resolve() == (engine / 'plugins/localvec').resolve()
                     and (provider / '__init__.py').is_file()
                     and all((engine / name).is_file() for name in (
                         'tools/recall_memory.py', 'runtime/hermes_memory_identity.py',
                         'runtime/memory_bootstrap.py')))
    except Exception:
        return failed

    units = Path.home() / '.config/systemd/user'
    active, aligned = True, True
    for route in routes:
        suffix = hashlib.sha256((route['user_id'] + '\0' + route['agent_id']).encode()).hexdigest()[:16]
        name = 'superagent-memory-' + profile + '-' + suffix + '.service'
        path = units / name
        running = _service_active(name)
        active = active and running is True and path.is_file() and not path.is_symlink()
        try:
            if (path.is_symlink() or path.with_name(name + '.d').exists()
                    or (units / 'service.d').exists()):
                raise ValueError('unit overrides require separate verification')
            env, command, service = {}, None, False
            settings = {}
            for line in path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith(('#', ';')):
                    continue
                if line.startswith('['):
                    service = line == '[Service]'
                    continue
                if not service:
                    continue
                key, value = line.split('=', 1)
                if key == 'Environment':
                    for item in shlex.split(value):
                        env_key, env_value = item.replace('%%', '%').split('=', 1)
                        if env_key in env:
                            raise ValueError('duplicate environment')
                        env[env_key] = env_value
                elif key == 'ExecStart' and command is None:
                    command = [part.replace('%%', '%') for part in shlex.split(value)]
                elif key in {'Type', 'UMask', 'Restart', 'RestartSec', 'KillMode', 'TimeoutStopSec'} and key not in settings:
                    settings[key] = value
                else:
                    raise ValueError('unit requires separate verification')
            python = Path(command[0])
            expected_env = dict(PYTHONPATH=str(engine), HERMES_HOME=str(home),
                                SUPERAGENT_MEMORY_DATA_DIR=route['data_dir'],
                                SUPERAGENT_MEMORY_USER=route['user_id'],
                                SUPERAGENT_MEMORY_AGENT=route['agent_id'],
                                SUPERAGENT_MEMORY_EMBED_URL=endpoint)
            expected_command = [str(python), '-m', 'agentcore.memory.runtime_service',
                                '--bootstrap', 'runtime.memory_bootstrap:bootstrap',
                                '--spool', route['spool_dir'],
                                '--queue', str(Path(route['data_dir']).parent / 'events.db'),
                                '--profile', profile, '--user', route['user_id'], '--agent', route['agent_id']]
            matches = (env == expected_env and command == expected_command
                       and python.is_absolute() and python.is_file() and os.access(python, os.X_OK)
                       and settings.get('Type') == 'simple' and settings.get('UMask') == '0077'
                       and settings.get('KillMode') == 'control-group')
            aligned = aligned and matches and running is True
        except (OSError, ValueError, TypeError, IndexError):
            aligned = False
    # A legacy worker may continue consuming the previous owner's spool. Never
    # call a routed profile ready while that worker is active or cannot be checked.
    legacy_inactive = _service_active('superagent-memory-' + profile + '.service') is False
    return [
        ('memory-identity-policy', True, str(len(routes)) + ' private memory runtimes; policy validated; host identity behavior not probed'),
        ('memory-recall', recall_ok, 'saved routed provider assets match; recall behavior not probed' if recall_ok else 'routed provider ownership or assets not verified'),
        ('memory-ingestion-worker', active and legacy_inactive,
         'all routed services active; loaded unit and process state not checked' if active and legacy_inactive else
         ('legacy ingestion service active or unverifiable' if not legacy_inactive else 'not all routed services and unit files verified')),
        ('memory-store-alignment', aligned and recall_ok and legacy_inactive,
         'all saved runtime/principal bindings match; loaded unit and live behavior not verified' if aligned and recall_ok and legacy_inactive else 'routed ingestion-to-recall binding not verified'),
    ]


def inspect(home: Path, profile: str):
    if not isinstance(profile, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', profile):
        raise ValueError('invalid profile')
    with (home / 'config.yaml').open(encoding='utf-8') as stream:
        text = stream.read(1048577)
    if len(text) > 1048576:
        raise ValueError('configuration exceeds check budget')
    config = yaml.load(text, Loader=_ConfigLoader)
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError('invalid configuration root')
    plugins = _section(config, 'plugins')
    enabled = plugins.get("enabled") or []
    disabled = plugins.get("disabled") or []
    if any(not isinstance(value, list) or any(not isinstance(name, str) for name in value)
           for value in (enabled, disabled)):
        raise ValueError('invalid plugin list')
    entries = _section(plugins, 'entries')
    for entry in entries.values():
        if not isinstance(entry, dict):
            raise ValueError('invalid plugin entry')
        _section(entry, 'settings')
    memory = _section(config, 'memory')
    localvec = _section(memory, 'localvec')
    bridge = _section(_section(entries, 'memory-bridge'), 'settings')
    routed = 'identity_routes_file' in bridge or 'identity_routes_file' in localvec
    results = []

    def add(name, ok, detail):
        results.append((name, bool(ok), detail))

    for name, required in {
        "task-status": ("enabled", "engine_root", "liveness_db"),
        "task-progress": ("enabled", "engine_root", "liveness_db"),
        "illustrated-delivery": ("enabled", "engine_root"),
        "durable-dispatch": ("enabled", "engine_root", "queue_root", "allowed_roots"),
        "markdown-delivery": ("enabled", "engine_root", "env_file", "outbox_root"),
        "memory-bridge": (("enabled", "engine_root", "identity_routes_file", "embed_url") if routed else
                          ("enabled", "engine_root", "spool_dir", "user_id", "agent_id")),
        "colleague-catalog": ("enabled", "engine_root", "roles"),
        # An empty cluster is a valid cluster: the node map is administrator
        # supplied, so it is not required here — only the switch and the
        # engine root the tools read from.
        "lan-cluster": ("enabled", "engine_root", "state_dir"),
    }.items():
        entry = (entries.get(name) or {}).get("settings") or {}
        missing = [key for key in required if not entry.get(key)]
        registered = name in enabled and name not in disabled
        add(name, registered and not missing,
            ("missing: " + ", ".join(missing)) if missing else ("not enabled" if not registered else "configured; runtime probe still required"))

    durable = ((entries.get("durable-dispatch") or {}).get("settings") or {})
    active = _service_active('superagent-worker-' + profile + '.service') is True
    unit_path = Path.home() / ".config/systemd/user" / ("superagent-worker-" + profile + ".service")
    aligned = False
    detail = "service not verified active"
    if active:
        detail = "service active but actual queue, runner, or policy alignment is not verified"
        try:
            unit = unit_path.read_text(encoding="utf-8")
            command = next(line[len("ExecStart="):] for line in unit.splitlines() if line.startswith("ExecStart="))
            argv = shlex.split(command)
            service_queue = argv[argv.index("--root") + 1].replace("%%", "%")
            runner_path = Path(argv[argv.index("--runner-config") + 1].replace("%%", "%"))
            if not runner_path.is_absolute():
                raise ValueError("runner path is not absolute")
            runner = json.loads(runner_path.read_text(encoding="utf-8"))
            if not isinstance(runner, list) or not all(isinstance(v, str) for v in runner):
                raise ValueError("invalid runner")
            policy_path = Path(runner[runner.index("--policy") + 1])
            if not policy_path.is_absolute():
                raise ValueError("policy path is not absolute")
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            configured_engine = Path(durable.get("engine_root", ""))
            module_runner = len(runner) >= 3 and runner[1:3] == ["-m", "agentcore.analysis_runner"]
            file_runner = len(runner) >= 2 and Path(runner[1]).resolve() == (configured_engine.resolve() / "agentcore/analysis_runner.py")
            # Unit contents alone cannot establish which Python module is loaded.
            # Leave module-mode ownership unverified until process state is checked.
            runner_verified = file_runner and not module_runner
            queue_matches = (isinstance(durable.get("queue_root"), str)
                             and Path(durable["queue_root"]).is_absolute()
                             and service_queue == durable["queue_root"])
            def canonical_roots(value):
                if not isinstance(value, list) or not value or any(
                        not isinstance(item, str) or not Path(item).is_absolute() for item in value):
                    raise ValueError("invalid allowed roots")
                return {str(Path(item).resolve(strict=True)) for item in value}
            roots_match = canonical_roots(policy.get("allowed_roots")) == canonical_roots(durable.get("allowed_roots"))
            denied_match = policy.get("denied_globs", []) == durable.get("denied_globs", [])
            aligned = (configured_engine.is_absolute() and queue_matches and roots_match
                       and denied_match and runner_verified)
            if not roots_match or not denied_match:
                detail = "actual worker policy differs from gateway allowed roots or denied patterns"
            elif not queue_matches:
                detail = "actual worker queue differs from gateway queue"
            elif module_runner:
                detail = "module runner configured; loaded module ownership not verified"
            elif aligned:
                detail = "active; actual saved queue, runner and policy match; process state not checked"
        except (OSError, ValueError, TypeError, StopIteration, IndexError, AttributeError):
            pass
    add("durable-worker", aligned, detail)
    if routed:
        results.extend(_routed_memory(home, profile, bridge, localvec)
                      if memory.get('provider') == 'localvec' else
                      [(name, False, 'routed memory requires localvec provider') for name in (
                          'memory-identity-policy', 'memory-recall', 'memory-ingestion-worker', 'memory-store-alignment')])
    else:
        results.extend(_legacy_memory(home, profile, entries, localvec, memory))
    delivery = (durable.get("deliver_completions") is True and
                durable.get("direct_delivery_enabled") is True and
                bool(durable.get("delivery_transport_file")))
    add("completion-delivery", delivery, "configured; real platform delivery not verified" if delivery else "direct completion delivery is not configured")
    add("colleague-team", False, "real provisioning and lifecycle integration is not established")
    return results


def _legacy_memory(home, profile, entries, localvec, memory):
    results = []
    def add(name, ok, detail):
        results.append((name, bool(ok), detail))
    recall = localvec.get("recall_cmd")
    if memory.get("provider") == "localvec" and isinstance(recall, list) and recall:
        scripts = [Path(part).expanduser() for part in recall if isinstance(part, str) and part.endswith(".py")]
        recall_configured = bool(scripts) and all(p.is_absolute() and p.is_file() for p in scripts)
    else:
        recall_configured = False
    add("memory-recall", recall_configured,
        "configured command exists; recall semantics and backend still require verification" if recall_configured
        else "localvec recall command is absent or its script is missing")
    memory_worker_active = _service_active('superagent-memory-' + profile + '.service') is True
    add("memory-ingestion-worker", memory_worker_active,
        "service active; store/principal alignment still requires verification" if memory_worker_active
        else "no managed automatic ingestion worker verified")
    aligned_memory = False
    if memory_worker_active and isinstance(recall, list):
        try:
            unit_path = Path.home() / ".config/systemd/user" / ("superagent-memory-" + profile + ".service")
            unit = unit_path.read_text(encoding="utf-8")
            env = {}
            command = []
            for line in unit.splitlines():
                if line.startswith("Environment="):
                    key, value = shlex.split(line[len("Environment="):])[0].split("=", 1)
                    env[key] = value.replace("%%", "%")
                elif line.startswith("ExecStart="):
                    command = [part.replace("%%", "%") for part in shlex.split(line[len("ExecStart="):])]
            def arg(argv, name):
                return argv[argv.index(name) + 1]
            bridge = ((entries.get("memory-bridge") or {}).get("settings") or {})
            aligned_memory = (
                env.get("SUPERAGENT_MEMORY_DATA_DIR") == arg(recall, "--data-dir")
                and env.get("SUPERAGENT_MEMORY_USER") == arg(recall, "--user") == bridge.get("user_id") == arg(command, "--user")
                and env.get("SUPERAGENT_MEMORY_AGENT") == arg(recall, "--agent") == bridge.get("agent_id") == arg(command, "--agent")
                and arg(recall, "--profile") == profile == arg(command, "--profile") == localvec.get("profile")
                and env.get("SUPERAGENT_MEMORY_EMBED_URL") == arg(recall, "--embed-url")
                and arg(command, "--spool") == bridge.get("spool_dir")
                and localvec.get("sqlite_db") == str(Path(arg(recall, "--data-dir")) / "coding_memory_cv.db")
            )
        except (OSError, ValueError, TypeError, IndexError, KeyError):
            pass
    add("memory-store-alignment", aligned_memory,
        "saved store/principal settings match; live behavior not verified" if aligned_memory else
        "ingestion-to-recall store and principal binding not verified")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-home", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    results = inspect(args.profile_home.resolve(), args.profile)
    for name, ok, detail in results:
        print(("[CONFIGURED]" if ok else "[OPEN]") + " " + name + ": " + detail)
    print("Configuration checks are not proof of real model, platform or end-to-end delivery.")
    return 0 if all(row[1] for row in results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.exit("readiness check failed: " + type(exc).__name__)
