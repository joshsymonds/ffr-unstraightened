#!/usr/bin/env -S just --justfile

set shell := ["bash", "-uc"]

default:
    @just --list

check: lint typecheck test

lint:
    ruff check .
    ruff format --check .

fmt:
    ruff format .
    ruff check --fix .

typecheck:
    basedpyright

test *args:
    pytest {{args}}

check-provenance:
    python3 -m ffrd.provenance

# Render every Mermaid block in tracked Markdown to SVG under build/docs/.
docs:
    #!/usr/bin/env bash
    set -u
    mapfile -d '' -t files < <(git ls-files -z -- '*.md')
    wait $! || exit 1
    status=0
    for f in "${files[@]}"; do
        dest="build/docs/$f"
        mkdir -p "$(dirname "$dest")"
        mmdc -i "$f" -o "$dest" || status=1
    done
    exit "$status"

# Build, probe in game, then zip the release for tag (default v1.0.0).
release tag="v1.0.0":
    ffrd build
    ffrd probe
    python3 -m ffrd.release {{tag}}
