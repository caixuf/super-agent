#!/usr/bin/env python3
"""Build an immutable HFC wheel overlay; never edit an installed environment.

The approved input is the ACK-repaired wheel, NOT upstream's unchanged v5.0.13.
The transport/ACK code is preserved byte-for-byte except for one render seam.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / 'integrations/hfc/final-render.lock.json'
SERVER_PATH = 'hermes_feishu_card/server.py'
RENDER_SEAM = '''    if card_config.get("streaming_mode") is True and result.card.get("schema") == "2.0":'''
CONFIG_SEAM = 'def normalize_card_config(value: object, *, path: str) -> None:\n    if not isinstance(value, dict):\n        return\n'
CONFIG_OVERLAY = '''    if "final_renderer" in value:
        choice = value["final_renderer"]
        if not isinstance(choice, str) or choice.strip().lower() not in {"hfc", "shared"}:
            raise ValueError(f"{path}.final_renderer must be hfc or shared")
        value["final_renderer"] = choice.strip().lower()
'''
RENDER_OVERLAY = '''    # SUPER_AGENT_SHARED_FINAL_RENDER_V1: presentation only; HFC owns all I/O.
    if card_config.get("final_renderer") == "shared":
        from .shared_final_render import project_final_result
        previous_result = result
        result = project_final_result(
            result, display_view(session), title=title, enabled=True,
        )
        if session.status == "completed" and session.delivery_kind == "chat":
            app[DIAGNOSTICS_KEY]["final_renderer"] = {
                "configured": "shared",
                "last_projection": "shared" if result is not previous_result else "hfc_fallback",
            }

'''


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_wheel(base: Path, output: Path, *, expected_sha256: str,
                renderer: Path | None = None, projection: Path | None = None) -> dict:
    """Return provenance only after validating and atomically publishing the wheel."""
    blob = base.read_bytes()
    if digest(blob) != expected_sha256:
        raise ValueError('base wheel digest does not match approved input')
    if base.name != 'hermes_feishu_streaming_card-5.0.13-py3-none-any.whl':
        raise ValueError('unexpected wheel identity')
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('duplicate wheel members')
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name:
                raise ValueError('unsafe wheel path')
        if any(name.endswith(('RECORD.jws', 'RECORD.p7s')) for name in names):
            raise ValueError('signed input wheel cannot be overlaid')
        files = {name: archive.read(name) for name in names if not name.endswith('/')}
    metadata = 'hermes_feishu_streaming_card-5.0.13.dist-info/METADATA'
    if (b'Name: hermes-feishu-streaming-card\n' not in files.get(metadata, b'')
            or b'Version: 5.0.13\n' not in files.get(metadata, b'')):
        raise ValueError('wheel metadata mismatch')
    server = files[SERVER_PATH].decode('utf-8')
    if server.count(RENDER_SEAM) != 1 or 'SUPER_AGENT_SHARED_FINAL_RENDER_V1' in server:
        raise ValueError('render seam drift or overlay already applied')
    server = server.replace(RENDER_SEAM, RENDER_OVERLAY + RENDER_SEAM, 1)
    compile(server, SERVER_PATH, 'exec')
    config_path = 'hermes_feishu_card/config_rules.py'
    config_source = files[config_path].decode('utf-8')
    if config_source.count(CONFIG_SEAM) != 1:
        raise ValueError('config validation seam drift')
    config_source = config_source.replace(CONFIG_SEAM, CONFIG_SEAM + CONFIG_OVERLAY, 1)
    compile(config_source, config_path, 'exec')
    renderer = renderer or ROOT / 'agentcore/card_markdown.py' 
    projection = projection or ROOT / 'integrations/hfc/final_render.py'
    extras = {
        'hermes_feishu_card/shared_card_markdown.py': renderer.read_bytes(),
        'hermes_feishu_card/shared_final_render.py': projection.read_bytes(),
    }
    for name, content in extras.items():
        compile(content, name, 'exec')
        if name in files:
            raise ValueError('wheel already owns overlay module')
    provenance = {
        'schema_version': 1, 'overlay': 'shared-final-render-v1',
        'base_wheel_sha256': expected_sha256,
        'modules': {name: digest(data) for name, data in extras.items()},
        'base_server_sha256': digest(files[SERVER_PATH]),
        'server_sha256': digest(server.encode()),
    }
    provenance['base_config_rules_sha256'] = digest(files[config_path])
    provenance['config_rules_sha256'] = digest(config_source.encode())
    files[config_path] = config_source.encode()
    files[SERVER_PATH] = server.encode()
    files.update(extras)
    files['hermes_feishu_card/shared_final_render_provenance.json'] = (
        json.dumps(provenance, indent=2, sort_keys=True) + '\n'
    ).encode()
    record = 'hermes_feishu_streaming_card-5.0.13.dist-info/RECORD'
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\n')
    for name in sorted(files):
        if name != record:
            data = files[name]
            value = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
            writer.writerow((name, 'sha256=' + value, len(data)))
    writer.writerow((record, '', ''))
    files[record] = buffer.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / base.name
    if target.exists():
        raise FileExistsError('output wheel already exists; use a new candidate directory')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    # Exclusive creation never overwrites a running/reviewed artifact.
    with target.open('xb') as stream:
        stream.write(data.getvalue())
    return {**provenance, 'wheel_sha256': digest(data.getvalue()), 'wheel': str(target)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-wheel', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--lock', type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    lock = json.loads(args.lock.read_text())
    result = build_wheel(args.base_wheel, args.output_dir,
                         expected_sha256=lock['base_wheel_sha256'])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
