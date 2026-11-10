#!/usr/bin/env python3
"""Rebuild the hackathon submission ZIP from the repo working tree.

Deterministic: fixed timestamps, sorted entries, ZIP_DEFLATED.
Layout: <prefix>/<repo-relative-path>, excluding node_modules, .git, __pycache__.
"""
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "echoes"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / "echoes-submit-final.zip")

EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".echoes", "site/tmp", "target"}
EXCLUDE_FILES = {".DS_Store"}
# The one-off page-editing helper is repo-only, it is not part of the shipped product.
EXCLUDE_PATHS = {"scripts/faq-add.py"}

files = []
for p in sorted(ROOT.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(ROOT).as_posix()
    if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts[:-1]):
        continue
    if p.name in EXCLUDE_FILES or rel in EXCLUDE_PATHS:
        continue
    files.append((rel, p))

FIXED_DATE = (2026, 11, 10, 0, 0, 0)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for rel, p in files:
        zi = zipfile.ZipInfo(f"{PREFIX}/{rel}", date_time=FIXED_DATE)
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = (0o644 & 0xFFFF) << 16
        z.writestr(zi, p.read_bytes())

data = OUT.read_bytes()
print(f"{OUT}")
print(f"entries: {len(files)}")
print(f"bytes:   {len(data)}")
print(f"sha256:  {hashlib.sha256(data).hexdigest()}")
big = sorted(((p.stat().st_size, rel) for rel, p in files), reverse=True)[:4]
for s, r in big:
    print(f"  largest: {r} {s:,}")
