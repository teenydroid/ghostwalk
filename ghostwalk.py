#!/usr/bin/env python3
"""ghostwalk: name the rows a listing lost, and flag same-author ghost twins.

Reads two exports of the same record listing (rows carrying id, author,
timestamp, status; JSON or CSV) and reports:

  * every row present in the old export but gone from the new one;
  * every same-author pair created within a seconds window
    (default 15s: the ghost's signature), one of which is often the
    ghost of its sibling.

Usage:
    ghostwalk OLD_EXPORT NEW_EXPORT [--window SECONDS] [--json]

Exit codes: 0 clean listing, 1 something found (lost rows or twin pairs),
2 bad input (unreadable file, malformed export, bad arguments).
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone

FIELD_ORDER = ("id", "author", "timestamp", "status")
DEFAULT_WINDOW = 15.0
VERSION = "1.0.0"


class GhostwalkError(Exception):
    """Raised for unreadable or malformed exports."""


# ---------------------------------------------------------------- parsing

def parse_timestamp(value):
    """Parse a row timestamp into an aware UTC datetime, or None.

    Accepts epoch seconds (number or numeric string) and ISO 8601
    strings; naive ISO timestamps are read as UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        pass
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_rows(path):
    """Load export rows from a JSON or CSV file.

    JSON may be a bare list of row objects, an object holding the list
    under a key such as "rows", a mapping of id -> row object, or
    newline-delimited JSON (one row object per line).
    Returns a list of dicts.
    """
    with open(path, "r", newline="", encoding="utf-8") as handle:
        head = handle.read(4096).lstrip()
        handle.seek(0)
        if head.startswith("{") or head.startswith("["):
            text = handle.read()
            try:
                return _rows_from_json(json.loads(text))
            except json.JSONDecodeError:
                return _rows_from_jsonl(text)  # one JSON object per line?
        return _rows_from_csv(handle)


def _rows_from_json(data):
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("rows", "records", "items", "data", "results"):
            if isinstance(data.get(key), list):
                return [dict(row) for row in data[key] if isinstance(row, dict)]
        if "id" in data:  # a bare row object: single-line JSONL or one-row export
            return [dict(data)]
        if data and all(isinstance(value, dict) for value in data.values()):
            rows = []
            for key, value in data.items():
                row = dict(value)
                row.setdefault("id", key)
                rows.append(row)
            return rows
    raise GhostwalkError(
        "JSON export must be a list of row objects, an object with a "
        "'rows' list, or a mapping of id to row object"
    )


def _rows_from_jsonl(text):
    """Parse newline-delimited JSON: one row object per non-blank line."""
    rows = []
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise GhostwalkError(
                "line %d is not valid JSON and the file is not a single "
                "JSON document" % line_no)
        if not isinstance(value, dict):
            raise GhostwalkError("JSONL line %d is not a row object" % line_no)
        rows.append(value)
    if not rows:
        raise GhostwalkError("no JSON rows found in export")
    return rows


def _rows_from_csv(handle):
    reader = csv.DictReader(handle)
    if reader.fieldnames is None:
        raise GhostwalkError("CSV export has no header row")
    rows = []
    for raw in reader:
        if None in raw:  # more cells than headers: dict gets a None key
            raise GhostwalkError("CSV row has more cells than the header")
        rows.append({key: value for key, value in raw.items() if key is not None})
    return rows


def index_rows(rows):
    """Key rows by id. Returns ({id: row}, duplicate_id_count, no_id_count)."""
    index = {}
    duplicates = 0
    missing_id = 0
    for row in rows:
        rid = row.get("id")
        if rid is None or (isinstance(rid, str) and not rid.strip()):
            missing_id += 1
            continue
        key = str(rid)
        if key in index:
            duplicates += 1
            continue
        index[key] = row
    return index, duplicates, missing_id


# --------------------------------------------------------------- analysis

def _row_sort_key(row):
    stamp = parse_timestamp(row.get("timestamp"))
    return (stamp is None, stamp or datetime.min.replace(tzinfo=timezone.utc),
            str(row.get("id")))


def find_lost(old_index, new_index):
    """Rows present in the old export but gone from the new one."""
    lost = [row for rid, row in old_index.items() if rid not in new_index]
    lost.sort(key=_row_sort_key)
    return lost


def combined_rows(old_index, new_index):
    """id -> (row, lost). A surviving row keeps the new export's copy."""
    combined = {}
    for rid, row in old_index.items():
        combined[rid] = (row, True)
    for rid, row in new_index.items():
        combined[rid] = (row, False)
    return combined


def find_pairs(old_index, new_index, window):
    """Same-author pairs created within `window` seconds of each other.

    Returns (pairs, skipped) where each pair is a dict with kind:
      ghost - one side lost, its sibling survived (the signature case)
      gone  - both sides lost
      twin  - both sides still present
    `skipped` counts rows that could not take part (missing author or
    unreadable timestamp).
    """
    by_author = defaultdict(list)
    skipped = 0
    for rid, (row, lost) in sorted(combined_rows(old_index, new_index).items()):
        author = row.get("author")
        stamp = parse_timestamp(row.get("timestamp"))
        if author is None or (isinstance(author, str) and not author.strip()) \
                or stamp is None:
            skipped += 1
            continue
        by_author[str(author).strip()].append((rid, row, lost, stamp))

    pairs = []
    for author, entries in by_author.items():
        entries.sort(key=lambda item: (item[3], item[0]))
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                delta = abs((entries[j][3] - entries[i][3]).total_seconds())
                if delta > window:
                    break  # sorted by time: the rest only drift further
                pairs.append(_pair(author, entries[i], entries[j], delta))
    pairs.sort(key=lambda pair: (pair["delta_seconds"], str(pair["older_id"]),
                                 str(pair["newer_id"])))
    return pairs, skipped


def _pair(author, side_a, side_b, delta):
    """Build one twin pair; side_a is the earlier row of the two."""
    older, newer = (side_a, side_b)
    if side_a[3] > side_b[3]:
        older, newer = (side_b, side_a)
    elif side_a[3] == side_b[3]:
        # same instant: order by id so "older/newer" stays deterministic
        if str(side_b[0]) < str(side_a[0]):
            older, newer = (side_b, side_a)
    lost_a, lost_b = older[2], newer[2]
    if lost_a != lost_b:
        kind = "ghost"   # one lost, its sibling survived: the signature case
    elif lost_a and lost_b:
        kind = "gone"    # both vanished
    else:
        kind = "twin"    # both still present
    return {
        "kind": kind,
        "author": author,
        "delta_seconds": delta,
        "older_id": older[0],
        "newer_id": newer[0],
        "older": _side(older),
        "newer": _side(newer),
    }


def _side(entry):
    rid, row, lost, _stamp = entry
    return {
        "id": rid,
        "lost": lost,
        "timestamp": row.get("timestamp"),
        "status": row.get("status"),
    }


def walk(old_rows, new_rows, window):
    """Run both checks. Returns the full findings dict."""
    old_index, old_dups, old_noid = index_rows(old_rows)
    new_index, new_dups, new_noid = index_rows(new_rows)
    lost = find_lost(old_index, new_index)
    pairs, skipped = find_pairs(old_index, new_index, window)
    skipped += old_noid + new_noid
    return {
        "window_seconds": window,
        "counts": {
            "old_rows": len(old_index),
            "new_rows": len(new_index),
            "lost": len(lost),
            "twin_pairs": len(pairs),
            "duplicate_ids": old_dups + new_dups,
            "rows_skipped_for_twins": skipped,
        },
        "lost": lost,
        "twin_pairs": pairs,
    }


# ---------------------------------------------------------------- output

def fmt_row(row):
    parts = []
    for field in FIELD_ORDER:
        if row.get(field) not in (None, ""):
            parts.append("%s=%s" % (field, row[field]))
    for key in sorted((str(k) for k in row if k not in FIELD_ORDER)):
        if row[key] not in (None, ""):
            parts.append("%s=%s" % (key, row[key]))
    return " ".join(parts) if parts else "(empty row)"


def fmt_seconds(value):
    return "%gs" % value


def render_human(findings, old_path, new_path):
    counts = findings["counts"]
    lines = [
        "ghostwalk: %s -> %s (window %s)"
        % (old_path, new_path, fmt_seconds(findings["window_seconds"])),
        "rows: %d old, %d new" % (counts["old_rows"], counts["new_rows"]),
    ]
    if findings["lost"]:
        lines.append("")
        lines.append("lost rows (in old, gone from new): %d"
                     % len(findings["lost"]))
        for row in findings["lost"]:
            lines.append("  - %s" % fmt_row(row))
    if findings["twin_pairs"]:
        lines.append("")
        lines.append("ghost twins (same author, <= %s apart): %d"
                     % (fmt_seconds(findings["window_seconds"]),
                        len(findings["twin_pairs"])))
        for pair in findings["twin_pairs"]:
            lines.append("  [%s] id=%s (%s) <-> id=%s (%s) | author=%s | %s apart"
                         % (pair["kind"], pair["older_id"],
                            "lost" if pair["older"]["lost"] else "present",
                            pair["newer_id"],
                            "lost" if pair["newer"]["lost"] else "present",
                            pair["author"],
                            fmt_seconds(pair["delta_seconds"])))
    notes = []
    if counts["duplicate_ids"]:
        notes.append("%d duplicate id(s), kept first occurrence"
                     % counts["duplicate_ids"])
    if counts["rows_skipped_for_twins"]:
        notes.append("%d row(s) skipped for twin detection "
                     "(no id, author or readable timestamp)"
                     % counts["rows_skipped_for_twins"])
    if notes:
        lines.append("")
        lines.append("notes: " + "; ".join(notes))
    lines.append("")
    if findings["lost"] or findings["twin_pairs"]:
        lines.append("verdict: %d lost row(s), %d twin pair(s) -> exit 1"
                     % (len(findings["lost"]), len(findings["twin_pairs"])))
    else:
        lines.append("verdict: clean (nothing lost, no twin pairs)")
    return "\n".join(lines)


# ------------------------------------------------------------------- cli

def build_parser():
    parser = argparse.ArgumentParser(
        prog="ghostwalk",
        description="Name the rows a listing lost between two exports and "
                    "flag same-author ghost twins (rows created within "
                    "seconds of a sibling).",
    )
    parser.add_argument("old", help="old export of the listing (JSON or CSV)")
    parser.add_argument("new", help="new export of the listing (JSON or CSV)")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW,
                        metavar="SECONDS",
                        help="max seconds between same-author creations to "
                             "count as twins (default: %g)" % DEFAULT_WINDOW)
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print findings as JSON instead of text")
    parser.add_argument("--version", action="version",
                        version="ghostwalk " + VERSION)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        old_rows = load_rows(args.old)
        new_rows = load_rows(args.new)
        findings = walk(old_rows, new_rows, args.window)
    except GhostwalkError as error:
        print("ghostwalk: %s" % error, file=sys.stderr)
        return 2
    except OSError as error:
        print("ghostwalk: cannot read export: %s" % error, file=sys.stderr)
        return 2
    except (json.JSONDecodeError, csv.Error, ValueError) as error:
        print("ghostwalk: malformed export: %s" % error, file=sys.stderr)
        return 2

    if args.as_json:
        findings["old_path"] = args.old
        findings["new_path"] = args.new
        print(json.dumps(findings, indent=2, default=str))
    else:
        print(render_human(findings, args.old, args.new))
    return 1 if (findings["lost"] or findings["twin_pairs"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
