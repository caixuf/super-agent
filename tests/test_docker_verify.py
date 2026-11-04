"""Release verification must build only a successfully exported commit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    source = tmp_path / "source"
    (source / "docker").mkdir(parents=True)
    shutil.copy2(REPO / "docker/verify.sh", source / "docker/verify.sh")
    (source / "docker/Dockerfile").write_text("FROM scratch\n")
    (source / "tracked").write_text("committed source\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=Verification Test",
        "-c", "user.email=verify@example.invalid", "commit", "-qm", "fixture",
    ], check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    docker = bindir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['VERIFY_DOCKER_CALLS'], 'a') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'build':\n"
        "    root = pathlib.Path(sys.argv[-1])\n"
        "    assert (root / 'tracked').read_text() == 'committed source\\n'\n"
        "    assert not (root / 'untracked').exists()\n"
        "sys.exit(int(os.environ.get('VERIFY_DOCKER_EXIT', '0')))\n"
    )
    docker.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               VERIFY_DOCKER_CALLS=str(calls), TMPDIR=str(tmp_path))
    return source, bindir, calls, env


def run_verify(checkout, ref="HEAD"):
    source, _, _, env = checkout
    return subprocess.run(["bash", str(source / "docker/verify.sh"), ref],
                          env=env, cwd=source, text=True, capture_output=True,
                          timeout=20)


def test_missing_revision_never_invokes_docker(checkout):
    result = run_verify(checkout, "does-not-exist")
    assert result.returncode != 0
    assert not checkout[2].exists(), result.stdout + result.stderr


def test_tree_object_never_invokes_docker(checkout):
    result = run_verify(checkout, "HEAD^{tree}")
    assert result.returncode != 0
    assert not checkout[2].exists(), result.stdout + result.stderr


def test_failed_archive_never_invokes_docker(checkout):
    _, bindir, calls, _ = checkout
    git = bindir / "git"
    git.write_text(
        "#!/bin/bash\n"
        f"real_git={shutil.which('git')!r}\n"
        "if [ \"$1\" = archive ]; then\n"
        "    \"$real_git\" \"$@\"\n"
        "    exit 7\n"
        "fi\n"
        "exec \"$real_git\" \"$@\"\n"
    )
    git.chmod(0o755)
    result = run_verify(checkout)
    assert result.returncode != 0
    assert not calls.exists(), result.stdout + result.stderr


def test_verification_uses_committed_source_not_worktree(checkout):
    source, _, calls, _ = checkout
    (source / "tracked").write_text("uncommitted source\n")
    (source / "untracked").write_text("must not reach container\n")
    result = run_verify(checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [args[0] for args in invocations] == ["build", "run"]
    assert invocations[1][1:4] == ["--rm", "--network", "none"]
    assert not Path(invocations[0][-1]).exists(), "temporary export was not cleaned"


def test_build_failure_never_runs_container(checkout):
    checkout[3]["VERIFY_DOCKER_EXIT"] = "9"
    result = run_verify(checkout)
    assert result.returncode != 0
    invocations = [json.loads(line) for line in checkout[2].read_text().splitlines()]
    assert [args[0] for args in invocations] == ["build"]
    assert not Path(invocations[0][-1]).exists()


def test_export_uses_resolved_commit_not_moving_ref(checkout):
    source, bindir, _, env = checkout
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                     text=True).strip()
    logged = source.parent / "archive-ref"
    env["VERIFY_ARCHIVE_REF"] = str(logged)
    git = bindir / "git"
    git.write_text(
        "#!/bin/bash\n"
        f"real_git={shutil.which('git')!r}\n"
        "if [ \"$1\" = archive ]; then\n"
        "    printf '%s' \"$3\" > \"$VERIFY_ARCHIVE_REF\"\n"
        "fi\n"
        "exec \"$real_git\" \"$@\"\n"
    )
    git.chmod(0o755)
    result = run_verify(checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert logged.read_text() == commit
