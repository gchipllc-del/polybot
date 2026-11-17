# Notes for Claude sessions in this repo

## Push each finished change
When a modification is finished - the full suite passes (`python -m pytest tests/ -q`) and
so does the `selftest` of every script it touched - commit it and push it to the working
branch right away. One logical change per commit. Don't batch finished work, and never
push a failing suite. The user pulls on their Windows machine (`git pull`) and runs from
there.

## Standing rules
- `.env` and `*.pem` are git-ignored and never committed. Key material never goes into
  chat, commits, logs or tests.
- Kalshi access here is read-only; there is no live order path. Live money stays OFF until
  a forward paper rule earns EDGE: 100+ events after registration, surviving the
  false-discovery correction across all forward rules.
- Forward rules are pre-registered (`data/sleeve_rules.jsonl`, append-only): a rule is
  never edited after it is named, only retired. History can nominate a method; only the
  forward record can call it proven.
- The dev sandbox cannot reach Kalshi or the news feeds. Network code is tested with
  injected fakes; the user runs the real thing on the host.
