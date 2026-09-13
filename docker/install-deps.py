#!/usr/bin/env python3
"""Install this project's *dependencies* (not the package) for a set of extras.

Reads pyproject.toml so the extras there stay the single source of truth for
what each image gets, rather than duplicating package lists in the Dockerfile
where they would drift.

The package itself is deliberately never installed: the image copies the source
to /app and sets PYTHONPATH=/app, because server.py resolves the static UI as
``Path(__file__).resolve().parent.parent / "web"``. A site-packages copy would
resolve that to the wrong directory and the is_dir() guard would fail silently,
serving an empty page. Installing only dependencies also keeps the layers
smaller and avoids a duplicate copy of the source.

Usage: install-deps.py <extra> [<extra> ...]
"""
import pathlib
import subprocess
import sys
import tomllib

HERE = pathlib.Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    requested = argv[1:]
    if not requested:
        print("usage: install-deps.py <extra> [<extra> ...]", file=sys.stderr)
        return 2

    pyproject = HERE / "pyproject.toml"
    if not pyproject.is_file():
        print(f"cannot find {pyproject}", file=sys.stderr)
        return 2

    data = tomllib.loads(pyproject.read_text())
    project = data["project"]
    optional = project.get("optional-dependencies", {})

    packages = list(project["dependencies"])
    for name in requested:
        if name not in optional:
            print(
                f"unknown extra {name!r}; available: "
                f"{', '.join(sorted(optional))}",
                file=sys.stderr,
            )
            return 2
        packages += optional[name]

    print(f"installing extras {requested} -> {len(packages)} requirement(s)")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", *packages]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
