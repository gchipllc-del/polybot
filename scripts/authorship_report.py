#!/usr/bin/env python3
"""
Authorship attribution by Co-Authored-By model trailer.

Every AI-assisted commit in this repo stamps a trailer like
``Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>``. This tool
maps that trailer onto the code two ways:

  blame  (default) — runs ``git blame`` over every tracked source file and
                     tallies how many CURRENTLY-LIVE lines each model is
                     responsible for. This is the honest "what parts of the
                     code were modified by Fable 5" answer: it counts only
                     surviving lines, so work later overwritten by another
                     model doesn't get miscredited.
  commits          — lists the commits each model co-authored and the files
                     they touched (intent-level, not surviving-line-level).

Usage (from anywhere):
    python scripts/authorship_report.py                  # blame, all models, Fable 5 first
    python scripts/authorship_report.py --model "Fable 5"  # only Fable 5, per-file
    python scripts/authorship_report.py commits --model "Fable 5"
    python scripts/authorship_report.py --path lib        # scope to a subtree

Notes / honesty caveats:
  - Attribution is only as truthful as the trailers. A commit with no
    Co-Authored-By trailer (hand-written, or pre-convention) counts as
    "(unattributed)". The tool never guesses.
  - blame credits the LAST model to touch a line, not the original author.
    A line Fable 5 wrote and Opus 4.8 later edited counts as Opus 4.8.
  - Whitespace-only reflows still count; this is a footprint estimate,
    not a semantic-contribution measure.
"""
from __future__ import annotations

import argparse
import collections
import os
import subprocess
import sys

# Source extensions worth attributing; data/log/binary files are skipped.
CODE_EXTS = (".py", ".yaml", ".yml", ".sh", ".md", ".js", ".ts", ".toml", ".cfg")
SKIP_DIRS = ("data/", "logs/", "node_modules/", ".venv/", "venv/", "__pycache__/")


def _git(args: list[str], cwd: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"git {' '.join(args[:3])}… failed: {r.stderr[:200]}\n")
    return r.stdout


def _repo_root(start: str) -> str:
    out = _git(["rev-parse", "--show-toplevel"], start).strip()
    return out or start


def _normalize_model(trailer_value: str) -> str | None:
    """'Claude Fable 5 (1M context) <noreply@…>' -> 'Claude Fable 5'."""
    name = trailer_value.split("<")[0].strip()
    if not name.lower().startswith("claude"):
        return None
    # Collapse context-window variants so 'Opus 4.7' and 'Opus 4.7 (1M
    # context)' are one model.
    if "(" in name:
        name = name.split("(")[0].strip()
    return name


def build_commit_model_map(cwd: str) -> dict[str, str]:
    """sha40 -> model name (or '(unattributed)')."""
    fmt = "%H%x1f%(trailers:key=Co-authored-by,valueonly,separator=%x1e)"
    out = _git(["log", "--no-merges", f"--pretty={fmt}"], cwd)
    mapping: dict[str, str] = {}
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, trailers = line.split("\x1f", 1)
        model = "(unattributed)"
        for t in trailers.split("\x1e"):
            m = _normalize_model(t.strip())
            if m:
                model = m
                break
        mapping[sha.strip()[:40]] = model
    return mapping


def list_source_files(cwd: str, path: str) -> list[str]:
    out = _git(["ls-files", "--", path], cwd)
    files = []
    for f in out.splitlines():
        if not f.endswith(CODE_EXTS):
            continue
        if any(seg in f for seg in SKIP_DIRS):
            continue
        files.append(f)
    return files


def blame_counts(path: str, commit_model: dict[str, str], cwd: str) -> collections.Counter:
    """Per-model surviving-line counts for one file."""
    out = _git(["blame", "--line-porcelain", "HEAD", "--", path], cwd)
    counts: collections.Counter = collections.Counter()
    for line in out.splitlines():
        # Each line's porcelain block opens with: "<40-hex sha> <orig> <final> [n]"
        head = line.split(" ", 1)[0]
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            counts[commit_model.get(head, "(unattributed)")] += 1
    return counts


def cmd_blame(cwd: str, path: str, only_model: str | None) -> None:
    commit_model = build_commit_model_map(cwd)
    files = list_source_files(cwd, path)
    per_file: dict[str, collections.Counter] = {}
    totals: collections.Counter = collections.Counter()
    for f in files:
        c = blame_counts(f, commit_model, cwd)
        if c:
            per_file[f] = c
            totals.update(c)

    grand = sum(totals.values()) or 1
    print(f"\n=== SURVIVING-LINE ATTRIBUTION (git blame) — {len(files)} files, "
          f"{grand:,} lines under '{path}' ===\n")
    # Totals table
    print(f"{'model':32s} {'lines':>8s} {'share':>7s}")
    print("-" * 50)
    for model, n in totals.most_common():
        print(f"{model:32s} {n:>8,d} {100*n/grand:>6.1f}%")

    # Per-file, focused on the queried model (default Fable 5)
    focus = _resolve_model(only_model, totals)
    if focus:
        print(f"\n--- files containing surviving lines from '{focus}' "
              f"(sorted by count) ---\n")
        rows = []
        for f, c in per_file.items():
            if c.get(focus):
                rows.append((f, c[focus], sum(c.values())))
        rows.sort(key=lambda r: r[1], reverse=True)
        if not rows:
            print(f"  (no surviving lines attributed to '{focus}')")
        for f, n, tot in rows:
            print(f"  {n:>6,d} / {tot:<6,d} ({100*n/tot:>4.0f}%)  {f}")


def cmd_commits(cwd: str, path: str, only_model: str | None) -> None:
    commit_model = build_commit_model_map(cwd)
    focus = _resolve_model(only_model, collections.Counter(commit_model.values()))
    fmt = "%H%x1f%ad%x1f%s"
    out = _git(["log", "--no-merges", "--date=short", f"--pretty={fmt}", "--", path], cwd)
    print(f"\n=== COMMITS by model under '{path}'"
          + (f" (filter: {focus})" if focus else "") + " ===\n")
    shown = 0
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 3:
            continue
        sha, date, subj = parts[0], parts[1], parts[2]
        model = commit_model.get(sha[:40], "(unattributed)")
        if focus and model != focus:
            continue
        files = _git(["show", "--name-only", "--pretty=format:", sha], cwd).split()
        print(f"{date}  {sha[:9]}  [{model}]  {subj[:64]}")
        for f in files[:12]:
            print(f"             · {f}")
        shown += 1
    if shown == 0:
        print("  (no matching commits)")


def _resolve_model(query: str | None, available: collections.Counter) -> str | None:
    """Fuzzy-match a --model query ('Fable 5') to a full model name."""
    if query is None:
        # default focus: Fable 5 if present, else the top model
        for m in available:
            if "fable" in m.lower():
                return m
        return None
    q = query.lower()
    for m in available:
        if q in m.lower():
            return m
    sys.stderr.write(f"⚠ no model matching '{query}'; known: "
                     f"{', '.join(sorted(available))}\n")
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Attribute code to AI model by Co-Authored-By trailer")
    ap.add_argument("mode", nargs="?", choices=["blame", "commits"], default="blame")
    ap.add_argument("--model", default=None, help="focus one model, e.g. 'Fable 5'")
    ap.add_argument("--path", default=".", help="path/subtree to scope (default: whole repo)")
    args = ap.parse_args()

    cwd = _repo_root(os.getcwd())
    path = args.path
    if args.mode == "commits":
        cmd_commits(cwd, path, args.model)
    else:
        cmd_blame(cwd, path, args.model)


if __name__ == "__main__":
    main()
