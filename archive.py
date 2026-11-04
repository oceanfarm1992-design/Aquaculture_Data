#!/usr/bin/env python3
"""
archive.py — decide which tables a run must archive, so unchanged data is never stored twice.

    python archive.py --data-dir data --previous previous_manifest.json \\
        --tag data-2026-11-09-run7 --out-manifest manifest.json --out-changed changed.txt

Every release carries a manifest.json listing every table, its content fingerprint, its
row count and the release that holds that version. A table whose fingerprint matches the
previous manifest is not stored again: the new manifest points to the older release.
Only changed (or new) tables are written to changed.txt, and the release holds just those.
If nothing changed, changed.txt is empty and no release is needed.

Tables missing from this run (e.g. a source that failed) keep their previous entry.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from fingerprint import table_fingerprint


def load_manifest(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {"tables": {}}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        print(f"warning: {path} is not valid JSON; treating every table as new", file=sys.stderr)
        return {"tables": {}}
    return data if isinstance(data.get("tables"), dict) else {"tables": {}}


def plan(data_dir: Path, previous: dict, tag: str) -> tuple[dict, list[str]]:
    tables: dict[str, dict] = {}
    changed: list[str] = []
    for path in sorted(data_dir.glob("*.json")):
        name = path.stem
        rows = json.loads(path.read_text(encoding="utf-8"))
        digest, count = table_fingerprint(rows)
        old = previous["tables"].get(name)
        if old and old.get("fingerprint") == digest:
            tables[name] = {**old, "last_checked": tag}
        else:
            tables[name] = {"fingerprint": digest, "rows": count, "release": tag, "last_checked": tag}
            changed.append(name)
    for name, old in previous["tables"].items():  # not scraped this run: keep the old version
        tables.setdefault(name, old)
    manifest = {
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": tag,
        "tables": dict(sorted(tables.items())),
    }
    return manifest, changed


def release_notes(manifest: dict, changed: list[str], run_url: str | None) -> str:
    lines = ["Tables that changed since the previous archive (stored in this release):", ""]
    lines += [f"- `{t}`: {manifest['tables'][t]['rows']:,} rows" for t in changed]
    unchanged = [t for t in manifest["tables"] if t not in changed]
    if unchanged:
        lines += ["", "Unchanged tables are not stored again. Latest version of each:", ""]
        lines += [f"- `{t}`: in release `{manifest['tables'][t]['release']}`" for t in unchanged]
    lines += ["", "`manifest.json` lists every table, its fingerprint and the release that holds it."]
    if run_url:
        lines += ["", f"Run log: {run_url}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Plan an archive that stores only changed tables.")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--previous", help="manifest.json from the latest earlier release (optional)")
    p.add_argument("--tag", required=True, help="tag of the release being made")
    p.add_argument("--out-manifest", default="manifest.json")
    p.add_argument("--out-changed", default="changed.txt")
    p.add_argument("--out-notes", default="notes.md")
    p.add_argument("--run-url")
    args = p.parse_args(argv)

    manifest, changed = plan(Path(args.data_dir), load_manifest(args.previous), args.tag)
    Path(args.out_manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    Path(args.out_changed).write_text("".join(f"{t}\n" for t in changed), encoding="utf-8")
    Path(args.out_notes).write_text(release_notes(manifest, changed, args.run_url), encoding="utf-8")
    print(f"{len(changed)} changed: {', '.join(changed) or 'none'}")
    unchanged = [t for t in manifest["tables"] if t not in changed]
    print(f"{len(unchanged)} unchanged (not stored again): {', '.join(unchanged) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
