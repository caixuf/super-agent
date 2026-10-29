"""Explicit host lifecycle wiring for the colleague Gateway dispatcher.

This module constructs authorities only from trusted host-supplied objects. It
never starts a Gateway, provisions a member, fabricates readiness, or invents
quiescence evidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping
import os

from agentcore.colleague_registry import Registry, Supervisor
from agentcore.colleague_provisioner import Provisioner
from agentcore.colleague_hermes_runner import (
    HermesGatewaySessionCreator,
    HermesSessionBindingStore,
)
from agentcore.colleague_gateway_dispatch import GatewayColleagueDispatcher
from agentcore.colleague_gateway_host import HermesGatewayHost
from agentcore.colleague_gateway_service import (
    register_gateway_dispatch_service,
    unregister_gateway_dispatch_service,
)


class ColleagueGatewayBootstrap:
    """Build and register one dispatcher; unregister before closing its owned host.

    owns_gateway_host is an explicit ownership transfer. Set it only when this
    bootstrap is responsible for the supplied host's Gateway connections.
    """

    def __init__(
        self,
        *,
        service_profile_name: str,
        registry: Registry,
        supervisor: Supervisor,
        backend,
        member_profiles: Mapping[tuple[str, int], str],
        profile_workdirs: Mapping[str, str | Path],
        gateway_host: HermesGatewayHost,
        session_store_factory: Callable,
        state_dir: str | Path,
        owns_gateway_host: bool,
        all_work_quiescent: Callable | None = None,
        lock_timeout: float = 30,
    ):
        self.service_profile_name = service_profile_name
        self.gateway_host = gateway_host
        self.owns_gateway_host = owns_gateway_host
        self._registered = False
        self._closed = False

        try:
            member_profiles = dict(member_profiles)
            profile_workdirs = dict(profile_workdirs)
            self._validate(
                service_profile_name=service_profile_name,
                registry=registry,
                supervisor=supervisor,
                backend=backend,
                member_profiles=member_profiles,
                profile_workdirs=profile_workdirs,
                gateway_host=gateway_host,
                session_store_factory=session_store_factory,
                state_dir=state_dir,
                owns_gateway_host=owns_gateway_host,
                all_work_quiescent=all_work_quiescent,
            )
            self.state_dir = Path(state_dir).resolve(strict=True)
            self.member_profiles = dict(member_profiles)
            used_profiles = set(self.member_profiles.values())
            self.profile_workdirs = {
                profile: str(Path(profile_workdirs[profile]).expanduser().resolve(strict=True))
                for profile in used_profiles
            }
            self.provisioner = Provisioner(
                registry,
                supervisor=supervisor,
                backend=backend,
                state_dir=self.state_dir,
                lock_timeout=lock_timeout,
            )
            creator = HermesGatewaySessionCreator(
                connection_for_profile=gateway_host.connection_for_profile,
                cwd_for_profile=self.profile_workdirs.__getitem__,
                store_factory=session_store_factory,
            )
            self.bindings = HermesSessionBindingStore(
                registry=registry,
                supervisor=supervisor,
                profiles=self.member_profiles,
                creator=creator,
                state_dir=self.state_dir,
                lock_timeout=lock_timeout,
            )
            self.dispatcher = GatewayColleagueDispatcher(
                registry=registry,
                supervisor=supervisor,
                provisioner=self.provisioner,
                bindings=self.bindings,
                connection_for_profile=gateway_host.connection_for_profile,
                all_work_quiescent=all_work_quiescent,
                state_dir=self.state_dir,
                lock_timeout=lock_timeout,
            )
            register_gateway_dispatch_service(service_profile_name, self.dispatcher)
            self._registered = True
        except BaseException:
            # Registration is deliberately last, but keep rollback correct if its
            # implementation later gains a failure path after storing the service.
            if self._registered:
                unregister_gateway_dispatch_service(service_profile_name, self.dispatcher)
                self._registered = False
            if owns_gateway_host and isinstance(gateway_host, HermesGatewayHost):
                gateway_host.close()
            raise

    @staticmethod
    def _validate(
        *,
        service_profile_name,
        registry,
        supervisor,
        backend,
        member_profiles,
        profile_workdirs,
        gateway_host,
        session_store_factory,
        state_dir,
        owns_gateway_host,
        all_work_quiescent,
    ):
        from agentcore.colleague_gateway_service import _PROFILE

        if not isinstance(service_profile_name, str) or not _PROFILE.fullmatch(service_profile_name):
            raise ValueError("invalid_host_profile")
        if type(registry) is not Registry or type(supervisor) is not Supervisor:
            raise TypeError("real_registry_and_supervisor_required")
        if supervisor.profile_id != registry.profile_id:
            raise PermissionError("trusted_supervisor_required")
        if not isinstance(gateway_host, HermesGatewayHost):
            raise TypeError("hermes_gateway_host_required")
        if not callable(session_store_factory):
            raise TypeError("trusted_session_store_factory_required")
        if type(owns_gateway_host) is not bool:
            raise ValueError("explicit_gateway_host_ownership_required")
        if any(not callable(getattr(backend, method, None))
               for method in ("ensure", "inspect", "idle", "park")):
            raise TypeError("complete_provisioner_backend_required")
        if all_work_quiescent is not None and not callable(all_work_quiescent):
            raise TypeError("trusted_quiescence_callback_required")
        root = Path(state_dir)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise ValueError("private_state_directory_required")
        info = root.stat()
        if (info.st_mode & 0o077 or
                (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise ValueError("private_state_directory_required")
        if not isinstance(member_profiles, Mapping) or not member_profiles:
            raise ValueError("member_profile_map_required")
        if not isinstance(profile_workdirs, Mapping):
            raise ValueError("profile_workdirs_required")
        configured_profiles = gateway_host.configured_profiles
        used_profiles = set()
        seen_members = set()
        for key, profile in member_profiles.items():
            if (not isinstance(key, tuple) or len(key) != 2
                    or not isinstance(key[0], str) or type(key[1]) is not int or key[1] < 1
                    or not isinstance(profile, str)):
                raise ValueError("invalid_member_profile_map")
            if key in seen_members:
                raise ValueError("duplicate_member_profile_binding")
            seen_members.add(key)
            member = registry.role_for_member(
                supervisor=supervisor, member_id=key[0], generation=key[1])
            if (key[0] == registry.primary_member(supervisor=supervisor)
                    or member["role_id"] == "primary"):
                raise PermissionError("primary_protected")
            if profile not in configured_profiles:
                raise ValueError("member_gateway_profile_not_configured")
            used_profiles.add(profile)
        if len(used_profiles) != len(member_profiles):
            raise ValueError("profile_shared_between_members")
        if not used_profiles <= set(profile_workdirs):
            raise ValueError("profile_workdir_missing")
        for profile in used_profiles:
            workdir = Path(profile_workdirs[profile]).expanduser()
            if not workdir.is_absolute() or workdir.is_symlink() or not workdir.is_dir():
                raise ValueError("absolute_profile_workdir_required")
            resolved = workdir.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("absolute_profile_workdir_required")

    def close(self):
        """Unregister first; then close only the GatewayHost ownership transferred here."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._registered:
                unregister_gateway_dispatch_service(self.service_profile_name, self.dispatcher)
                self._registered = False
        finally:
            if self.owns_gateway_host:
                self.gateway_host.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
