"""Host-side Gateway dialling for the colleague adapter.

Kept out of the engine package on purpose: the engine must not import the host's
runtime modules, so it takes a connector and this supplies the real one. Every
dial rediscovers the endpoint, because a Gateway may have been restarted.
"""
from __future__ import annotations

from contextlib import asynccontextmanager


def connect_profile(profile, home):
    """Return ``(GatewayConnection, expected gateway profile id)`` for one profile."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.gateway_connection import GatewayConnection
    from hermes_cli.gateway_client import connect_gateway
    from hermes_cli.gateway_runtime import discover_gateway_endpoint, gateway_profile_home

    expected_identity = str(gateway_profile_home(home))

    async def connect_for_profile():
        # A GatewayConnection may redial after an owner restart. Discover on every
        # dial so the ticket binds the current instance, never a stale one.
        discovered = discover_gateway_endpoint(home)
        endpoint = discovered.endpoint
        if endpoint is None or discovered.state != "ready":
            raise ConnectionError("configured_profile_gateway_not_ready")
        if endpoint.profile_id != expected_identity:
            raise PermissionError("gateway_profile_identity_mismatch")
        # Apply inside the GatewayConnection-owned thread/task, so ticket minting
        # resolves this profile without mutating process-wide HERMES_HOME.
        token = set_hermes_home_override(home)
        try:
            async with connect_gateway(endpoint=endpoint) as client:
                yield client
        finally:
            reset_hermes_home_override(token)

    connection = GatewayConnection(connect=asynccontextmanager(connect_for_profile))
    try:
        connection.start()
    except BaseException:
        connection.close()
        raise
    return connection, expected_identity


def session_store(connection):
    """Build the host's Gateway session store for one bound connection."""
    from hermes_cli.gateway_session_store import GatewaySessionStore
    return GatewaySessionStore(connection=connection)


def completion_sink_for_profile(ctx):
    """Use the already-configured direct-delivery queue for this host profile."""
    if ctx.get_config("gateway_dispatch.deliver_completions", False) is not True:
        return None
    import os
    import stat
    from pathlib import Path
    from hermes_cli.config import load_config_readonly
    from agentcore.pool import Pool
    config = load_config_readonly() or {}
    settings = (((config.get("plugins") or {}).get("entries") or {}).get("durable-dispatch") or {}).get("settings") or {}
    if (settings.get("enabled") is not True or settings.get("deliver_completions") is not True
            or settings.get("direct_delivery_enabled") is not True):
        raise ValueError("direct_completion_delivery_not_enabled")
    root = Path(settings.get("queue_root", ""))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("private_completion_queue_required")
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private_completion_queue_required")
    return Pool(root)


def install_dispatcher(*, service_profile_name, registry, supervisor,
                       member_profiles, profile_homes, profile_workdirs, state_dir,
                       backend=None, backend_factory=None, session_toolsets=None,
                       session_options_for_profile=None, all_work_quiescent=None, completion_sink=None):
    """Install the dispatcher using host-owned identities and a real backend.

    Call only from the host lifecycle. Member mapping, execution policy and
    readiness evidence are not accepted from model-generated tool arguments.
    The backend must satisfy Provisioner's real isolation/readiness contract;
    an endpoint being reachable alone is not sufficient.
    """
    from agentcore.colleague_gateway_bootstrap import ColleagueGatewayBootstrap
    from agentcore.colleague_gateway_host import HermesGatewayHost

    if (backend is None) == (backend_factory is None):
        raise ValueError("exactly_one_trusted_backend_required")
    profiles = {name: (home, "process-peer") for name, home in profile_homes.items()}
    host = HermesGatewayHost(profiles, connector=connect_profile)
    try:
        if backend_factory is not None:
            if not callable(backend_factory):
                raise ValueError("trusted_backend_factory_required")
            backend = backend_factory(host)
        from agentcore.colleague_safe_gateway_backend import SafeGatewayProfileBackend
        if isinstance(backend, SafeGatewayProfileBackend):
            if session_toolsets not in (None, ()) or session_options_for_profile is not None:
                raise ValueError("safe_backend_policy_mismatch")
            session_toolsets = ()
            session_options_for_profile = backend.session_options
        return ColleagueGatewayBootstrap(
            service_profile_name=service_profile_name, registry=registry, supervisor=supervisor,
            backend=backend, member_profiles=member_profiles, profile_workdirs=profile_workdirs,
            gateway_host=host, session_store_factory=session_store, state_dir=state_dir,
            owns_gateway_host=True, session_toolsets=session_toolsets,
            session_options_for_profile=session_options_for_profile,
            all_work_quiescent=all_work_quiescent, completion_sink=completion_sink,
        )
    except BaseException:
        host.close()
        raise





def profile_is_unserved(profile, home):
    """Observe a parked profile using its marker and the live multiplexer's identity."""
    import os
    import stat
    from pathlib import Path
    from hermes_cli.gateway_runtime_discovery import query_identify
    from hermes_cli.profiles import parked_marker_path, profile_is_standalone
    try:
        home = Path(home)
        if (not home.is_absolute() or home.is_symlink() or home.name != profile
                or home.parent.name != "profiles" or profile_is_standalone(home)):
            return False
        home = home.resolve(strict=True)
        info = parked_marker_path(home).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return False
        payload = query_identify(home.parent.parent, timeout=2.0)
        entries = payload.get("served_profiles") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("home"), str) for item in entries):
            return False
        return all(Path(item["home"]).resolve() != home for item in entries)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False



def serve_profile(profile, home):
    """Unpark one named profile and require a live host acknowledgement."""
    import os
    import stat
    from pathlib import Path
    from gateway.control_socket import request_serve_profile_hot
    from hermes_cli.gateway_runtime_discovery import query_identify
    from hermes_cli.profiles import parked_marker_path, profile_is_standalone

    home = Path(home)
    if (not home.is_absolute() or home.is_symlink() or home.name != profile
            or home.parent.name != "profiles" or profile_is_standalone(home)):
        raise ValueError("multiplexed_profile_home_required")
    home = home.resolve(strict=True)
    marker = parked_marker_path(home)
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("unsafe_profile_park_marker")
    marker.unlink()
    try:
        answer = request_serve_profile_hot(home.parent.parent, profile)
        if not isinstance(answer, dict) or answer.get("served") != profile or answer.get("error"):
            raise RuntimeError("profile_serve_unconfirmed")
        payload = query_identify(home.parent.parent, timeout=2.0)
        entries = payload.get("served_profiles") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or sum(
                isinstance(item, dict) and isinstance(item.get("home"), str)
                and Path(item["home"]).resolve() == home for item in entries) != 1:
            raise RuntimeError("profile_serve_identity_unconfirmed")
    except BaseException:
        # Persist the previous parked intent on ambiguous host responses.
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        raise


def unserve_profile(profile, home):
    """Park one multiplexed profile and require the host's live acknowledgement.

    This only stops profile routing. The caller must establish whole-work
    quiescence before calling; this function does not prove descendant exit.
    """
    import os
    import stat
    from pathlib import Path
    from gateway.control_socket import request_unserve_profile
    from hermes_cli.gateway_runtime_discovery import query_identify
    from hermes_cli.profiles import parked_marker_path, profile_is_standalone

    home = Path(home)
    if (not home.is_absolute() or home.is_symlink() or home.name != profile
            or home.parent.name != "profiles" or profile_is_standalone(home)):
        raise ValueError("multiplexed_profile_home_required")
    home = home.resolve(strict=True)
    host_home = home.parent.parent
    def served():
        payload = query_identify(host_home, timeout=2.0)
        entries = payload.get("served_profiles") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or any(not isinstance(item, dict) or not isinstance(item.get("home"), str) for item in entries):
            raise RuntimeError("host_profile_state_unknown")
        return any(Path(item["home"]).resolve() == home for item in entries)
    marker = parked_marker_path(home)
    if marker.exists() or marker.is_symlink():
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe_profile_park_marker")
        if not served():
            return
    else:
        if not served():
            raise RuntimeError("profile_ownership_unconfirmed")
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
    answer = request_unserve_profile(host_home, profile)
    if not isinstance(answer, dict) or answer.get("unserved") != profile or answer.get("error"):
        raise RuntimeError("profile_unserve_unconfirmed")
    if served():
        raise RuntimeError("profile_still_served")


def start_receipt_refresh(dispatcher, *, interval=15.0):
    """Refresh existing admissions until host unload; never submit or replay."""
    import logging
    import threading
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 5 <= interval <= 300:
        raise ValueError("invalid_receipt_refresh_interval")
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                dispatcher.refresh_pending(should_stop=stop.is_set)
            except Exception as exc:
                logging.getLogger(__name__).warning("colleague receipt refresh failed: %s", type(exc).__name__)
            if stop.wait(interval):
                break
    thread = threading.Thread(target=loop, name="colleague-receipts", daemon=True)
    thread.start()
    def close():
        stop.set()
        thread.join(timeout=2.0)
    return close


def install_safe_dispatcher(*, service_profile_name, registry, supervisor, entries, state_dir, completion_sink=None):
    """Attach explicitly configured, tool-free profiles to the live dispatcher.

    This does not provision profiles or grant tool-bearing work. Profile paths
    and member mappings come only from the host-owned plugin configuration.
    """
    from pathlib import Path
    import os
    try:
        from ruamel.yaml import YAML
        def load_yaml(path): return YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        import yaml
        def load_yaml(path): return yaml.safe_load(path.read_text(encoding="utf-8"))
    from agentcore.colleague_safe_gateway_backend import SafeGatewayProfileBackend

    if not isinstance(entries, list) or not entries:
        raise ValueError("trusted_worker_entries_required")
    member_profiles, homes, workdirs, options = {}, {}, {}, {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"member_id", "generation", "profile", "home"}:
            raise ValueError("invalid_trusted_worker_entry")
        member, generation, profile = entry["member_id"], entry["generation"], entry["profile"]
        home = Path(entry["home"])
        key = (member, generation)
        if key in member_profiles or profile in homes or not home.is_absolute():
            raise ValueError("duplicate_or_invalid_worker_binding")
        config = load_yaml(home / "config.yaml")
        if not isinstance(config, dict) or not isinstance(config.get("model"), dict):
            raise ValueError("worker_model_configuration_missing")
        model = config["model"]; provider_name = model.get("provider")
        provider = (config.get("providers") or {}).get(provider_name)
        if not isinstance(provider, dict):
            raise ValueError("explicit_worker_provider_required")
        # Gateway safe mode deliberately does not inherit profile providers.
        # Supply a host-selected explicit connection without publishing it in
        # the colleague registry or task payload.
        options[profile] = {"model": model.get("default"), "provider": "custom",
                            "base_url": provider.get("base_url"), "api_key": provider.get("api_key")}
        member_profiles[key] = profile
        homes[profile] = home
        workdirs[profile] = home
    state = Path(state_dir)
    if not state.is_absolute() or state.is_symlink():
        raise ValueError("private_state_directory_required")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("private_state_directory_required")
    def backend_factory(host):
        return SafeGatewayProfileBackend(gateway_host=host, member_profiles=member_profiles,
                                         profile_homes=homes, model_options=options)
    return install_dispatcher(service_profile_name=service_profile_name, registry=registry,
                              supervisor=supervisor, backend_factory=backend_factory,
                              member_profiles=member_profiles, profile_homes=homes,
                              profile_workdirs=workdirs, state_dir=state, completion_sink=completion_sink)


def make_quiescence_verifier(gateway_host, profile_workdirs):
    """Do not infer whole-attempt termination from session state or process cwd.

    Gateway-owned tool descendants may change directory or outlive the worker.
    An attempt-bound containment authority must be wired before reservations can
    be settled. Until then, keep their state unknown instead of issuing evidence.
    """
    def all_work_quiescent(member_id, generation, hermes_profile, hermes_session_id, admission_id):
        return {"all_owned_processes_terminal": False,
                "reason": "attempt_bound_containment_unavailable"}
    return all_work_quiescent


def _ensure_private_worker_directory(path):
    """Create or repair an owner-only workdir before it is bound to a colleague.

    ``Path.mkdir(exist_ok=True)`` does not tighten a pre-existing directory, so a
    profile created by an older tool can keep mode 0o775 and later fail the
    colleague plugin's ``private_directory_required`` gate. Repair that here while
    still refusing symlinks and foreign ownership.
    """
    import os
    import stat
    from pathlib import Path

    target = Path(path)
    try:
        info = target.lstat()
    except FileNotFoundError:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = target.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("private_worker_directory_required")
    if info.st_uid != os.getuid():
        raise ValueError("private_worker_directory_required")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(target, 0o700)
    if stat.S_IMODE(target.lstat().st_mode) & 0o077:
        raise ValueError("private_worker_directory_required")
    return target


def install_tool_dispatcher(*, service_profile_name, registry, supervisor, entries, state_dir, session_toolsets=None, completion_sink=None):
    """Attach explicitly configured, tool-enabled profiles to the live dispatcher.

    Profile directories are private, but execution isolation and whole-attempt
    quiescence are not established by the current Gateway integration.
    """
    from pathlib import Path
    import os
    try:
        from ruamel.yaml import YAML
        def load_yaml(path): return YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError:
        import yaml
        def load_yaml(path): return yaml.safe_load(path.read_text(encoding="utf-8"))
    from agentcore.colleague_tool_gateway_backend import ToolEnabledGatewayProfileBackend
    from agentcore.colleague_gateway_bootstrap import ColleagueGatewayBootstrap
    from agentcore.colleague_gateway_host import HermesGatewayHost

    if not isinstance(entries, list) or not entries:
        raise ValueError("trusted_worker_entries_required")
    member_profiles, homes, workdirs, options = {}, {}, {}, {}
    for entry in entries:
        if not isinstance(entry, dict) or not {"member_id", "generation", "profile", "home"}.issubset(set(entry)):
            raise ValueError("invalid_trusted_worker_entry")
        member, generation, profile = entry["member_id"], entry["generation"], entry["profile"]
        home = Path(entry["home"])
        workdir = Path(entry["workdir"]) if "workdir" in entry else home / "workspace"
        _ensure_private_worker_directory(workdir)
        key = (member, generation)
        if key in member_profiles or profile in homes or not home.is_absolute():
            raise ValueError("duplicate_or_invalid_worker_binding")
        config = load_yaml(home / "config.yaml")
        if not isinstance(config, dict) or not isinstance(config.get("model"), dict):
            raise ValueError("worker_model_configuration_missing")
        policy = entry.get("model_policy", "explicit")
        if policy == "profile":
            # Hermes resolves the model, provider, and credentials in the
            # member's own profile, including native OAuth and env-based keys.
            options[profile] = {}
        elif policy == "explicit":
            model = config["model"]; provider_name = model.get("provider")
            provider = (config.get("providers") or {}).get(provider_name)
            if not isinstance(provider, dict):
                raise ValueError("explicit_worker_provider_required")
            options[profile] = {"model": model.get("default"), "provider": "custom",
                                "base_url": provider.get("base_url"), "api_key": provider.get("api_key")}
        else:
            raise ValueError("invalid_worker_model_policy")
        member_profiles[key] = profile
        homes[profile] = home
        workdirs[profile] = workdir
    state = Path(state_dir)
    if not state.is_absolute() or state.is_symlink():
        raise ValueError("private_state_directory_required")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.stat().st_uid != os.getuid() or state.stat().st_mode & 0o077:
        raise ValueError("private_state_directory_required")

    profiles_map = {name: (home, "process-peer") for name, home in homes.items()}
    host = HermesGatewayHost(profiles_map, connector=connect_profile)
    try:
        backend = ToolEnabledGatewayProfileBackend(gateway_host=host, member_profiles=member_profiles,
                                                   profile_homes=homes, model_options=options,
                                                   profile_workdirs=workdirs, stop_profile=unserve_profile,
                                                   parked_profile=profile_is_unserved, start_profile=serve_profile)
        quiescence_verifier = make_quiescence_verifier(host, workdirs)
        return ColleagueGatewayBootstrap(
            service_profile_name=service_profile_name, registry=registry, supervisor=supervisor,
            backend=backend, member_profiles=member_profiles, profile_workdirs=workdirs,
            gateway_host=host, session_store_factory=session_store, state_dir=state,
            owns_gateway_host=True, session_toolsets=session_toolsets,
            session_options_for_profile=backend.session_options,
            all_work_quiescent=quiescence_verifier, completion_sink=completion_sink,
        )
    except BaseException:
        host.close()
        raise
