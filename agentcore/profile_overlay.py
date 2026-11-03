"""Generic Profile Overlay Packaging and Merging Engine.

Allows packaging persona (SOUL.md, AGENTS.md), specialized skills, domain
memories, and MCP service definitions from a specific profile into a
reusable, self-contained overlay tarball, and applying it idempotently to a
target profile on deployment.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any

logger = logging.getLogger(__name__)

EXCLUDE_SKILL_DIRS = frozenset({".locks", "__pycache__", ".git", ".venv"})
EXCLUDE_SKILL_FILES = frozenset({".usage.json", ".usage.json.lock", ".curator_state", ".bundled_manifest"})


def resolve_profile_dir(profile_spec: str | Path, base_hermes_dir: Path | None = None) -> Path:
    """Resolve a profile name or directory path to an existing profile Path."""
    path = Path(profile_spec).expanduser()
    if path.is_dir():
        return path.resolve()
    hermes_root = (base_hermes_dir or (Path.home() / ".hermes")).expanduser().resolve()
    candidate = hermes_root / "profiles" / str(profile_spec)
    if candidate.is_dir():
        return candidate.resolve()
    raise FileNotFoundError(f"Profile directory not found: {profile_spec} (checked {candidate})")


def collect_profile_overlay(
    profile_dir: Path,
    overlay_name: str,
) -> tuple[dict[str, tuple[bytes, int]], dict[str, Any]]:
    """Scan profile directory and collect overlay payload for packaging.

    Returns:
        files: dict mapping relative POSIX path in archive (prefixed with 'overlay/')
               to (content_bytes, file_mode).
        metadata: dictionary of overlay components.
    """
    profile_dir = profile_dir.resolve()
    files: dict[str, tuple[bytes, int]] = {}

    components: dict[str, Any] = {
        "has_soul": False,
        "has_agents": False,
        "skills": [],
        "memories": [],
        "mcp_servers": [],
    }

    # 1. SOUL.md & AGENTS.md
    soul_path = profile_dir / "SOUL.md"
    if soul_path.is_file():
        files["overlay/SOUL.md"] = (soul_path.read_bytes(), 0o644)
        components["has_soul"] = True

    agents_path = profile_dir / "AGENTS.md"
    if agents_path.is_file():
        files["overlay/AGENTS.md"] = (agents_path.read_bytes(), 0o644)
        components["has_agents"] = True

    # 2. Domain Memories (memories/*.md)
    memories_dir = profile_dir / "memories"
    if memories_dir.is_dir():
        for mem_file in sorted(memories_dir.glob("*.md")):
            if mem_file.is_file() and not mem_file.name.endswith(".lock"):
                rel_name = mem_file.name
                files[f"overlay/memories/{rel_name}"] = (mem_file.read_bytes(), 0o644)
                components["memories"].append(rel_name)

    # 3. Specialized Skills (skills/*)
    skills_dir = profile_dir / "skills"
    if skills_dir.is_dir():
        for skill_entry in sorted(skills_dir.iterdir()):
            if not skill_entry.is_dir() or skill_entry.name in EXCLUDE_SKILL_DIRS:
                continue
            skill_name = skill_entry.name
            components["skills"].append(skill_name)
            for item in sorted(skill_entry.rglob("*")):
                if not item.is_file():
                    continue
                if any(part in EXCLUDE_SKILL_DIRS for part in item.parts):
                    continue
                if item.name in EXCLUDE_SKILL_FILES or item.suffix in (".pyc", ".lock"):
                    continue
                rel_subpath = item.relative_to(skill_entry).as_posix()
                archive_key = f"overlay/skills/{skill_name}/{rel_subpath}"
                mode = 0o755 if (item.stat().st_mode & 0o111) else 0o644
                files[archive_key] = (item.read_bytes(), mode)

    # 4. MCP Servers and related env files from config.yaml
    config_path = profile_dir / "config.yaml"
    mcp_snippet: dict[str, Any] = {}
    if config_path.is_file():
        try:
            import yaml
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("Failed to parse config.yaml for overlay MCP scan: %s", exc)
            cfg = {}

        mcp_defs = cfg.get("mcp_servers") or cfg.get("mcpServers") or {}
        if isinstance(mcp_defs, dict) and mcp_defs:
            mcp_snippet = copy.deepcopy(mcp_defs)
            for srv_name, srv_conf in mcp_snippet.items():
                components["mcp_servers"].append(srv_name)
                if isinstance(srv_conf, dict) and "env_file" in srv_conf:
                    raw_env = srv_conf["env_file"]
                    env_p = Path(raw_env)
                    if not env_p.is_absolute():
                        env_p = (profile_dir / env_p).resolve()
                    if env_p.is_file():
                        env_content = env_p.read_bytes()
                        env_target_name = env_p.name
                        files[f"overlay/envs/{env_target_name}"] = (env_content, 0o644)
                        # Replace in snippet with relative reference
                        srv_conf["env_file"] = f"{{{{PROFILE_HOME}}}}/{env_target_name}"

            # Save snippet
            files["overlay/config_snippet.json"] = (
                (json.dumps({"mcp_servers": mcp_snippet}, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                0o644,
            )

    metadata = {
        "schema_version": 1,
        "overlay_name": overlay_name,
        "source_profile": str(profile_dir),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "components": components,
    }
    files["overlay/metadata.json"] = (
        (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        0o644,
    )

    return files, metadata


def apply_profile_overlay(
    profile_dir: Path,
    overlay_dir: Path,
    *,
    skip_overlay: bool = False,
) -> dict[str, Any]:
    """Idempotently apply overlay payload from overlay_dir into target profile_dir."""
    profile_dir = profile_dir.resolve()
    overlay_dir = overlay_dir.resolve()

    if skip_overlay or not overlay_dir.is_dir():
        return {"status": "skipped", "reason": "overlay_absent_or_skipped"}

    meta_file = overlay_dir / "metadata.json"
    if not meta_file.is_file():
        return {"status": "skipped", "reason": "no_overlay_metadata"}

    try:
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "reason": f"invalid_overlay_metadata: {exc}"}

    applied: dict[str, Any] = {
        "status": "applied",
        "overlay_name": metadata.get("overlay_name", "unknown"),
        "soul_updated": False,
        "agents_updated": False,
        "skills_applied": [],
        "memories_applied": [],
        "mcp_servers_applied": [],
    }

    profile_dir.mkdir(parents=True, exist_ok=True)

    # 1. SOUL.md
    src_soul = overlay_dir / "SOUL.md"
    if src_soul.is_file():
        dst_soul = profile_dir / "SOUL.md"
        if not dst_soul.exists() or dst_soul.read_bytes() != src_soul.read_bytes():
            if dst_soul.exists():
                backup = profile_dir / f"SOUL.md.bak-overlay-{int(dt.datetime.now().timestamp())}"
                shutil.copy2(dst_soul, backup)
            shutil.copy2(src_soul, dst_soul)
            applied["soul_updated"] = True

    # 2. AGENTS.md
    src_agents = overlay_dir / "AGENTS.md"
    if src_agents.is_file():
        dst_agents = profile_dir / "AGENTS.md"
        if not dst_agents.exists() or dst_agents.read_bytes() != src_agents.read_bytes():
            if dst_agents.exists():
                backup = profile_dir / f"AGENTS.md.bak-overlay-{int(dt.datetime.now().timestamp())}"
                shutil.copy2(dst_agents, backup)
            shutil.copy2(src_agents, dst_agents)
            applied["agents_updated"] = True

    # 3. Memories
    src_memories = overlay_dir / "memories"
    if src_memories.is_dir():
        dst_memories = profile_dir / "memories"
        dst_memories.mkdir(parents=True, exist_ok=True)
        for m_file in src_memories.glob("*.md"):
            dst_m = dst_memories / m_file.name
            if not dst_m.exists():
                shutil.copy2(m_file, dst_m)
                applied["memories_applied"].append(m_file.name)
            else:
                existing_text = dst_m.read_text(encoding="utf-8")
                overlay_text = m_file.read_text(encoding="utf-8")
                marker = f"<!-- overlay import: {m_file.name} -->"
                if marker not in existing_text and overlay_text.strip() not in existing_text:
                    merged = existing_text.rstrip() + f"\n\n{marker}\n" + overlay_text
                    dst_m.write_text(merged, encoding="utf-8")
                    applied["memories_applied"].append(f"{m_file.name}(merged)")

    # 4. Skills
    src_skills = overlay_dir / "skills"
    if src_skills.is_dir():
        dst_skills = profile_dir / "skills"
        dst_skills.mkdir(parents=True, exist_ok=True)
        for skill_entry in src_skills.iterdir():
            if not skill_entry.is_dir():
                continue
            target_skill_dir = dst_skills / skill_entry.name
            if target_skill_dir.exists():
                shutil.rmtree(target_skill_dir)
            shutil.copytree(skill_entry, target_skill_dir)
            applied["skills_applied"].append(skill_entry.name)

    # 5. Envs & MCP configuration snippet
    src_envs = overlay_dir / "envs"
    if src_envs.is_dir():
        for env_f in src_envs.iterdir():
            if env_f.is_file():
                dest_env = profile_dir / env_f.name
                shutil.copy2(env_f, dest_env)
                try:
                    dest_env.chmod(0o600)
                except OSError:
                    pass

    snippet_file = overlay_dir / "config_snippet.json"
    if snippet_file.is_file():
        try:
            snippet_data = json.loads(snippet_file.read_text(encoding="utf-8"))
            mcp_defs = snippet_data.get("mcp_servers", {})
            if mcp_defs:
                merged_count = merge_mcp_into_profile_config(profile_dir, mcp_defs)
                applied["mcp_servers_applied"] = list(mcp_defs.keys())
        except Exception as exc:
            logger.error("Failed to merge MCP snippet into config: %s", exc)

    return applied


def merge_mcp_into_profile_config(profile_dir: Path, mcp_servers: dict[str, Any]) -> int:
    """Merge mcp_servers dictionary into profile_dir / 'config.yaml' safely."""
    config_path = profile_dir / "config.yaml"
    profile_str = str(profile_dir)

    # Resolve {{PROFILE_HOME}} placeholders
    resolved_mcp: dict[str, Any] = {}
    for name, conf in mcp_servers.items():
        srv_conf = copy.deepcopy(conf)
        if isinstance(srv_conf, dict):
            if "env_file" in srv_conf and isinstance(srv_conf["env_file"], str):
                srv_conf["env_file"] = srv_conf["env_file"].replace("{{PROFILE_HOME}}", profile_str)
            if "args" in srv_conf and isinstance(srv_conf["args"], list):
                srv_conf["args"] = [
                    arg.replace("{{PROFILE_HOME}}", profile_str) if isinstance(arg, str) else arg
                    for arg in srv_conf["args"]
                ]
        resolved_mcp[name] = srv_conf

    try:
        import yaml
        if config_path.is_file():
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}

        existing_mcp = data.get("mcp_servers") or {}
        if not isinstance(existing_mcp, dict):
            existing_mcp = {}
        existing_mcp.update(resolved_mcp)
        data["mcp_servers"] = existing_mcp

        # Write atomically
        temp_cfg = config_path.with_suffix(".tmp")
        temp_cfg.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temp_cfg.replace(config_path)
        return len(resolved_mcp)
    except Exception as exc:
        logger.warning("YAML safe update failed (%s), falling back to append block", exc)
        # Fallback append if yaml parsing failed
        lines = ["\nmcp_servers:\n"]
        for srv_name, srv_conf in resolved_mcp.items():
            lines.append(f"  {srv_name}:\n")
            if isinstance(srv_conf, dict):
                for k, v in srv_conf.items():
                    if isinstance(v, list):
                        lines.append(f"    {k}:\n")
                        for item in v:
                            lines.append(f"      - {item}\n")
                    else:
                        lines.append(f"    {k}: {v}\n")
        with open(config_path, "a", encoding="utf-8") as f:
            f.writelines(lines)
        return len(resolved_mcp)
