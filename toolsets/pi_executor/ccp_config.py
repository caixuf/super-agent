"""Compatibility re-exports for the canonical agentcore CCP configuration."""
from agentcore.ccp_config import (
    CCP_KEY_ENV_VAR,
    DEFAULT_CCP_ALIAS,
    DEFAULT_CCP_BASE_URL,
    CcpCredentials,
    resolve_ccp_credentials,
    write_models_json,
)

__all__ = [
    "CCP_KEY_ENV_VAR",
    "DEFAULT_CCP_ALIAS",
    "DEFAULT_CCP_BASE_URL",
    "CcpCredentials",
    "resolve_ccp_credentials",
    "write_models_json",
]
