"""The "Condor is online and ready" notice carries a version, never a git error.

In the Docker image there is no git checkout. ``get_local_commit`` and
``get_current_branch`` hand back git's own stderr in that case, and the boot notice
printed "fatal: not a git repository ..." twice, as a branch and as a commit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from utils import updater

ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "PATH": "/usr/bin:/bin",
}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=ENV)


@pytest.mark.asyncio
async def test_a_directory_that_is_not_a_checkout_has_no_version(tmp_path):
    assert await updater.get_checkout_version(str(tmp_path)) == ("", "")


@pytest.mark.asyncio
async def test_a_checkout_without_a_commit_has_no_version(tmp_path):
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    assert await updater.get_checkout_version(str(tmp_path)) == ("", "")


@pytest.mark.asyncio
async def test_a_checkout_reports_its_branch_and_short_commit(tmp_path):
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    (tmp_path / "f.txt").write_text("x\n")
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-qm", "c", cwd=tmp_path)
    branch, commit = await updater.get_checkout_version(str(tmp_path))
    assert branch == "main"
    assert re.fullmatch(r"[0-9a-f]{7,12}", commit)


@pytest.mark.asyncio
async def test_a_missing_git_binary_or_directory_is_no_version_not_a_crash(
    monkeypatch, tmp_path
):
    async def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(updater, "_run_git", no_git)
    assert await updater.get_checkout_version(str(tmp_path)) == ("", "")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5bf3562e42b1708b06958e99c9bc20eee9e41c2f", "5bf3562"),
        ("  A1B2C3D4E5F6  ", "a1b2c3d"),
        ("unknown", ""),
        ("", ""),
        ("5bf356", ""),  # too short to be a commit
        ("5bf3562; rm -rf /", ""),
        ("fatal: not a git repository", ""),
    ],
)
def test_the_image_commit_is_a_hex_commit_or_nothing(monkeypatch, value, expected):
    monkeypatch.setenv("CONDOR_BUILD_COMMIT", value)
    assert updater.get_image_commit() == expected


def test_no_build_commit_in_the_environment_is_no_version(monkeypatch):
    monkeypatch.delenv("CONDOR_BUILD_COMMIT", raising=False)
    assert updater.get_image_commit() == ""


def test_the_label_names_the_checkout_then_the_image_then_nothing():
    assert updater.version_label("main", "abc1234", "def5678") == " (main @ abc1234)"
    assert updater.version_label("", "", "def5678") == " (image @ def5678)"
    assert updater.version_label("main", "", "def5678") == " (image @ def5678)"
    assert updater.version_label("", "", "") == ""
    assert updater.version_label("main", "", "") == ""


def test_the_boot_notice_builds_its_version_from_the_safe_helpers():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "version_label(branch, commit, get_image_commit())" in source
    assert "get_local_commit" not in source
    assert "get_current_branch" not in source


def test_the_image_carries_its_build_commit_to_the_running_app():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "CONDOR_BUILD_COMMIT=${VCS_REF}" in dockerfile
    assert "--build-arg VCS_REF=" in (
        ROOT / ".github/workflows/container.yml"
    ).read_text(encoding="utf-8")
