#!/usr/bin/env python3
"""Distill docs/index.html (the pick-list page digest.py just wrote) into
digest-out.json, shaped the way the Quest Board's digest panel expects:

    { "digest": { "subject": ..., "snippet": ..., "date": ... } }

Runs as a workflow step right after digest.py. Reads the page rather than
importing digest.py so it can never interfere with the send itself: if this
script fails, the email has already gone out.
"""

import datetime
import json
import re
import sys

PAGE_PATH = "docs/index.html"  # must match PAGE_PATH in digest.py
OUT_PATH = "digest-out.json"


def main():
    html = open(PAGE_PATH, encoding="utf-8").read()

    m = re.search(r"Week of ([^<]+)</div>", html)
    week_of = m.group(1).strip() if m else datetime.date.today().strftime("%B %d, %Y")

    # Each listing row carries data-key="tw..." (this week), "up..." (upcoming),
    # or "bl..." (borderline), with its deadline in data-deadline on the same tag.
    tw_deadlines = re.findall(r'data-key="tw[^"]*"[^>]*data-deadline="([^"]*)"', html)
    this_week = len(tw_deadlines)
    upcoming = len(re.findall(r'data-key="up', html))
    borderline = len(re.findall(r'data-key="bl', html))
    total = this_week + upcoming

    today = datetime.date.today().strftime("%m/%d/%Y")
    urgent = sum(1 for d in tw_deadlines if d == today)

    m = re.search(r"Sources: ([^.<]+)\.", html)
    sources = m.group(1).strip() if m else "BroadwayWorld"

    snippet = (
        f"{total} listings made it through this week, "
        f"{borderline} flagged as borderline, pulled from {sources}."
    )
    if urgent:
        snippet += f" {urgent} of these close TODAY."
    snippet += " Open the full digest in your email for the complete list."

    digest = {
        "digest": {
            "subject": f"Audition Digest — week of {week_of}",
            "snippet": snippet,
            "date": f"Week of {week_of}",
        }
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Wrote {OUT_PATH}:", file=sys.stderr)
    print(json.dumps(digest, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
