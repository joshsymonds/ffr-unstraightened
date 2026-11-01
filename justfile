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
