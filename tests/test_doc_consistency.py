"""Documentation and Code Consistency Gate Tests.

Verifies that documentation across the repository remains consistent with actual code:
1. Relative markdown links point to existing files.
2. Backtick inline paths matching repository top-level prefixes exist on disk.
3. tools/superagent.py CLI invocations use valid registered subcommands without forbidden flags.
4. superagent.sh CLI invocations use supported subcommands without forbidden flags.
5. Known hallucinated terms do not reappear.
6. agentcore/PACKAGE_MAP.md accurately reflects all top-level modules and references.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXCLUDED_DIRS = frozenset({
    ".git", ".agents", "venv", ".venv", ".superagent-venv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules",
})


def is_generated_or_excluded(path: Path) -> bool:
    """Return True if path falls within generated, vendor, or cache directories."""
    return any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in path.parts)


def get_managed_markdown_files(repo_root: Path | None = None) -> list[Path]:
    """Retrieve all managed markdown files in the repository.

    Uses git ls-files if available, falling back to rglob excluding cache directories.
    """
    root = repo_root or REPO_ROOT
    try:
        proc = subprocess.run(
            ["git", "-c", "safe.directory=*", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        proc = None

    if proc is None or proc.returncode != 0:
        return sorted(
            p for p in root.rglob("*.md")
            if p.is_file() and not is_generated_or_excluded(p)
        )

    files = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or not line.endswith(".md"):
            continue
        p = root / line
        if not is_generated_or_excluded(p):
            files.append(p)
    return sorted(files)


def strip_fenced_code_blocks(text: str) -> str:
    """Strip fenced code blocks (``` or ~~~) from markdown content."""
    lines = text.splitlines()
    clean_lines = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_regex = re.compile(r"^ {0,3}(`{3,}|~{3,})")
    for line in lines:
        m = fence_regex.match(line)
        if m:
            marker = m.group(1)
            char = marker[0]
            length = len(marker)
            if not in_fence:
                in_fence = True
                fence_char = char
                fence_len = length
                continue
            elif in_fence and char == fence_char and length >= fence_len:
                in_fence = False
                continue
        if not in_fence:
            clean_lines.append(line)
    return "\n".join(clean_lines)


LINK_REGEX = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
IGNORED_SCHEMES = ("http://", "https://", "mailto:", "conversation://", "file://")


def extract_markdown_relative_links(text: str) -> list[str]:
    """Extract relative link targets from markdown (after fenced blocks are stripped)."""
    clean_text = strip_fenced_code_blocks(text)
    targets = []
    for match in LINK_REGEX.finditer(clean_text):
        target = match.group(2).strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        elif " " in target:
            target = target.split()[0].strip()

        if target.startswith("#") or target.startswith(IGNORED_SCHEMES):
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
            continue

        clean_target = target.split("#")[0].split("?")[0].strip()
        if clean_target:
            targets.append(clean_target)
    return targets


REPO_PREFIX_PATTERN = re.compile(
    r"(?:agentcore|tools|install|checks|scripts|runtime|plugins|docker|patches|skills|identity|toolsets|docs|tests)"
    r"/[A-Za-z0-9_./-]+\.(?:py|sh|md|yaml|yml|json|service|timer|patch)"
)
PLACEHOLDER_CHARS = frozenset("<>{}$*")


def extract_backtick_repo_paths(text: str) -> list[str]:
    """Extract repo relative file paths within single backtick spans.

    Skips placeholder/template paths and runtime generated paths.
    """
    results = []
    tokens = re.findall(r"(?<!`)(?:`)([^`\r\n]+)(?:`)(?!`)", text)
    for tok in tokens:
        for word in tok.split():
            if any(c in word for c in PLACEHOLDER_CHARS) or "..." in word:
                continue
            cleaned = word.strip("(),;:\"'[]{}")
            for m in REPO_PREFIX_PATTERN.finditer(cleaned):
                candidate = m.group(0)
                if any(c in candidate for c in PLACEHOLDER_CHARS) or "..." in candidate:
                    continue
                if candidate.startswith("identity/manifests/"):
                    continue
                if candidate.startswith("identity/overlays/") and candidate.endswith(".overlay.md"):
                    continue
                results.append(candidate)
    return results


OPTS_WITH_VALUE = frozenset({
    "--profile", "--profile-home", "--hermes-root", "--root",
    "--runtime-manifest", "--legacy-engine-root", "--expect-commit",
    "--drain-timeout", "--keep", "--release", "--compose-soul",
})

CLI_PREFIX_PATTERN = re.compile(
    r"^\s*(?:[-*+]\s+|\d+\.\s+|[|]\s*|[$#>]\s*)*(?:`{1,2}\s*)?"
    r"(?:(?:exec|time|nohup|sudo)\s+)?"
    r"(?:python[23]?\b\s+|(?:\$|\"\$)PY(?:THON)?(?:\"|\b)?\s*|\./)?"
)


def _is_valid_cli_prefix(before: str) -> bool:
    """Check if preceding characters represent a command execution context rather than prose."""
    m = CLI_PREFIX_PATTERN.match(before)
    return m is not None and m.end() == len(before)


def _join_multiline_commands(text: str) -> list[str]:
    """Join markdown lines that end with a trailing backslash."""
    lines = text.splitlines()
    joined = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.rstrip().endswith("\\") and i + 1 < len(lines):
            line = line.rstrip()[:-1].rstrip() + " " + lines[i + 1].strip()
            i += 1
        joined.append(line)
        i += 1
    return joined


def extract_superagent_py_invocations(text: str) -> list[dict]:
    """Extract tools/superagent.py invocations with subcommand and flag checks."""
    joined_lines = _join_multiline_commands(text)
    invocations = []
    for line in joined_lines:
        for m in re.finditer(r"(?:\btools/superagent\.py)\s+([^\n\r`|;)]+)", line):
            last_bt = line[:m.start()].rfind("`")
            before = line[last_bt + 1:m.start()] if last_bt != -1 else line[:m.start()]
            if not _is_valid_cli_prefix(before):
                continue

            raw_args = m.group(1).strip()
            tokens = []
            for tok in raw_args.split():
                clean_tok = tok.strip("(),;\"'[]")
                if any(ord(c) >= 128 for c in clean_tok):
                    break
                if clean_tok in (">", ">>", "<", "|", "&&", ";", "#"):
                    break
                tokens.append(clean_tok)
            if not tokens:
                continue

            subcmd = None
            has_apply = "--apply" in tokens
            has_compose_soul = "--compose-soul" in tokens

            idx = 0
            while idx < len(tokens):
                t = tokens[idx]
                if t in OPTS_WITH_VALUE:
                    idx += 2
                    continue
                if t.startswith("-"):
                    idx += 1
                    continue
                if subcmd is None:
                    subcmd = t
                idx += 1

            if subcmd or has_apply or has_compose_soul:
                invocations.append({
                    "subcmd": subcmd,
                    "has_apply": has_apply,
                    "has_compose_soul": has_compose_soul,
                    "line": line.strip(),
                })
    return invocations


def extract_superagent_sh_invocations(text: str) -> list[dict]:
    """Extract superagent.sh invocations with subcommand and flag checks."""
    joined_lines = _join_multiline_commands(text)
    invocations = []
    for line in joined_lines:
        for m in re.finditer(r"(?:\b(?:bash\s+|\./)?(?:\S*/)?superagent\.sh)\s+([^`\n\r#|;)]+)", line):
            last_bt = line[:m.start()].rfind("`")
            before = line[last_bt + 1:m.start()] if last_bt != -1 else line[:m.start()]
            if not _is_valid_cli_prefix(before):
                continue

            raw_args = m.group(1).strip()
            tokens = []
            for tok in raw_args.split():
                clean_tok = tok.strip("(),;\"'[]")
                if any(ord(c) >= 128 for c in clean_tok):
                    break
                if clean_tok in (">", ">>", "<", "|", "&&", ";", "#"):
                    break
                tokens.append(clean_tok)
            if not tokens:
                continue

            subcmd = None
            has_compose_soul = "--compose-soul" in tokens

            for tok in tokens:
                if tok.startswith("-"):
                    continue
                if any(c in tok for c in ("{", "}", "|", "/")):
                    break
                subcmd = tok
                break

            if subcmd or has_compose_soul:
                invocations.append({
                    "subcmd": subcmd,
                    "has_compose_soul": has_compose_soul,
                    "line": line.strip(),
                })
    return invocations


def get_registered_superagent_py_subcommands() -> frozenset[str]:
    """Extract dynamically registered subcommands from tools/superagent.py."""
    tool_path = REPO_ROOT / "tools" / "superagent.py"
    if tool_path.is_file():
        try:
            tree = ast.parse(tool_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
                    vals = [elt.value for elt in node.iter.elts if isinstance(elt, ast.Constant)]
                    if "plan" in vals and "doctor" in vals:
                        return frozenset(vals)
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "known" and isinstance(node.value, ast.Set):
                            vals = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
                            return frozenset(vals)
        except Exception:
            pass
    return frozenset({
        "plan", "install", "update", "doctor", "uninstall",
        "activate-release", "rollback", "prune-releases", "migrate-config",
        "candidate-hermes", "install-hermes-candidate", "switch-gateway-source",
    })


SUPPORTED_SUPERAGENT_SH_CMDS = frozenset({
    "package", "plan", "install", "update", "doctor", "uninstall",
    "candidate-hermes", "profile", "check", "preflight",
})

KNOWN_HALLUCINATED_IDENTIFIERS = (
    "durable_pool.sqlite",
    "liveness_ledgers",
    "memory_spool",
    "super-agent@",
    "super-agent-worker@",
    "agentcore-memory@.service",
    "sync_roles.py",
    "SKILLS_PATH",
    "ALL 7 DIAGNOSTIC GATES PASSED",
    "<!-- {{BUSINESS_OVERLAY}} -->",
    "recall_diagnostics_observation.py",
    "max_stagnation_steps",
    "enforce_worktree_harvest",
    "memory_footprint --profile",
)


def scan_hallucinated_identifiers(text: str) -> list[str]:
    """Find any occurrences of known hallucinated identifiers in text."""
    hits = []
    for token in KNOWN_HALLUCINATED_IDENTIFIERS:
        if token in text:
            hits.append(token)
    return hits


def parse_package_map(map_path: Path) -> tuple[set[str], set[str]]:
    """Parse PACKAGE_MAP.md to return (registered_top_level_modules, all_referenced_modules)."""
    text = map_path.read_text(encoding="utf-8")
    tokens = re.findall(r"`([^`\r\n]+)`", text)
    top_level = set()
    all_refs = set()
    for tok in tokens:
        tok = tok.strip()
        if re.match(r"^(?:memory/)?[a-zA-Z0-9_]+\.py$", tok):
            all_refs.add(tok)
            if not tok.startswith("memory/"):
                top_level.add(tok)
    return top_level, all_refs


# ---------------------------------------------------------------------------
# 6 Document Consistency Gate Tests
# ---------------------------------------------------------------------------


def test_markdown_relative_links_exist():
    """1. Verify relative links in all managed markdown files exist on disk."""
    violations = []
    for md_path in get_managed_markdown_files():
        targets = extract_markdown_relative_links(md_path.read_text(encoding="utf-8", errors="ignore"))
        for target in targets:
            if target.startswith("/"):
                resolved = REPO_ROOT / target.lstrip("/")
            else:
                resolved = (md_path.parent / target).resolve()
            if not resolved.exists():
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: relative link '{target}' -> missing path {resolved}"
                )
    assert not violations, "Found dead relative markdown links:\n" + "\n".join(violations)


def test_backtick_repo_paths_exist():
    """2. Verify backtick paths with repo top-level prefixes exist on disk."""
    violations = []
    for md_path in get_managed_markdown_files():
        paths = extract_backtick_repo_paths(md_path.read_text(encoding="utf-8", errors="ignore"))
        for path_str in paths:
            target = REPO_ROOT / path_str
            if not target.exists():
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: referenced path `{path_str}` does not exist"
                )
    assert not violations, "Found non-existent repo paths in inline backticks:\n" + "\n".join(violations)


def test_superagent_py_cli_invocations_valid():
    """3. Verify tools/superagent.py invocations use valid subcommands and omit forbidden flags."""
    valid_subcmds = get_registered_superagent_py_subcommands()
    violations = []
    for md_path in get_managed_markdown_files():
        invs = extract_superagent_py_invocations(md_path.read_text(encoding="utf-8", errors="ignore"))
        for inv in invs:
            subcmd = inv["subcmd"]
            if subcmd and subcmd not in valid_subcmds:
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: unknown tools/superagent.py subcommand '{subcmd}' in: {inv['line']}"
                )
            if inv["has_apply"]:
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: forbidden '--apply' used with tools/superagent.py in: {inv['line']}"
                )
            if inv["has_compose_soul"]:
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: forbidden '--compose-soul' used with tools/superagent.py in: {inv['line']}"
                )
    assert not violations, "Found invalid tools/superagent.py invocations:\n" + "\n".join(violations)


def test_superagent_sh_cli_invocations_valid():
    """4. Verify superagent.sh invocations use supported subcommands and omit forbidden flags."""
    violations = []
    for md_path in get_managed_markdown_files():
        invs = extract_superagent_sh_invocations(md_path.read_text(encoding="utf-8", errors="ignore"))
        for inv in invs:
            subcmd = inv["subcmd"]
            if subcmd and subcmd not in SUPPORTED_SUPERAGENT_SH_CMDS:
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: unsupported superagent.sh subcommand '{subcmd}' in: {inv['line']}"
                )
            if inv["has_compose_soul"]:
                violations.append(
                    f"{md_path.relative_to(REPO_ROOT)}: forbidden '--compose-soul' used with superagent.sh in: {inv['line']}"
                )
    assert not violations, "Found invalid superagent.sh invocations:\n" + "\n".join(violations)


def test_no_known_hallucinated_doc_identifiers():
    """5. Verify none of the known hallucinated terms appear in any managed markdown file."""
    violations = []
    for md_path in get_managed_markdown_files():
        hits = scan_hallucinated_identifiers(md_path.read_text(encoding="utf-8", errors="ignore"))
        for hit in hits:
            violations.append(
                f"{md_path.relative_to(REPO_ROOT)}: contains hallucinated token '{hit}'"
            )
    assert not violations, "Found known hallucinated identifiers in documentation:\n" + "\n".join(violations)


def test_package_map_matches_agentcore_modules():
    """6. Verify PACKAGE_MAP.md registers all top-level agentcore modules and references valid files."""
    agentcore_dir = REPO_ROOT / "agentcore"
    map_path = agentcore_dir / "PACKAGE_MAP.md"
    assert map_path.is_file(), f"Missing package map at {map_path}"

    top_level_actual = {
        p.name for p in agentcore_dir.glob("*.py")
        if p.name != "__init__.py"
    }

    registered_top_level, all_refs = parse_package_map(map_path)

    missing_from_map = sorted(top_level_actual - registered_top_level)
    assert not missing_from_map, (
        f"Top-level agentcore/*.py modules missing from PACKAGE_MAP.md: {missing_from_map}"
    )

    missing_on_disk = []
    for ref in sorted(all_refs):
        target = agentcore_dir / ref
        if not target.exists():
            missing_on_disk.append(f"{ref} -> {target}")
    assert not missing_on_disk, (
        "Modules referenced in PACKAGE_MAP.md do not exist under agentcore/:\n" + "\n".join(missing_on_disk)
    )


# ---------------------------------------------------------------------------
# Synthetic Unit Tests for Extractors
# ---------------------------------------------------------------------------


def test_unit_strip_fenced_code_blocks():
    md = (
        "Leading text\n"
        "```python\n"
        "[fake](link.md)\n"
        "```\n"
        "Middle text\n"
        "~~~bash\n"
        "[another](fake.md)\n"
        "~~~\n"
        "Trailing text\n"
    )
    stripped = strip_fenced_code_blocks(md)
    assert "[fake](link.md)" not in stripped
    assert "[another](fake.md)" not in stripped
    assert "Leading text" in stripped
    assert "Middle text" in stripped
    assert "Trailing text" in stripped


def test_unit_extract_markdown_relative_links():
    md = (
        "Here is a [valid link](docs/ARCH.md) and [another](sub/page.md#section).\n"
        "Here is an external [link](https://example.com/page).\n"
        "Here is mailto [email](mailto:test@example.com).\n"
        "Here is anchor only [anchor](#some-anchor).\n"
        "```markdown\n"
        "[ignore me](docs/IGNORED.md)\n"
        "```\n"
        "[with title](target.md 'Title description')\n"
    )
    links = extract_markdown_relative_links(md)
    assert links == ["docs/ARCH.md", "sub/page.md", "target.md"]


def test_unit_extract_backtick_repo_paths():
    text = (
        "Check `agentcore/pool.py` and `tools/superagent.py`.\n"
        "Template `<hermes_root>/plugins/platforms/foo.py` must be skipped.\n"
        "Placeholder `runtime/profiles/<profile>/config.yaml` must be skipped.\n"
        "Wildcard `identity/overlays/*.overlay.md` must be skipped.\n"
        "Runtime `identity/manifests/manifest.json` must be skipped.\n"
        "Ellipsis `scripts/.../task.sh` must be skipped.\n"
        "Command `python3 tools/superagent.py doctor` should find `tools/superagent.py`.\n"
    )
    paths = extract_backtick_repo_paths(text)
    assert "agentcore/pool.py" in paths
    assert "tools/superagent.py" in paths
    assert not any("<" in p or ">" in p or "*" in p or "..." in p for p in paths)
    assert not any(p.startswith("identity/manifests/") for p in paths)


def test_unit_extract_superagent_py_invocations():
    sample = (
        "```bash\n"
        "python3 tools/superagent.py doctor --profile dev\n"
        "python3 tools/superagent.py \\\n"
        "  --profile dev \\\n"
        "  doctor\n"
        "python3 tools/superagent.py prune-releases --profile p1 --keep 3 --apply\n"
        "tools/superagent.py pack --profile test\n"
        "tools/superagent.py update --compose-soul overlay.md\n"
        "```\n"
        "Prose explanation about tools/superagent.py should not be parsed as command.\n"
    )
    invs = extract_superagent_py_invocations(sample)
    assert len(invs) == 5
    assert invs[0]["subcmd"] == "doctor" and not invs[0]["has_apply"]
    assert invs[1]["subcmd"] == "doctor"
    assert invs[2]["subcmd"] == "prune-releases" and invs[2]["has_apply"]
    assert invs[3]["subcmd"] == "pack"
    assert invs[4]["subcmd"] == "update" and invs[4]["has_compose_soul"]


def test_unit_extract_superagent_sh_invocations():
    sample = (
        "./install/superagent.sh install --profile test --apply\n"
        "bash install/superagent.sh package --out-dir /tmp/out\n"
        "./install/superagent.sh \\\n"
        "  update --profile prod --apply\n"
        "./install/superagent.sh rollback --profile p1\n"
        "./install/superagent.sh install --compose-soul custom.md --apply\n"
        "install/superagent.sh plan/install/doctor explanation\n"
        "superagent.sh {package|plan|install} usage text\n"
    )
    invs = extract_superagent_sh_invocations(sample)
    assert len(invs) == 5
    assert invs[0]["subcmd"] == "install" and not invs[0]["has_compose_soul"]
    assert invs[1]["subcmd"] == "package"
    assert invs[2]["subcmd"] == "update"
    assert invs[3]["subcmd"] == "rollback"
    assert invs[4]["subcmd"] == "install" and invs[4]["has_compose_soul"]


def test_unit_scan_hallucinated_identifiers():
    sample = "Found durable_pool.sqlite in legacy text and SKILLS_PATH configured."
    hits = scan_hallucinated_identifiers(sample)
    assert "durable_pool.sqlite" in hits
    assert "SKILLS_PATH" in hits
    assert len(hits) == 2


def test_unit_parse_package_map(tmp_path):
    pkg_map = tmp_path / "PACKAGE_MAP.md"
    pkg_map.write_text(
        "# Package Map\n\n"
        "| `pool.py` | Execution | Core pool |\n"
        "| `worker.py` | Execution | Worker |\n"
        "| `memory/` | Memory | Subpackage (`memory/memory_store.py`) |\n",
        encoding="utf-8",
    )
    top_level, all_refs = parse_package_map(pkg_map)
    assert top_level == {"pool.py", "worker.py"}
    assert all_refs == {"pool.py", "worker.py", "memory/memory_store.py"}
