"""Write the host Git revision into the persistent server data directory."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="Git repository directory")
    parser.add_argument("--output", default="server/data/deployed-version.json")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve(strict=True)
    output = Path(args.output)
    if not output.is_absolute():
        output = repo / output
    output.parent.mkdir(parents=True, exist_ok=True)

    sha = git(repo, "rev-parse", "HEAD")
    files = []
    for line in git(repo, "show", "--format=", "--numstat", "HEAD").splitlines()[:200]:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append({"path": path, "added": None if added == "-" else int(added), "deleted": None if deleted == "-" else int(deleted)})
    payload = {
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "current": {
            "sha": sha,
            "short": git(repo, "rev-parse", "--short", "HEAD"),
            "author": git(repo, "show", "-s", "--format=%an", "HEAD"),
            "committed_at": float(git(repo, "show", "-s", "--format=%ct", "HEAD")),
            "subject": git(repo, "show", "-s", "--format=%s", "HEAD"),
            "body": git(repo, "show", "-s", "--format=%B", "HEAD"),
            "files": files,
        },
        "generated_at": time.time(),
    }
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, output)
    print(f"Wrote deployed version {payload['current']['short']} to {output}")


if __name__ == "__main__":
    main()
