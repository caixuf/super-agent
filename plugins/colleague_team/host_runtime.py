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


def install_dispatcher(*, service_profile_name, registry, supervisor,
                       member_profiles, profile_homes, profile_workdirs, state_dir,
                       backend=None, backend_factory=None, session_toolsets=None,
                       session_options_for_profile=None, all_work_quiescent=None):
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
            all_work_quiescent=all_work_quiescent,
        )
    except BaseException:
        host.close()
        raise


def install_safe_dispatcher(*, service_profile_name, registry, supervisor, entries, state_dir):
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
                              profile_workdirs=workdirs, state_dir=state)


def make_quiescence_verifier(gateway_host, profile_workdirs):
    """Authoritative quiescence verification that checks Gateway receipt status,
    session liveness and verifies no orphaned processes remain in the worker's private workspace.
    """
    import time
    import os
    from pathlib import Path

    def all_work_quiescent(member_id, generation, hermes_profile, hermes_session_id, admission_id):
        try:
            connection = gateway_host.connection_for_profile(hermes_profile)
            receipt = connection.call("prompt.receipt", session_id=hermes_session_id, admission_id=admission_id, include_result=True)
            if not isinstance(receipt, dict) or receipt.get("status") != "terminal":
                return {"all_owned_processes_terminal": False}

            snap = connection.call("session.resume", session_id=hermes_session_id)
            if not isinstance(snap, dict) or snap.get("running") is not False or snap.get("prompts") or snap.get("pending"):
                return {"all_owned_processes_terminal": False}

            workdir = profile_workdirs.get(hermes_profile)
            if workdir:
                workdir_path = Path(workdir).resolve()
                proc_dir = Path("/proc")
                if proc_dir.is_dir():
                    for entry in proc_dir.iterdir():
                        if not entry.name.isdigit():
                            continue
                        pid = int(entry.name)
                        if pid == os.getpid():
                            continue
                        try:
                            proc_cwd = Path(f"/proc/{pid}/cwd").resolve()
                            if proc_cwd == workdir_path:
                                with open(f"/proc/{pid}/status", "r") as f:
                                    status = f.read()
                                if "State:\tZ" not in status:
                                    return {"all_owned_processes_terminal": False}
                        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                            continue

            evidence_id = f"quiescence-{admission_id}-{generation}-{int(time.time() * 1000)}"
            return {"all_owned_processes_terminal": True, "evidence_id": evidence_id,
                    "member_id": member_id, "generation": generation,
                    "hermes_profile": hermes_profile, "hermes_session_id": hermes_session_id,
                    "admission_id": admission_id}
        except Exception:
            return {"all_owned_processes_terminal": False}

    return all_work_quiescent


def install_tool_dispatcher(*, service_profile_name, registry, supervisor, entries, state_dir, session_toolsets=None):
    """Attach explicitly configured, tool-enabled profiles to the live dispatcher.

    Profiles execute with full tools, skills, isolated workspaces, and
    verifiable quiescence attestation upon terminal completion.
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
        if not workdir.exists():
            workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        options[profile] = {"model": model.get("default"), "provider": "custom",
                            "base_url": provider.get("base_url"), "api_key": provider.get("api_key")}
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
                                                   profile_workdirs=workdirs)
        quiescence_verifier = make_quiescence_verifier(host, workdirs)
        return ColleagueGatewayBootstrap(
            service_profile_name=service_profile_name, registry=registry, supervisor=supervisor,
            backend=backend, member_profiles=member_profiles, profile_workdirs=workdirs,
            gateway_host=host, session_store_factory=session_store, state_dir=state,
            owns_gateway_host=True, session_toolsets=session_toolsets,
            session_options_for_profile=backend.session_options,
            all_work_quiescent=quiescence_verifier,
        )
    except BaseException:
        host.close()
        raise
