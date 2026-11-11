#!/usr/bin/env python3
"""Prepare and manage LobeChat web frontend deployment for Super Agent."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
from typing import Optional

_INSTALL_DIR = Path(__file__).resolve().parent
if str(_INSTALL_DIR) not in sys.path:
    sys.path.insert(0, str(_INSTALL_DIR))

from setup_worker import quote_systemd


def render_lobechat_compose(
    *,
    port: int = 3210,
    api_url: str = "http://host.docker.internal:8642/v1",
    api_key: str = "",
    access_code: str = "",
    container_name: str = "superagent-lobechat",
    image: str = "lobehub/lobe-chat:latest",
) -> str:
    """Render docker-compose.yml content for LobeChat connecting to Hermes API Server."""
    env_lines = [
        f"      - OPENAI_API_KEY={api_key}",
        f"      - OPENAI_PROXY_URL={api_url}",
    ]
    if access_code:
        env_lines.append(f"      - ACCESS_CODE={access_code}")

    env_block = "\n".join(env_lines)

    return (
        'services:\n'
        '  lobe-chat:\n'
        f'    image: {image}\n'
        f'    container_name: {container_name}\n'
        '    restart: unless-stopped\n'
        '    ports:\n'
        f'      - "{port}:3210"\n'
        '    environment:\n'
        f'{env_block}\n'
        '    extra_hosts:\n'
        '      - "host.docker.internal:host-gateway"\n'
    )


def render_lobechat_service(
    *,
    compose_file: Path,
    working_dir: Path,
    service_name: str = "superagent-lobechat.service",
) -> str:
    """Render a systemd user unit to manage the LobeChat docker compose service."""
    compose_path_str = quote_systemd(str(compose_file))
    workdir_str = quote_systemd(str(working_dir))
    return (
        "[Unit]\n"
        "Description=Super Agent LobeChat Web UI Service\n"
        "After=network-online.target docker.service\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workdir_str}\n"
        f"ExecStart=/usr/bin/docker compose -f {compose_path_str} up\n"
        f"ExecStop=/usr/bin/docker compose -f {compose_path_str} down\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "TimeoutStopSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=3210, help="LobeChat listen port (default 3210)")
    p.add_argument(
        "--api-url",
        default="http://host.docker.internal:8642/v1",
        help="Hermes API Server endpoint accessible from container (default http://host.docker.internal:8642/v1)",
    )
    p.add_argument("--api-key", default="", help="Hermes API Server key (optional)")
    p.add_argument("--access-code", default="", help="LobeChat web access code password protection (optional)")
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path.home() / ".hermes/lobechat",
        help="Output directory for docker-compose file (default ~/.hermes/lobechat)",
    )
    p.add_argument(
        "--unit-dir",
        type=Path,
        default=Path.home() / ".config/systemd/user",
        help="Systemd user unit directory",
    )
    p.add_argument("--with-service", action="store_true", help="Also generate systemd user service unit")
    p.add_argument("--apply", action="store_true", help="Write configuration files to disk")

    a = p.parse_args()

    if not 1 <= a.port <= 65535:
        p.error("port must be between 1 and 65535")

    compose_yaml = render_lobechat_compose(
        port=a.port,
        api_url=a.api_url,
        api_key=a.api_key,
        access_code=a.access_code,
    )

    compose_path = a.out_dir / "docker-compose.yml"
    service_path = a.unit_dir / "superagent-lobechat.service"

    print("=== LobeChat Deployment Configuration ===")
    print(f"Target directory: {a.out_dir}")
    print(f"Web Port:        {a.port}")
    print(f"API Endpoint:    {a.api_url}")
    print("\n--- docker-compose.yml ---")
    print(compose_yaml)

    if a.with_service:
        service_content = render_lobechat_service(
            compose_file=compose_path,
            working_dir=a.out_dir,
        )
        print("\n--- superagent-lobechat.service ---")
        print(service_content)

    if not a.apply:
        print("\n[Dry-run] Re-run with --apply to write files.")
        return 0

    a.out_dir.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(compose_yaml, encoding="utf-8")
    print(f"[OK] Wrote {compose_path}")

    # Also create a quick-run script
    run_script = a.out_dir / "start.sh"
    run_script.write_text("#!/usr/bin/env bash\nexec docker compose up -d\n", encoding="utf-8")
    run_script.chmod(0o755)

    stop_script = a.out_dir / "stop.sh"
    stop_script.write_text("#!/usr/bin/env bash\nexec docker compose down\n", encoding="utf-8")
    stop_script.chmod(0o755)

    if a.with_service:
        a.unit_dir.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service_content, encoding="utf-8")
        print(f"[OK] Wrote {service_path}")
        print("To enable systemd service: systemctl --user daemon-reload && systemctl --user enable --now superagent-lobechat.service")
    else:
        print(f"To start LobeChat now: cd {a.out_dir} && ./start.sh")

    return 0


if __name__ == "__main__":
    sys.exit(main())

