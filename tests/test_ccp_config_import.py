"""Import-boundary and install-package checks for CCP resolver ownership."""
import os
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]

def _run(code, *, pythonpath, cwd):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(item) for item in pythonpath)
    return subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env,
                          text=True, capture_output=True, timeout=20)

def test_analysis_adapter_imports_with_hermes_toolsets_module_first(tmp_path):
    shadow = tmp_path / "hermes"
    shadow.mkdir()
    (shadow / "toolsets.py").write_text("HERMES_TOOLSETS_SENTINEL = True\n", encoding="utf-8")
    code = (
        "import toolsets; assert toolsets.HERMES_TOOLSETS_SENTINEL; "
        "from agentcore.ccp_analysis_model import model; "
        "from agentcore.ccp_config import resolve_ccp_credentials; "
        "assert callable(model) and callable(resolve_ccp_credentials)"
    )
    result = _run(code, pythonpath=[shadow, REPO], cwd=tmp_path)
    assert result.returncode == 0, result.stderr

def test_analysis_adapter_imports_when_hermes_toolsets_is_preloaded(tmp_path):
    shadow = tmp_path / "hermes"
    shadow.mkdir()
    (shadow / "toolsets.py").write_text("HERMES_TOOLSETS_SENTINEL = True\n", encoding="utf-8")
    code = (
        "import toolsets, sys; assert toolsets.HERMES_TOOLSETS_SENTINEL; "
        "assert sys.modules['toolsets'] is toolsets; "
        "import agentcore.ccp_analysis_model as adapter; "
        "assert callable(adapter.model)"
    )
    result = _run(code, pythonpath=[shadow, REPO], cwd=tmp_path)
    assert result.returncode == 0, result.stderr

def test_legacy_pi_executor_resolver_api_reexports_canonical_symbols(tmp_path):
    code = (
        "from agentcore.ccp_config import CcpCredentials, resolve_ccp_credentials, write_models_json; "
        "from toolsets.pi_executor.ccp_config import CcpCredentials as OldCredentials, "
        "resolve_ccp_credentials as old_resolver, write_models_json as old_writer; "
        "assert OldCredentials is CcpCredentials; "
        "assert old_resolver is resolve_ccp_credentials; "
        "assert old_writer is write_models_json"
    )
    result = _run(code, pythonpath=[REPO], cwd=tmp_path)
    assert result.returncode == 0, result.stderr

def test_agentcore_package_discovery_includes_canonical_resolver():
    from setuptools import find_packages
    packages = find_packages(where=str(REPO), include=["agentcore*"])
    assert "agentcore" in packages
    assert (REPO / "agentcore" / "ccp_config.py").is_file()
    assert (REPO / "agentcore" / "ccp_analysis_model.py").is_file()
    assert not any(name == "toolsets" or name.startswith("toolsets.") for name in packages)


def test_resolver_reads_simple_yaml_without_a_third_party_parser(tmp_path):
    from agentcore.ccp_config import resolve_ccp_credentials
    profile = tmp_path / 'config.yaml'
    profile.write_text('providers:\n  litellm:\n    base_url: http://127.0.0.1:4000/v1\n'
                       '    key_env: LOCAL_TEST_KEY\n', encoding='utf-8')
    result = resolve_ccp_credentials(env={'LOCAL_TEST_KEY': 'test-value'},
                                     hermes_config_path=profile,
                                     litellm_config_path=tmp_path / 'missing.yaml', alias='flash')
    assert result.alias == 'flash' and result.api_key == 'test-value'
    assert result.base_url == 'http://127.0.0.1:4000/v1'
