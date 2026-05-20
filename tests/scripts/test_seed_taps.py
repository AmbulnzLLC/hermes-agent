"""Tests for ``docker/seed_taps.py``.

The seed script runs at container boot to register admin-managed
skills-hub taps from ``HERMES_DEFAULT_TAPS``.  Its contract:

- Silent no-op when ``HERMES_DEFAULT_TAPS`` is unset/empty (generic
  deploys).
- Idempotent — re-running on the same env doesn't duplicate entries and
  doesn't rewrite an already-correct file.
- Accepts both ``owner/repo[@path]`` shorthand and full ``https://`` URLs;
  comma- and newline-separated.
- Preserves taps the user added manually — only appends, never deletes.
- Refuses to clobber a malformed ``taps.json`` (non-zero exit), so a
  user-edited file with a typo crash-loops rather than silently losing
  the user's customizations.
- Refuses URLs with embedded credentials (``user:token@host``) — those
  belong in the GitHub App PEM, not in taps.json on the data volume.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "docker" / "seed_taps.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_seed_taps", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def taps_path(hermes_home):
    return hermes_home / "skills" / ".hub" / "taps.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_bare_owner_repo():
    mod = _load_module()
    assert mod._parse_entries("foo/bar") == [
        {"repo": "https://github.com/foo/bar", "path": "skills/"}
    ]


def test_parse_owner_repo_with_path():
    mod = _load_module()
    assert mod._parse_entries("foo/bar@custom/path/") == [
        {"repo": "https://github.com/foo/bar", "path": "custom/path/"}
    ]


def test_parse_full_url():
    mod = _load_module()
    assert mod._parse_entries("https://github.com/foo/bar/") == [
        {"repo": "https://github.com/foo/bar", "path": "skills/"}
    ]


def test_parse_full_url_with_path():
    mod = _load_module()
    assert mod._parse_entries(
        "https://github.com/foo/bar@things/"
    ) == [{"repo": "https://github.com/foo/bar", "path": "things/"}]


def test_parse_comma_and_newline_separated():
    mod = _load_module()
    raw = "  a/b ,\n  c/d@x/  \n\n https://github.com/e/f \n"
    assert mod._parse_entries(raw) == [
        {"repo": "https://github.com/a/b", "path": "skills/"},
        {"repo": "https://github.com/c/d", "path": "x/"},
        {"repo": "https://github.com/e/f", "path": "skills/"},
    ]


def test_parse_skips_invalid_bare_entry(capsys):
    mod = _load_module()
    # "no-slash" isn't a valid owner/repo pair and isn't a URL.
    result = mod._parse_entries("no-slash, foo/bar")
    assert result == [
        {"repo": "https://github.com/foo/bar", "path": "skills/"}
    ]
    err = capsys.readouterr().err
    assert "no-slash" in err


def test_parse_rejects_authenticated_url(capsys):
    mod = _load_module()
    # user:token@host form must be rejected — credentials belong in the
    # App PEM, not on the data volume.
    result = mod._parse_entries(
        "https://x-access-token:abc123@github.com/foo/bar"
    )
    assert result == []
    err = capsys.readouterr().err
    assert "authenticated URL" in err


# ---------------------------------------------------------------------------
# main() — env-driven flow
# ---------------------------------------------------------------------------


def test_main_noop_when_env_unset(hermes_home, taps_path, monkeypatch):
    monkeypatch.delenv("HERMES_DEFAULT_TAPS", raising=False)
    mod = _load_module()
    assert mod.main() == 0
    assert not taps_path.exists()


def test_main_noop_when_env_empty(hermes_home, taps_path, monkeypatch):
    monkeypatch.setenv("HERMES_DEFAULT_TAPS", "   \n  ")
    mod = _load_module()
    assert mod.main() == 0
    assert not taps_path.exists()


def test_main_seeds_into_missing_taps_file(hermes_home, taps_path, monkeypatch):
    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "AmbulnzLLC/hermes-shared-skills",
    )
    mod = _load_module()
    assert mod.main() == 0

    data = _read(taps_path)
    assert data == {
        "taps": [
            {
                "repo": "https://github.com/AmbulnzLLC/hermes-shared-skills",
                "path": "skills/",
            }
        ]
    }


def test_main_idempotent_on_second_run(hermes_home, taps_path, monkeypatch):
    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "AmbulnzLLC/hermes-shared-skills",
    )
    mod = _load_module()
    assert mod.main() == 0
    first = taps_path.read_text()

    # Reload module to be safe and rerun.
    mod = _load_module()
    assert mod.main() == 0
    second = taps_path.read_text()

    assert first == second
    assert len(_read(taps_path)["taps"]) == 1


def test_main_preserves_user_added_taps(hermes_home, taps_path, monkeypatch):
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(
        json.dumps(
            {
                "taps": [
                    {
                        "repo": "https://github.com/user/manual",
                        "path": "skills/",
                    }
                ]
            }
        )
        + "\n"
    )

    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "AmbulnzLLC/hermes-shared-skills",
    )
    mod = _load_module()
    assert mod.main() == 0

    repos = [t["repo"] for t in _read(taps_path)["taps"]]
    assert repos == [
        "https://github.com/user/manual",
        "https://github.com/AmbulnzLLC/hermes-shared-skills",
    ]


def test_main_dedups_against_existing_entry(hermes_home, taps_path, monkeypatch):
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(
        json.dumps(
            {
                "taps": [
                    {
                        "repo": "https://github.com/AmbulnzLLC/hermes-shared-skills",
                        "path": "skills/",
                    }
                ]
            }
        )
        + "\n"
    )
    mtime_before = taps_path.stat().st_mtime_ns

    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        # bare form should match the URL form already present
        "AmbulnzLLC/hermes-shared-skills",
    )
    mod = _load_module()
    assert mod.main() == 0
    # No-op write — file should not have been rewritten.
    assert taps_path.stat().st_mtime_ns == mtime_before


def test_main_dedups_against_trailing_slash(hermes_home, taps_path, monkeypatch):
    """``foo/bar`` in env and ``foo/bar/`` in taps.json are the same tap."""
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(
        json.dumps(
            {
                "taps": [
                    {
                        "repo": "https://github.com/AmbulnzLLC/hermes-shared-skills/",
                        "path": "skills/",
                    }
                ]
            }
        )
        + "\n"
    )

    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "AmbulnzLLC/hermes-shared-skills",
    )
    mod = _load_module()
    assert mod.main() == 0
    assert len(_read(taps_path)["taps"]) == 1


def test_main_crashes_on_malformed_taps_file(hermes_home, taps_path, monkeypatch, capsys):
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text("{ not json")

    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "foo/bar",
    )
    mod = _load_module()
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "not valid JSON" in err
    # Original (corrupt) content must be left intact for the operator to fix.
    assert taps_path.read_text() == "{ not json"


def test_main_crashes_on_wrong_shape_taps_file(hermes_home, taps_path, monkeypatch, capsys):
    """JSON-parsable but not a {"taps": [...]} object → refuse to clobber."""
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    taps_path.write_text(json.dumps(["not", "a", "dict"]))

    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "foo/bar",
    )
    mod = _load_module()
    assert mod.main() == 1
    err = capsys.readouterr().err
    assert "did not parse" in err


def test_main_handles_multiple_entries(hermes_home, taps_path, monkeypatch):
    monkeypatch.setenv(
        "HERMES_DEFAULT_TAPS",
        "foo/bar, baz/qux@custom/, https://github.com/e/f",
    )
    mod = _load_module()
    assert mod.main() == 0

    taps = _read(taps_path)["taps"]
    assert taps == [
        {"repo": "https://github.com/foo/bar", "path": "skills/"},
        {"repo": "https://github.com/baz/qux", "path": "custom/"},
        {"repo": "https://github.com/e/f", "path": "skills/"},
    ]


def test_main_atomic_write_no_tmp_left_behind(hermes_home, taps_path, monkeypatch):
    monkeypatch.setenv("HERMES_DEFAULT_TAPS", "foo/bar")
    mod = _load_module()
    assert mod.main() == 0

    leftovers = list(taps_path.parent.glob("*.tmp"))
    assert leftovers == []
