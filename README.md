# ghostwalk

A stdlib-only Python 3 CLI that reads two exports of the same record
listing and answers two questions:

1. **Lost rows** — which rows were in the old export but are gone from
   the new one? Each one is named, with its id, author, timestamp and
   status.
2. **Ghost twins** — which rows by the same author were created within
   seconds of each other (default window: 15 seconds, the ghost's
   signature)? Pairs where one side vanished and its sibling survived
   are flagged as `ghost`; pairs where both sides vanished are `gone`;
   pairs where both still exist are `twin`.

Exit code **0** means the listing walked clean, **1** means something
was found (lost rows and/or twin pairs), **2** means bad input
(unreadable file, malformed export, bad arguments) — so it drops into
cron jobs and CI without a wrapper.

## Why

People keep registries where a record disappears and, seconds later, a
near-identical sibling by the same author appears: a ghost of its
former self. Spotting that by eye means noticing the shape by hand;
ghostwalk automates the re-walk.

## Usage

```sh
python3 ghostwalk.py OLD_EXPORT NEW_EXPORT [--window SECONDS] [--json]
```

- `OLD_EXPORT`, `NEW_EXPORT`: the two listings to compare.
- `--window SECONDS`: how close two same-author creations must be to
  count as twins (default: 15).
- `--json`: machine-readable findings instead of text.

### Example

```sh
$ python3 ghostwalk.py yesterday.json today.json
ghostwalk: yesterday.json -> today.json (window 15s)
rows: 2 old, 1 new

lost rows (in old, gone from new): 1
  - id=r2 author=wren_muse timestamp=1700000012 status=active

ghost twins (same author, <= 15s apart): 1
  [ghost] id=r1 (present) <-> id=r2 (lost) | author=wren_muse | 12s apart

verdict: 1 lost row(s), 1 twin pair(s) -> exit 1
```

## Input formats

Both exports must be rows carrying at least `id`, `author` and
`timestamp` (a `status` column is shown when present). Formats, auto
detected per file:

- **JSON** — a bare list of row objects, `{"rows": [...]}` (also
  `records`, `items`, `data`, `results`), an id → row mapping, or a
  single bare row object.
- **JSONL** — one row object per line.
- **CSV** — header row plus one row per line.

Timestamps may be epoch seconds (number or numeric string) or ISO 8601
(`...Z`, offsets, and naive values read as UTC); files may mix them
freely.

Rows that make ghostwalk's job impossible are reported, not fatal:
duplicate ids keep the first occurrence and are counted; rows with no
id, author, or readable timestamp are counted under
`rows_skipped_for_twins` (they can still appear as lost rows).

## Notes

- Python 3 standard library only: no installs, no dependencies.
- Twin detection compares every same-author pair within the window,
  so three rows 5 seconds apart yield three pairs.
- Same-instant rows are ordered by id so reports are deterministic.

## Tests

```sh
python3 -m unittest test_ghostwalk -v
```

36 tests: timestamp parsing, all input formats, lost-row and ghost/gone/twin
pair detection, window boundaries, CLI exit codes (0/1/2), and JSON output.
