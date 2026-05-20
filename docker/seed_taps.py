"""Seed admin-managed skills-hub taps at container boot.

Reads ``HERMES_DEFAULT_TAPS`` from the environment and registers each tap in
``$HERMES_HOME/skills/.hub/taps.json`` so private organization skill repos
are available to ``hermes skills browse / search / inspect / install``
without the operator having to run ``hermes skills tap add`` manually after
every deploy.

Designed for the AmbulnzLLC pilot (and any deployment that ships private
skill repos via a GitHub App) where the org's shared-skills repo should
appear automatically on every pod.  Pairs with ``install_github_app_pem.py``
— the PEM provides outbound auth, this script registers the tap that uses
it.

Behavior:
- ``HERMES_DEFAULT_TAPS`` unset or empty → silent no-op.  Generic
  (non-pilot) deploys aren't burdened.
- ``HERMES_DEFAULT_TAPS`` set → each entry is normalized and added to
  ``taps.json`` if not already present.  Idempotent — re-running on every
  boot is safe.
- Entry format: comma- or newline-separated.  Each entry is one of:
    * ``owner/repo``                 → expanded to ``https://github.com/owner/repo``
    * ``owner/repo@<path>``          → custom skills path (default ``skills/``)
    * ``https://github.com/owner/repo`` → used as-is
    * ``https://...@<path>``         → URL with custom path
  Trailing slashes on repo URLs are stripped before comparison so
  ``foo/bar`` and ``foo/bar/`` are the same tap.
- Crashes the boot (non-zero exit) when ``HERMES_DEFAULT_TAPS`` is set but
  ``taps.json`` exists and is malformed JSON.  Loud > silent: clobbering a
  user-edited taps file would be worse than crash-looping the pod.
- Preserves any taps the user added manually — only appends, never deletes.
  An admin who wants to *remove* a tap must do it via ``hermes skills tap
  remove`` or by editing ``taps.json`` directly; this script never deletes.

This is a SEED, not a lock.  The hermes runtime user can subsequently
remove these taps via ``hermes skills tap remove``; they will be re-seeded
on the next container boot.

Must run after the gosu drop (as the hermes user) so the resulting
``taps.json`` has the right ownership without an extra chown.  The file
itself ends up at ``$HERMES_HOME/skills/.hub/taps.json`` which is inside
the volume the hermes user already owns.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List


def _log(msg: str) -> None:
    print(f"[seed_taps] {msg}", flush=True)


def _err(msg: str) -> None:
    print(f"[seed_taps] ERROR: {msg}", file=sys.stderr, flush=True)


def _parse_entries(raw: str) -> List[dict]:
    """Parse the ``HERMES_DEFAULT_TAPS`` value into normalized tap dicts.

    Entries are separated by commas or newlines and may carry an optional
    ``@<path>`` suffix.  Whitespace is stripped from every component.
    """
    entries: List[dict] = []
    for chunk in re.split(r"[,\n]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue

        # Split off optional "@<path>" suffix.  Bare "owner/repo" has no @
        # at all; full URLs have an @ only when carrying a custom path
        # (we use "://github.com/" or absence of "://" to disambiguate
        # from "user:token@host" auth URLs, which we explicitly do NOT
        # accept here — taps are stored unauthenticated and the App PEM
        # provides credentials at fetch time).
        if "://" in chunk:
            # Full URL form.  An @ here means "<url>@<path>".
            scheme, rest = chunk.split("://", 1)
            if "@" in rest:
                # Reject any URL with embedded credentials — that's a
                # config smell and a security smell (creds end up in
                # taps.json on the volume).
                host_and_more, _, path_part = rest.partition("@")
                if "/" not in host_and_more:
                    _err(
                        f"refusing entry {chunk!r}: looks like an "
                        "authenticated URL (user:token@host).  Store "
                        "credentials via GITHUB_APP_PEM_SECRET_ID instead."
                    )
                    continue
                repo = f"{scheme}://{host_and_more}"
                path = path_part.strip() or "skills/"
            else:
                repo = chunk
                path = "skills/"
        else:
            # Bare "owner/repo[@path]" form.
            if "@" in chunk:
                repo_part, _, path_part = chunk.partition("@")
                repo_part = repo_part.strip()
                path = path_part.strip() or "skills/"
            else:
                repo_part = chunk
                path = "skills/"

            if "/" not in repo_part:
                _err(
                    f"skipping entry {chunk!r}: not a valid owner/repo "
                    "pair and not a full URL"
                )
                continue
            repo = f"https://github.com/{repo_part}"

        repo = repo.rstrip("/")
        entries.append({"repo": repo, "path": path})
    return entries


def main() -> int:
    raw = os.environ.get("HERMES_DEFAULT_TAPS", "").strip()
    if not raw:
        # Nothing to seed — silent no-op.  Matches seed_admin_config.py
        # contract for env-driven optional provisioning.
        return 0

    home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    taps_path = home / "skills" / ".hub" / "taps.json"

    parsed = _parse_entries(raw)
    if not parsed:
        _log("HERMES_DEFAULT_TAPS contained no usable entries; nothing to do")
        return 0

    # Load existing taps.  Treat a missing file as "{taps: []}".  Treat a
    # malformed file as fatal — silently clobbering a user-edited file is
    # worse than crash-looping the pod.
    existing: dict = {"taps": []}
    if taps_path.exists():
        try:
            text = taps_path.read_text(encoding="utf-8")
            if text.strip():
                loaded = json.loads(text)
                if not isinstance(loaded, dict) or not isinstance(
                    loaded.get("taps"), list
                ):
                    _err(
                        f"{taps_path} did not parse as a JSON object with a "
                        "'taps' list; refusing to clobber"
                    )
                    return 1
                existing = loaded
        except json.JSONDecodeError as exc:
            _err(
                f"{taps_path} is not valid JSON ({exc}); refusing to "
                "clobber a user-edited taps file"
            )
            return 1
        except OSError as exc:
            _err(f"cannot read {taps_path}: {exc}")
            return 2

    # Build a set of existing (repo, path) pairs for dedup.  Compare by
    # rstripped repo so trailing-slash variants don't double-add.
    have = {
        (str(t.get("repo", "")).rstrip("/"), str(t.get("path", "skills/")))
        for t in existing.get("taps", [])
        if isinstance(t, dict)
    }

    added = 0
    for tap in parsed:
        key = (tap["repo"], tap["path"])
        if key in have:
            continue
        existing.setdefault("taps", []).append(tap)
        have.add(key)
        added += 1
        _log(f"added {tap['repo']} (path={tap['path']})")

    if added == 0:
        _log("all configured taps already present; nothing to do")
        return 0

    # Atomic write: tmp + rename so a crash mid-write leaves the previous
    # taps.json intact.
    taps_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = taps_path.with_suffix(taps_path.suffix + ".tmp")
    try:
        tmp_path.write_text(
            json.dumps(existing, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, taps_path)
    except OSError as exc:
        _err(f"failed to write {taps_path}: {exc}")
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return 3

    _log(f"seeded {added} tap(s) into {taps_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
