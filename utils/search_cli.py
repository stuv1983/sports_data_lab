#!/usr/bin/env python3
"""Command-line entry point for the shared Advanced Search compiler."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys

import query_filters_family as Q


def default_db(sport: str) -> str:
    from data_paths import sport_db
    return sport_db(sport)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="search expression")
    ap.add_argument("--sport", default="afl")
    ap.add_argument("--db")
    ap.add_argument("--format", choices=("table", "csv", "json"), default="table")
    ap.add_argument("--show-sql", action="store_true")
    args = ap.parse_args(argv)

    try:
        import sports
        sport = sports.get(args.sport)
        db = args.db or default_db(args.sport)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ensure = getattr(sport.C, "ensure_captain_table", None)
        if ensure:
            ensure(con)
        sql, params, _spec = Q.compile_query(
            sport.schema, args.query, con=con,
            extensions=sport.search_extensions())
        cur = con.execute(sql, params)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except (ImportError, OSError, sqlite3.Error, Q.QuerySyntaxError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.show_sql:
        print(sql)
        print(repr(params))

    if args.format == "json":
        print(json.dumps([dict(zip(columns, row)) for row in rows], indent=2))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        writer.writerows(rows)
    else:
        if not rows:
            print("No matches")
            return 0
        widths = [len(c) for c in columns]
        for row in rows:
            for i, value in enumerate(row):
                widths[i] = min(60, max(widths[i], len(str(value))))
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)))
        print("  ".join("-" * w for w in widths))
        for row in rows:
            print("  ".join(str(v).ljust(widths[i])[:widths[i]] for i, v in enumerate(row)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
