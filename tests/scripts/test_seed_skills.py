"""Tests for ``docker/seed_skills.py``.

The seed script runs at container boot to install admin-managed skills
listed in ``HERMES_DEFAULT_SKILLS``.  Its contract:

- Silent no-op when ``HERMES_DEFAULT_SKILLS`` is unset/empty (generic
  deploys).
- Each entry is fed to ``hermes_cli.skills_hub.do_install`` with
  ``skip_confirm=True`` and ``force=True`` (the admin-baked path must
  proceed past ``caution`` scan verdicts; the dedup gate ahead of it
  ensures ``force`` only fires on a genuine first install).
- Idempotent: skills already recorded in ``lock.json`` (matched by
  ``identifier`` field, prefix-tolerantly) are skipped without invoking
  the installer.  A skill's ``scripts/install.sh`` post-install hook,
  however, still runs on the skip path (it self-heals side effects that
  live outside the skill dir, e.g. a ``~/.local/bin`` wrapper symlink).
- Non-fatal: a single failed install emits a warning but the script
  exits 0 so boot continues.  A bad private-skill repo must not
  crash-loop the pod.
- Refuses URLs with embedded credentials; supports an ``@<name>`` suffix
  on bare entries for URL-sourced skills whose SKILL.md has no name.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "docker" / "seed_skills.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_seed_skills", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def lock_path(hermes_home):
    return hermes_home / "skills" / ".hub" / "lock.json"


@pytest.fixture
def fake_do_install(monkeypatch):
    """Inject a fake hermes_cli.skills_hub.do_install module so the seed
    script can be exercised without the real installer (no network, no
    quarantine, no scan).  Yields the MagicMock so tests can assert on
    calls and configure failure modes."""
    mock = MagicMock(name="do_install")

    fake_module = type(sys)("hermes_cli.skills_hub")
    fake_module.do_install = mock
    fake_pkg = type(sys)("hermes_cli")
    fake_pkg.skills_hub = fake_module

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.skills_hub", fake_module)
    return mock


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_bare_identifier():
    mod = _load_module()
    assert mod._parse_entries("foo/bar/skills/baz") == [
        ("foo/bar/skills/baz", "")
    ]


def test_parse_bare_identifier_with_name_override():
    mod = _load_module()
    assert mod._parse_entries("foo/bar/skills/baz@my-skill") == [
        ("foo/bar/skills/baz", "my-skill")
    ]


def test_parse_url():
    mod = _load_module()
    assert mod._parse_entries("https://example.com/path/SKILL.md") == [
        ("https://example.com/path/SKILL.md", "")
    ]


def test_parse_url_with_name_override():
    mod = _load_module()
    assert mod._parse_entries(
        "https://example.com/path/SKILL.md@my-skill"
    ) == [("https://example.com/path/SKILL.md", "my-skill")]


def test_parse_comma_and_newline_separated():
    mod = _load_module()
    raw = "  a/b/skills/c ,\n  d/e/skills/f@nm  \n\n https://x.example/SKILL.md \n"
    assert mod._parse_entries(raw) == [
        ("a/b/skills/c", ""),
        ("d/e/skills/f", "nm"),
        ("https://x.example/SKILL.md", ""),
    ]


def test_parse_skips_invalid_bare_entry(capsys):
    mod = _load_module()
    result = mod._parse_entries("no-slash, foo/bar/skills/baz")
    assert result == [("foo/bar/skills/baz", "")]
    err = capsys.readouterr().err
    assert "no-slash" in err


def test_parse_rejects_authenticated_url(capsys):
    mod = _load_module()
    result = mod._parse_entries(
        "https://x-access-token:abc123@github.com/foo/bar/SKILL.md"
    )
    assert result == []
    err = capsys.readouterr().err
    assert "authenticated URL" in err


# ---------------------------------------------------------------------------
# main() — env-driven flow
# ---------------------------------------------------------------------------


def test_main_noop_when_env_unset(hermes_home, monkeypatch, fake_do_install):
    monkeypatch.delenv("HERMES_DEFAULT_SKILLS", raising=False)
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_not_called()


def test_main_noop_when_env_empty(hermes_home, monkeypatch, fake_do_install):
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "   \n  ")
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_not_called()


def test_main_installs_each_entry(hermes_home, monkeypatch, fake_do_install):
    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "foo/bar/skills/a, foo/bar/skills/b",
    )
    mod = _load_module()
    assert mod.main() == 0

    assert fake_do_install.call_count == 2
    # Verify both calls passed force=True, skip_confirm=True.
    #
    # force=True is deliberate: HERMES_DEFAULT_SKILLS is the deployment
    # admin's curated seed list (reviewed out-of-band), and the installer
    # *refuses* to install a skill whose scanner verdict is ``caution`` /
    # ``dangerous`` unless forced.  Org-shared skills like
    # ``ambulnz-github-app-auth`` carry a ``caution`` verdict, so without
    # force=True every fresh pod would boot missing them until an operator
    # manually re-ran the install.  The dedup gate (_already_installed)
    # ensures this only happens on genuine first install, not every boot.
    for call in fake_do_install.call_args_list:
        kwargs = call.kwargs
        assert kwargs["force"] is True, "admin seed list bypasses scan verdicts"
        assert kwargs["skip_confirm"] is True, "boot is non-interactive"


def test_main_passes_name_override(hermes_home, monkeypatch, fake_do_install):
    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "https://example.com/path/SKILL.md@custom-name",
    )
    mod = _load_module()
    assert mod.main() == 0

    fake_do_install.assert_called_once()
    kwargs = fake_do_install.call_args.kwargs
    assert kwargs["name_override"] == "custom-name"


def test_main_skips_already_installed_by_identifier(
    hermes_home, lock_path, monkeypatch, fake_do_install
):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "installed": {
                    "airflow-dag": {
                        "identifier": "AmbulnzLLC/hermes-shared-skills/skills/data-engineering/airflow-dag",
                        "install_path": "/opt/data/skills/data-engineering/airflow-dag",
                    }
                },
            }
        )
        + "\n"
    )

    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "AmbulnzLLC/hermes-shared-skills/skills/data-engineering/airflow-dag, "
        "AmbulnzLLC/hermes-shared-skills/skills/data-engineering/datalake-jobs",
    )
    mod = _load_module()
    assert mod.main() == 0

    # Only the second one should have been installed.
    assert fake_do_install.call_count == 1
    args, _ = fake_do_install.call_args[0], fake_do_install.call_args[1]
    assert "datalake-jobs" in args[0]


def test_main_dedup_handles_trailing_slash(
    hermes_home, lock_path, monkeypatch, fake_do_install
):
    """``foo/bar/skills/baz`` and ``foo/bar/skills/baz/`` are the same."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "installed": {
                    "baz": {
                        "identifier": "foo/bar/skills/baz/",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "foo/bar/skills/baz")
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_not_called()


def test_main_dedup_handles_source_prefix(
    hermes_home, lock_path, monkeypatch, fake_do_install
):
    """A bare env identifier matches a lockfile entry recorded with a source
    prefix prepended by ``do_install``.

    ``do_install`` routes a bare ``owner/repo/skills/<path>`` identifier
    through whichever source resolves it (a tap, the skills.sh index, ...) and
    records the *resolved* identifier — the env entry with a ``<source>/``
    prefix.  Without prefix-tolerant dedup the seeder reinstalls (and
    overwrites) the skill on every pod restart.  Regression test for that.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "installed": {
                    "ambulnz-github-app-auth": {
                        "identifier": (
                            "skills-sh/AmbulnzLLC/hermes-shared-skills"
                            "/skills/github/ambulnz-github-app-auth"
                        ),
                        "install_path": "ambulnz-github-app-auth",
                    }
                },
            }
        )
        + "\n"
    )
    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth",
    )
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_not_called()


def test_main_source_prefix_dedup_requires_boundary(
    hermes_home, lock_path, monkeypatch, fake_do_install
):
    """Suffix matching must respect path-segment boundaries: a recorded
    ``...xfoo/bar`` must NOT dedup an env entry ``foo/bar`` (no '/' boundary),
    otherwise distinct skills sharing a tail would collide."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "installed": {
                    "bar": {"identifier": "skills-sh/notfoo/bar/skills/baz"}
                }
            }
        )
    )
    # Env entry's tail ("foo/bar/skills/baz") is a substring of the recorded
    # id but not on a '/' boundary (recorded has "notfoo", not "/foo").
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "foo/bar/skills/baz")
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_called_once()


def test_main_continues_on_partial_failure(
    hermes_home, monkeypatch, fake_do_install, capsys
):
    """One bad skill must not block the rest from installing.

    A bad private-skill repo crash-looping the pod would take chat down;
    that's strictly worse than degraded operation with a warning.
    """
    # First call raises; second call succeeds.
    fake_do_install.side_effect = [
        RuntimeError("simulated network failure"),
        None,
    ]

    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "foo/bar/skills/broken, foo/bar/skills/working",
    )
    mod = _load_module()
    assert mod.main() == 0  # exit 0 even on partial failure

    assert fake_do_install.call_count == 2
    err = capsys.readouterr().err
    assert "simulated network failure" in err


def test_main_continues_on_systemexit(
    hermes_home, monkeypatch, fake_do_install
):
    """``do_install`` calls sys.exit(1) on some error paths — we catch it."""
    fake_do_install.side_effect = [SystemExit(1), None]

    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "foo/bar/skills/broken, foo/bar/skills/working",
    )
    mod = _load_module()
    assert mod.main() == 0
    assert fake_do_install.call_count == 2


def test_main_handles_missing_lockfile(hermes_home, monkeypatch, fake_do_install):
    """No lock.json yet (fresh container) → install everything."""
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "foo/bar/skills/a")
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_called_once()


def test_main_handles_malformed_lockfile(
    hermes_home, lock_path, monkeypatch, fake_do_install, capsys
):
    """Malformed lock.json → warn, install anyway (don't crash boot)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("{ not json")
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "foo/bar/skills/a")
    mod = _load_module()
    assert mod.main() == 0
    fake_do_install.assert_called_once()
    err = capsys.readouterr().err
    assert "could not read" in err


# ---------------------------------------------------------------------------
# Install hooks (scripts/install.sh)
# ---------------------------------------------------------------------------


def _make_skill_with_hook(skills_root: Path, name: str, body: str) -> Path:
    """Create <skills_root>/<name>/scripts/install.sh with the given body."""
    skill_dir = skills_root / name
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    hook = skill_dir / "scripts" / "install.sh"
    hook.write_text("#!/usr/bin/env bash\n" + body)
    return skill_dir


def test_run_install_hook_noop_when_absent(hermes_home):
    """A skill with no scripts/install.sh is a silent no-op."""
    mod = _load_module()
    skill_dir = hermes_home / "skills" / "no-hook"
    skill_dir.mkdir(parents=True)
    mod._run_install_hook(skill_dir)  # must not raise


def test_run_install_hook_runs_and_inherits_env(hermes_home, tmp_path, monkeypatch):
    """Present hook is executed via bash and inherits the env (e.g. so it can
    write to a $HOME-derived path)."""
    mod = _load_module()
    skills_root = hermes_home / "skills"
    marker = tmp_path / "hook-ran"
    skill_dir = _make_skill_with_hook(
        skills_root, "with-hook", 'echo ok; : > "$HOOK_MARKER"\n'
    )
    monkeypatch.setenv("HOOK_MARKER", str(marker))
    mod._run_install_hook(skill_dir)
    assert marker.exists(), "hook should have run and inherited HOOK_MARKER from env"


def test_run_install_hook_nonfatal_on_failure(hermes_home, capsys):
    """A hook that exits non-zero warns but does not raise."""
    mod = _load_module()
    skills_root = hermes_home / "skills"
    skill_dir = _make_skill_with_hook(skills_root, "bad-hook", "echo boom >&2; exit 3\n")
    mod._run_install_hook(skill_dir)  # must not raise
    err = capsys.readouterr().err
    assert "exited 3" in err
    assert "boom" in err


def test_skill_dir_for_uses_lock_install_path():
    """Resolution prefers the lockfile's recorded install_path, matched
    prefix-tolerantly against the source-prefixed identifier."""
    mod = _load_module()
    lock_data = {
        "installed": {
            "k": {
                "identifier": "skills-sh/AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth",
                "install_path": "ambulnz-github-app-auth",
            }
        }
    }
    root = Path("/skills")
    got = mod._skill_dir_for(
        "AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth",
        "",
        lock_data,
        root,
    )
    assert got == root / "ambulnz-github-app-auth"


def test_skill_dir_for_falls_back_to_name_override():
    mod = _load_module()
    root = Path("/skills")
    got = mod._skill_dir_for("https://example.com/x/SKILL.md", "my-skill", {}, root)
    assert got == root / "my-skill"


def test_skill_dir_for_falls_back_to_identifier_tail():
    mod = _load_module()
    root = Path("/skills")
    got = mod._skill_dir_for("foo/bar/skills/baz", "", {"installed": {}}, root)
    assert got == root / "baz"


def test_main_runs_hook_on_install(hermes_home, monkeypatch, fake_do_install):
    """After a fresh install, the skill's install.sh runs."""
    mod = _load_module()
    skills_root = hermes_home / "skills"
    marker = hermes_home / "installed-marker"
    # do_install is faked, so we materialize the skill dir + hook ourselves to
    # stand in for what the real installer would have written.
    _make_skill_with_hook(skills_root, "baz", f': > "{marker}"\n')
    monkeypatch.setenv("HERMES_DEFAULT_SKILLS", "foo/bar/skills/baz")
    assert mod.main() == 0
    fake_do_install.assert_called_once()
    assert marker.exists(), "install hook should run on the install path"


def test_main_runs_hook_on_skip(hermes_home, lock_path, monkeypatch, fake_do_install):
    """Even when the skill is already installed (skipped), the hook still runs
    — the PATH symlink can be missing on a fresh volume."""
    mod = _load_module()
    skills_root = hermes_home / "skills"
    marker = hermes_home / "skip-marker"
    _make_skill_with_hook(skills_root, "ambulnz-github-app-auth", f': > "{marker}"\n')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "installed": {
                    "k": {
                        "identifier": "skills-sh/AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth",
                        "install_path": "ambulnz-github-app-auth",
                    }
                }
            }
        )
    )
    monkeypatch.setenv(
        "HERMES_DEFAULT_SKILLS",
        "AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth",
    )
    assert mod.main() == 0
    fake_do_install.assert_not_called()  # was skipped (already installed via dedup)
    assert marker.exists(), "install hook should run even on the skip path"
