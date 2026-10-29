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
