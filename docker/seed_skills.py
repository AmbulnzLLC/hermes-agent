"""Seed admin-managed skills installs at container boot.

Reads ``HERMES_DEFAULT_SKILLS`` from the environment and installs each
listed skill into ``$HERMES_HOME/skills/`` via the same code path used by
``hermes skills install`` — quarantine, scan, install, lockfile entry.

Designed for the AmbulnzLLC pilot (and any deployment that ships private
skill repos via a GitHub App) where a fixed set of org-shared skills
should be present on every pod immediately after boot, instead of the
operator having to run ``hermes skills install`` for each one.

Pairs with:
- ``install_github_app_pem.py`` — provides outbound GitHub auth so private
  repos resolve.
- ``seed_taps.py`` — registers the tap that ``hermes skills install``
  consults when resolving a tap-relative identifier.

Behavior:
- ``HERMES_DEFAULT_SKILLS`` unset or empty → silent no-op (generic
  deploys).
- ``HERMES_DEFAULT_SKILLS`` set → each entry is installed via
  ``hermes_cli.skills_hub.do_install`` with ``skip_confirm=True``.
- Entry format: comma- or newline-separated.  Each entry is one of:
    * ``owner/repo/skills/<path>``    → standard hub identifier
    * ``owner/repo/<path>``           → identifier sans `skills/` prefix
                                       (the source router accepts either)
    * ``https://...SKILL.md``         → direct URL to a SKILL.md file
    * ``owner/repo/skills/<path>@<name>``
                                      → with explicit `--name` override,
                                        used when the SKILL.md frontmatter
                                        lacks a ``name:`` field
- Idempotent: skills already recorded in ``lock.json`` (matched by
  ``identifier``) are skipped.  Re-running on every boot is safe and
  fast.
- Non-fatal: a single skill failing to install (network blip, scan
  blocked, malformed SKILL.md) emits a warning and continues with the
  next.  Boot continues.  Rationale: a private-skill outage shouldn't
  crash-loop the pod and take chat down with it; the operator can
  investigate at leisure.  Compare ``install_github_app_pem.py``, which
  *does* crash on failure — credentials are a hard prerequisite, but
  individual skill installs are a soft one.
- Uses ``--force`` so admin-managed default skills install even when the
  scanner returns a ``caution`` or ``dangerous`` verdict.  Rationale: this
  seed list is curated by the deployment admin (not a community drop), so
  individual scan findings have already been reviewed out-of-band; the
  alternative is every fresh pod missing org-shared skills until an operator
  manually re-runs the install with ``--force``, which defeats the seeder's
  purpose.  If you want scan verdicts respected on a per-skill basis, drop
  the entry from ``HERMES_DEFAULT_SKILLS`` and install it interactively.

This is a SEED, not a lock.  After boot the hermes runtime user can
``hermes skills uninstall <name>`` and the skill stays gone for the
lifetime of the container — but on the next container boot it will be
re-installed.  The admin desired-state wins across reboots; in-session
removal still works for ad-hoc debugging.

Must run after the gosu drop (as the hermes user) so files end up with
the right ownership without an extra chown, and after ``seed_taps.py``
so any tap-relative identifiers resolve.

Uses the venv's python, so ``hermes_cli.skills_hub`` and its transitive
deps (rich, pydantic, etc.) are importable.
"""
from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path
from typing import List, Tuple


def _log(msg: str) -> None:
    print(f"[seed_skills] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[seed_skills] ERROR: {msg}", file=sys.stderr, flush=True)


def _parse_entries(raw: str) -> List[Tuple[str, str]]:
    """Parse the env var into a list of ``(identifier, name_override)``.

    Entries are comma- or newline-separated.  An optional ``@<name>``
    suffix supplies an explicit ``--name`` for the install (used when a
    URL-sourced skill's SKILL.md has no ``name:`` frontmatter).

    Disambiguation: an ``@`` is only treated as a name-override delimiter
    when it appears *after* a path component, never inside a URL's
    ``user:token@host`` segment.  We reject embedded credentials outright
    — that's a config smell and a security smell (credentials end up in
    pod logs and process tables).
    """
    entries: List[Tuple[str, str]] = []
    for chunk in re.split(r"[,\n]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue

        identifier = chunk
        name_override = ""

        if "://" in chunk:
            scheme, rest = chunk.split("://", 1)
            # Look for "@<name>" suffix — but only if there's a path
            # component after the host (i.e. at least one '/' before the
            # '@').  Otherwise the '@' is part of an authenticated URL,
            # which we reject.
            at_idx = rest.rfind("@")
            if at_idx != -1:
                before_at = rest[:at_idx]
                after_at = rest[at_idx + 1:]
                if "/" in before_at and "/" not in after_at and after_at:
                    identifier = f"{scheme}://{before_at}"
                    name_override = after_at.strip()
                elif "/" not in before_at:
                    # user:token@host form
                    _err(
                        f"refusing entry {chunk!r}: looks like an "
                        "authenticated URL (user:token@host).  Store "
                        "credentials via GITHUB_APP_PEM_SECRET_ID instead."
                    )
                    continue
                # else: '@' is in the path or the suffix has slashes —
                # treat the whole thing as the identifier.
        else:
            # Bare identifier form.  '@' splits identifier from
            # name override.
            if "@" in chunk:
                identifier, _, name_part = chunk.partition("@")
                identifier = identifier.strip()
                name_override = name_part.strip()
            if "/" not in identifier:
                _err(
                    f"skipping entry {chunk!r}: not a valid skill "
                    "identifier (expected owner/repo/skills/<path> or "
                    "an https:// URL)"
                )
                continue

        entries.append((identifier, name_override))
    return entries


def _already_installed(identifier: str, lock_data: dict) -> bool:
    """Return True if any lock-file entry was sourced from this identifier.

    The hub records the resolved identifier alongside each install, so we
    can dedup before re-running ``do_install``.

    Matching is **source-prefix tolerant**.  ``do_install`` routes a bare
    ``owner/repo/skills/<path>`` identifier through whichever source resolves
    it (a registered tap, the ``skills.sh`` index, etc.) and records the
    *resolved* identifier in ``lock.json`` — which is the original env entry
    with a source prefix prepended, e.g.::

        env entry  : AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth
        recorded   : skills-sh/AmbulnzLLC/hermes-shared-skills/skills/github/ambulnz-github-app-auth

    An exact ``==`` check never matches that pair, so the old dedup re-ran
    ``do_install`` on **every** boot, clobbering the on-disk skill (and any
    runtime state under it) each pod restart.  We therefore treat a recorded
    identifier as a match when it equals the env identifier *or* ends with it
    on a path-segment boundary (the prepended source prefix case).  Trailing
    slashes are normalized on both sides.
    """
    needle = identifier.rstrip("/")
    if not needle:
        return False
    for entry in (lock_data.get("installed") or {}).values():
        recorded = str(entry.get("identifier", "")).rstrip("/")
        if not recorded:
            continue
        if recorded == needle:
            return True
        # Source-prefix tolerance: recorded is "<source>/<needle>".  Require a
        # '/' boundary so "foo/bar" doesn't spuriously match "xfoo/bar".
        if recorded.endswith("/" + needle):
            return True
    return False


def _install_one(identifier: str, name_override: str) -> bool:
    """Invoke ``do_install`` for a single entry.  Returns True on success."""
    # Imported lazily so a missing venv fails loudly with a clear traceback
    # instead of at module-load time on every boot.
    from hermes_cli.skills_hub import do_install

    try:
        do_install(
            identifier,
            category="",
            force=True,             # admin-curated seed list — bypass scan
                                    # verdicts so org-shared skills install on
                                    # every fresh pod without operator action.
            skip_confirm=True,      # non-interactive boot
            invalidate_cache=False, # avoid thrashing the index cache per skill
            name_override=name_override,
        )
        return True
    except SystemExit as exc:
        # do_install calls sys.exit(1) on some failure paths.  Treat as
        # soft failure so one bad skill doesn't prevent the rest from
        # installing.
        _err(f"install of {identifier!r} exited with code {exc.code}")
        return False
    except Exception as exc:  # noqa: BLE001 — top-level boot guard
        _err(f"install of {identifier!r} raised: {exc}")
        traceback.print_exc()
        return False


def _skill_dir_for(identifier: str, name_override: str, lock_data: dict, skills_root: Path) -> Path:
    """Best-effort resolve the on-disk directory a skill installed into.

    Prefers the lockfile's recorded ``install_path`` (the bare skill name the
    hub wrote, e.g. ``ambulnz-github-app-auth``) — matched prefix-tolerantly
    against ``identifier`` the same way ``_already_installed`` matches.  Falls
    back to ``name_override`` if given, else the trailing path segment of the
    identifier.  All of these resolve under ``<skills_root>/<name>``.
    """
    needle = identifier.rstrip("/")
    for entry in (lock_data.get("installed") or {}).values():
        recorded = str(entry.get("identifier", "")).rstrip("/")
        if not recorded:
            continue
        if recorded == needle or recorded.endswith("/" + needle):
            install_path = str(entry.get("install_path", "")).strip()
            if install_path:
                return skills_root / install_path
            break
    if name_override:
        return skills_root / name_override
    # Trailing path segment of the identifier (strip any URL/query cruft).
    tail = needle.rstrip("/").rsplit("/", 1)[-1]
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    return skills_root / tail


def _run_install_hook(skill_dir: Path) -> None:
    """Run a skill's ``scripts/install.sh`` if it ships one.

    Some skills need a post-install step that the file copy alone can't do —
    e.g. ``ambulnz-github-app-auth`` symlinks its ``ambulnz-gh`` wrapper onto
    PATH (``~/.local/bin``).  ``seed_skills`` copies the *files* every boot,
    but the PATH symlink lives outside the skill dir, so on a fresh volume the
    wrapper would be missing until someone ran the hook by hand.  Running the
    hook here makes it self-heal on every boot.

    Contract, mirroring the rest of the seeder:
      * Best-effort and **non-fatal** — a failing hook warns but never crashes
        the boot (the skill's own ``install.sh`` is expected to be idempotent).
      * Invoked via ``bash`` (skill files are re-cert'd to ``-rw-r--r--`` on
        deploy — no execute bit — so direct exec would fail).
      * Inherits the current environment so ``$HOME`` is correct and the
        wrapper lands in the login shell's ``$HOME/.local/bin``.
    """
    hook = skill_dir / "scripts" / "install.sh"
    if not hook.is_file():
        return
    import subprocess

    _log(f"running install hook: {hook}")
    try:
        proc = subprocess.run(
            ["bash", str(hook)],
            cwd=str(skill_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _err(f"install hook {hook} could not run: {exc}")
        return
    for line in (proc.stdout or "").splitlines():
        _log(f"  hook: {line}")
    if proc.returncode != 0:
        _err(f"install hook {hook} exited {proc.returncode}")
        for line in (proc.stderr or "").splitlines():
            _err(f"  hook: {line}")


def main() -> int:
    raw = os.environ.get("HERMES_DEFAULT_SKILLS", "").strip()
    if not raw:
        return 0

    parsed = _parse_entries(raw)
    if not parsed:
        _log("HERMES_DEFAULT_SKILLS contained no usable entries; nothing to do")
        return 0

    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    lock_path = home / "skills" / ".hub" / "lock.json"

    # Read the lockfile directly here for the dedup check — we don't want
    # to import ``tools.skills_hub`` purely to read JSON, because that
    # module has heavier transitive imports we don't need.
    lock_data: dict = {"installed": {}}
    if lock_path.exists():
        try:
            import json
            text = lock_path.read_text(encoding="utf-8")
            if text.strip():
                lock_data = json.loads(text)
        except (OSError, ValueError) as exc:
            # Don't crash on a malformed lockfile — installs will fail
            # downstream and emit clearer errors.  But do warn.
            _err(f"could not read {lock_path}: {exc}")

    installed_count = 0
    skipped_count = 0
    failed_count = 0
    skills_root = home / "skills"

    for identifier, name_override in parsed:
        if _already_installed(identifier, lock_data):
            skipped_count += 1
            _log(f"already installed: {identifier}")
            # Still run the install hook: file-copy idempotence doesn't cover
            # side effects outside the skill dir (e.g. the ~/.local/bin
            # wrapper symlink), which can be absent on a fresh volume even
            # when the skill files are present.  The hook is idempotent.
            _run_install_hook(
                _skill_dir_for(identifier, name_override, lock_data, skills_root)
            )
            continue

        _log(f"installing {identifier}" + (f" (name={name_override})" if name_override else ""))
        if _install_one(identifier, name_override):
            installed_count += 1
            _run_install_hook(
                _skill_dir_for(identifier, name_override, lock_data, skills_root)
            )
        else:
            failed_count += 1

    summary_parts = []
    if installed_count:
        summary_parts.append(f"installed={installed_count}")
    if skipped_count:
        summary_parts.append(f"skipped={skipped_count}")
    if failed_count:
        summary_parts.append(f"failed={failed_count}")
    if summary_parts:
        _log("done: " + ", ".join(summary_parts))

    # Exit 0 even on partial failure: a bad private-skill repo shouldn't
    # crash-loop the pod.  The boot logs make the failures visible.
    return 0


if __name__ == "__main__":
    sys.exit(main())
