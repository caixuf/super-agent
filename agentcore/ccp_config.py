"""CCP（Claude Code Provider Switcher）凭据解析与 models.json 生成。

凭据纪律（红线）
----------------
* 凭据只在**进程内**存在，仅经环境变量传给 Pi 子进程；
* **绝不** print / log / 入仓 / 传 CLI argv；
* 优先只读 ``~/.config/litellm/proxy.env`` 与 Hermes provider 配置，不扫描其他凭据。

解析顺序：
    1. 环境变量 ``PI_CCP_API_KEY``（调用方已经自己有）；
    2. Hermes profile ``config.yaml`` 的 ``providers.<name>.api_key`` /
       ``key_env``（默认读当前 profile 的 ``config.yaml``，provider 名由调用方给定）；
    3. litellm ``config.yaml`` 的 ``general_settings.master_key``（兜底）。

models.json 的 ``apiKey`` 写成 **环境变量引用** ``$PI_CCP_API_KEY``，
不落明文；Pi 在请求时从环境解析。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .model_context import load_config_subset

DEFAULT_CCP_BASE_URL = "http://127.0.0.1:4000/v1"
#: 默认走 CCP 的 `main` 分组，**不钉具体模型名**。
#:
#: 钉死模型名会绕过 `qianfan_window.py` 的白天/夜间窗口：那个脚本只在
#: 21:00 / 08:00 改写 registry 里 `main`/`pro`/`flash` 三个分组的成员，
#: 任何直接写 `qianfan-...` 的调用方都拿不到切换，白天还按夜间的账算。
#: 指分组名则自动跟随窗口，也不用在每个调用点重复一份模型名。
DEFAULT_CCP_ALIAS = "main"
# models.json 里用这个环境变量名引用 key，绝不写明文。
CCP_KEY_ENV_VAR = "PI_CCP_API_KEY"


@dataclass
class CcpCredentials:
    base_url: str
    alias: str
    api_key: str
    source: str

    def __repr__(self) -> str:  # 避免凭据被顺手打进日志 / traceback
        return (
            f"CcpCredentials(base_url={self.base_url!r}, alias={self.alias!r}, "
            f"api_key=<redacted len={len(self.api_key)}>, source={self.source!r})"
        )


def _hermes_profile_config_path() -> Path:
    env = os.environ.get("PI_EXECUTOR_HERMES_CONFIG")
    if env:
        return Path(env).expanduser()
    home = os.environ.get("HERMES_HOME") or f"~/.hermes/profiles/{os.environ.get('HERMES_PROFILE', 'default')}"
    return Path(home).expanduser() / "config.yaml"


def _litellm_config_path() -> Path:
    return Path(os.environ.get("PI_EXECUTOR_LITELLM_CONFIG", "~/.config/litellm/config.yaml")).expanduser()


def _load_yaml(path: Path) -> dict:
    """Read JSON or the mapping-only YAML subset used by CCP config.

    Complex YAML constructs are not supported; set PI_CCP_API_KEY when an
    installation uses anchors, flow mappings or other unsupported constructs.
    """
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return load_config_subset(path)


def _resolve_interpolation(value: str, env: Mapping[str, str]) -> str:
    """把 ``$VAR`` / ``${VAR}`` 解成环境变量值；非插值原样返回。"""
    v = value.strip()
    if v.startswith("${") and v.endswith("}"):
        return env.get(v[2:-1], "")
    if v.startswith("$"):
        return env.get(v[1:], "")
    return value


def resolve_ccp_credentials(
    *,
    env: Mapping[str, str] | None = None,
    provider: str = "litellm",
    base_url: str | None = None,
    alias: str | None = None,
    hermes_config_path: str | os.PathLike | None = None,
    litellm_config_path: str | os.PathLike | None = None,
) -> CcpCredentials:
    """按优先级解析 CCP 凭据。拿不到 key 直接抛，绝不臆造。"""
    envd: Mapping[str, str] = env if env is not None else os.environ

    key = ""
    source = ""
    resolved_base = base_url or DEFAULT_CCP_BASE_URL

    # 1) 环境变量
    if envd.get(CCP_KEY_ENV_VAR):
        key = envd[CCP_KEY_ENV_VAR]
        source = f"env:{CCP_KEY_ENV_VAR}"

    # 2) Hermes profile provider 配置
    if not key:
        cfg_path = Path(hermes_config_path).expanduser() if hermes_config_path else _hermes_profile_config_path()
        cfg = _load_yaml(cfg_path)
        prov = (cfg.get("providers") or {}).get(provider) or {}
        if isinstance(prov, dict):
            if not base_url and prov.get("base_url"):
                resolved_base = str(prov["base_url"])
            key_env = prov.get("key_env")
            if key_env and envd.get(key_env):
                key = envd[key_env]
                source = f"hermes:{cfg_path.name}:key_env={key_env}"
            elif prov.get("api_key"):
                key = _resolve_interpolation(str(prov["api_key"]), envd)
                source = f"hermes:{cfg_path.name}:providers.{provider}.api_key"

    # 3) litellm master_key 兜底
    if not key:
        lit_cfg_path = Path(litellm_config_path).expanduser() if litellm_config_path else _litellm_config_path()
        lit = _load_yaml(lit_cfg_path)
        master = ((lit.get("general_settings") or {}).get("master_key")) or lit.get("master_key")
        if master:
            key = _resolve_interpolation(str(master), envd)
            source = f"litellm:{lit_cfg_path.name}:master_key"

    if not key:
        raise RuntimeError(
            "解析不到 CCP 凭据：请设 PI_CCP_API_KEY，或确认 Hermes profile 的 "
            f"providers.{provider}.api_key / litellm master_key 可用。"
        )

    return CcpCredentials(
        base_url=resolved_base.rstrip("/"),
        alias=alias or DEFAULT_CCP_ALIAS,
        api_key=key,
        source=source,
    )


def write_models_json(
    agent_dir: str | os.PathLike,
    *,
    base_url: str = DEFAULT_CCP_BASE_URL,
    alias: str = DEFAULT_CCP_ALIAS,
    key_env_var: str = CCP_KEY_ENV_VAR,
    context_window: int = 128000,
    max_tokens: int = 8192,
) -> Path:
    """在隔离 agent dir 写 ``models.json``。

    ``apiKey`` 用环境变量引用（``$PI_CCP_API_KEY``），不落明文。
    ``compat`` 关掉 developer role / reasoning_effort —— CCP 后面的
    OpenAI 兼容转发对这些字段未必认。
    """
    agent_dir_p = Path(agent_dir).expanduser()
    agent_dir_p.mkdir(parents=True, exist_ok=True)
    doc = {
        "providers": {
            "ccp": {
                "baseUrl": base_url.rstrip("/"),
                "api": "openai-completions",
                "apiKey": "$" + key_env_var,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [
                    {
                        "id": alias,
                        "name": alias,
                        "reasoning": False,
                        "input": ["text"],
                        "contextWindow": context_window,
                        "maxTokens": max_tokens,
                    }
                ],
            }
        }
    }
    target = agent_dir_p / "models.json"
    fd, tmp = tempfile.mkstemp(dir=str(agent_dir_p), prefix=".models.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):  # pragma: no cover - 正常路径已 replace
            os.remove(tmp)
    return target
