#!/usr/bin/env python3
"""
Audition Digest -- weekly job.

Pulls audition listings from public feeds, drops anything that isn't
actually a performer audition (stage manager postings, non-audition gig
listings), runs what's left through the Claude API against Ben's
exclusion criteria, and emails the result.

This is meant to run standalone, outside of any Claude chat. See
.github/workflows/digest.yml for the scheduled job that runs it.

Required environment variables (set as GitHub repo secrets, never
committed to the repo):
    ANTHROPIC_API_KEY   Claude API key, from console.anthropic.com
    GMAIL_ADDRESS        the Gmail account that logs in and SENDS the digest
                          (this is the one with the app password below)
    GMAIL_APP_PASSWORD   the app password for GMAIL_ADDRESS (NOT its account password)
    GMAIL_RECIPIENT       the Gmail address the digest actually gets sent TO
                          (a different address from GMAIL_ADDRESS, on purpose)

--- Known rough edge, read before the first live run ---
The BroadwayWorld and Playbill parsers below were written from a cleaned
text preview of each page, not from the raw HTML, since the environment
that built this script couldn't fetch these two sites directly to inspect
real CSS selectors. They use resilient patterns (matching on URL shape and
known text patterns) rather than exact class names, which should hold up,
but the FIRST run should be a manual one (see README) so any parsing
mismatch shows up immediately in the debug output instead of silently
sending an empty or broken digest.

Feeds NOT included yet: NYCastings and Mandy.com. Both render their real
listings with JavaScript, so a plain HTTP fetch (what this script does)
comes back empty for those two. Adding them needs a headless-browser step
(e.g. Playwright), which is heavier to run on a schedule, deliberately left
out of v1 to keep this simple and working first.
"""

import os
import re
import sys
import smtplib
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
import anthropic

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

BROADWAYWORLD_URL = "https://www.broadwayworld.com/theatre-auditions/"
PLAYBILL_URL = "https://playbill.com/jobs"

# BroadwayWorld role types that are never a performer audition. Dropped
# before classification runs, since this is a plain category check, not
# a judgment call.
BWW_DROP_ROLE_SUBSTRINGS = ["stage manager"]

# Playbill lists many job categories on the same page. Only "Performer"
# postings belong in an audition digest.
PLAYBILL_KEEP_CATEGORIES = {"Performer"}

PLAYBILL_CATEGORIES = [
    "Academic/Instructor", "Administrative", "Career Services and Spaces",
    "Classes", "Coaching", "Design", "Directorial", "Editorial/Writing",
    "Festival/Competition Submissions", "Internship", "Musician",
    "Non-Theatrical", "Other", "Performer", "Services", "Technical",
]

CLASSIFIER_SYSTEM_PROMPT = """You are the filter for a personal weekly audition digest. Ben is a performer reviewing casting listings pulled from public audition feeds. Given a batch of listings, decide which to keep and which to drop, based only on the criteria below. This digest is for Ben's personal use only. It is never published or shared.

Drop a listing if it is:
- A cruise line audition (any onboard cruise ship performing role)
- A theme park audition (Disney, Universal, Six Flags, or any amusement/theme park entertainment role)
- A dancer-only call (the role is exclusively for dancers, with no singing or acting component)
- A women-only call (the casting notice restricts submissions to women or female-identifying performers)

Keep everything else. That includes calls that don't mention gender, mixed-gender ensembles, and any role for actors, singers, or actor-singers.

If a listing is ambiguous, keep it, mark it ambiguous, and explain why you weren't sure, rather than silently dropping something that might matter.

Return a JSON array only, no other text, one object per listing, in the same order as given:
[{"id": 1, "decision": "keep", "ambiguous": false, "reason": "one sentence"}, ...]"""


def log(msg):
    print(f"[digest] {msg}", file=sys.stderr)


def fetch_broadwayworld():
    """Returns a list of listing dicts pulled from BroadwayWorld's audition board."""
    resp = requests.get(BROADWAYWORLD_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    listings = []
    seen = set()
    for link in soup.find_all("a", href=re.compile(r"/equity-audition/")):
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue

        # The company/role/location/deadline line sits near the title link
        # in the page's own layout, separated by middle-dots ("·").
        meta_text = ""
        container = link.find_parent(["div", "li", "article"])
        if container:
            meta_text = container.get_text(" ", strip=True)
        if meta_text.startswith(title):
            # the container's text usually includes the title itself first,
            # since the link lives inside it, strip that back off
            meta_text = meta_text[len(title):].strip()
        parts = [p.strip() for p in meta_text.split("·") if p.strip()]
        company = parts[0] if len(parts) > 0 else ""
        role = parts[1] if len(parts) > 1 else ""
        date_match = re.search(r"\d{1,2}/\d{1,2}/\d{4}", meta_text)
        deadline = date_match.group(0) if date_match else ""
        location = ""
        if len(parts) > 2:
            location = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{4}\s*$", "", parts[2]).strip()

        key = (title, company, role, deadline)
        if key in seen:
            continue
        seen.add(key)

        if any(s in role.lower() for s in BWW_DROP_ROLE_SUBSTRINGS):
            continue  # pre-filter: not a performer role, skip before classifying

        listings.append({
            "title": title,
            "company": company,
            "role": role or "Performer",
            "location": location,
            "deadline": deadline,
            "url": href if href.startswith("http") else "https://www.broadwayworld.com" + href,
            "source": "BroadwayWorld",
        })

    log(f"BroadwayWorld: parsed {len(listings)} performer listings")
    return listings


def fetch_playbill():
    """Returns a list of listing dicts pulled from Playbill's job board, Performer category only."""
    resp = requests.get(PLAYBILL_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    cat_pattern = "|".join(re.escape(c) for c in PLAYBILL_CATEGORIES)
    listings = []
    for link in soup.find_all("a", href=re.compile(r"^https?://playbill\.com/job/")):
        text = link.get_text(" ", strip=True)
        if not text:
            continue

        date_match = re.search(r"\d{2}/\d{2}/\d{4}$", text)
        deadline = date_match.group(0) if date_match else ""
        body = text[: date_match.start()].strip() if date_match else text

        cat_match = re.match(rf"({cat_pattern})", body)
        category = cat_match.group(1) if cat_match else ""
        if category not in PLAYBILL_KEEP_CATEGORIES:
            continue  # pre-filter: only performer-category postings

        rest = body[len(category):].strip()
        paid = rest.startswith("Paid")
        if paid:
            rest = rest[len("Paid"):].strip()
        rest = re.sub(r"\s+US$", "", rest).strip()  # trailing country code, not meaningful content

        # Playbill's listing text flattens title, company, and city into one
        # run with no reliable delimiter between them (unlike BroadwayWorld,
        # which uses a real "·" separator). Splitting company/location out
        # of that blob turned out to be unreliable in testing, capitalized
        # company names get confused with capitalized city names, so this
        # keeps the whole thing as one description rather than guessing
        # wrong and mislabeling a company as a city. The listing's own link
        # still goes to the full posting if the detail matters.
        listings.append({
            "title": rest,
            "company": "",
            "role": "Performer" + (" (Paid)" if paid else ""),
            "location": "",
            "deadline": deadline,
            "url": link.get("href", ""),
            "source": "Playbill",
        })

    log(f"Playbill: parsed {len(listings)} performer-category listings")
    return listings


def drop_expired(listings, today):
    kept = []
    for item in listings:
        d = parse_date(item["deadline"])
        if d and d < today:
            continue  # deadline already passed, not actionable
        kept.append(item)
    return kept


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        return None


def classify(listings):
    """Calls the Claude API once with the whole batch. Returns the same
    listings, each with 'decision', 'ambiguous', and 'reason' added."""
    if not listings:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    numbered = "\n".join(
        f"{i+1}. Title: {item['title']} | Company: {item['company']} | "
        f"Role: {item['role']} | Location: {item['location']} | "
        f"Deadline: {item['deadline']}"
        for i, item in enumerate(listings)
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4096,
        system=CLASSIFIER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": numbered}],
    )

    raw = response.content[0].text.strip()
    import json
    try:
        decisions = json.loads(raw)
    except json.JSONDecodeError:
        # model sometimes wraps JSON in a code fence despite instructions
        cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        decisions = json.loads(cleaned)

    by_id = {d["id"]: d for d in decisions}
    for i, item in enumerate(listings):
        d = by_id.get(i + 1, {"decision": "keep", "ambiguous": True, "reason": "No classifier response for this item."})
        item["decision"] = d.get("decision", "keep")
        item["ambiguous"] = d.get("ambiguous", False)
        item["reason"] = d.get("reason", "")

    log(f"Classifier: {sum(1 for i in listings if i['decision']=='keep')} kept, "
        f"{sum(1 for i in listings if i['decision']=='exclude')} dropped, "
        f"{sum(1 for i in listings if i.get('ambiguous'))} ambiguous")
    return listings


def meta_line(item):
    parts = [item["role"]]
    if item["location"]:
        parts.append(item["location"])
    parts.append(f"Deadline {item['deadline']}" if item["deadline"] else "No deadline listed")
    parts.append(item["source"])
    return " &middot; ".join(parts)


def listing_html(item):
    return f"""<p style="margin:0 0 14px;"><b style="color:#c9a227;">{item['title']}</b>{' — ' + item['company'] if item['company'] else ''}<br>
<span style="color:#8f8878;font-size:12px;">{meta_line(item)}</span><br>
<i style="color:#7a9d8a;font-size:12px;">{item['reason']}</i></p>"""


def borderline_html(item):
    where = item["company"] or item["location"] or item["source"]
    return f"""<p style="margin:0 0 10px;color:#c9bfae;font-size:12px;line-height:1.5;"><b style="color:#e0d3b0;">{item['title']}</b> ({where}) &mdash; {item['reason']}</p>"""


def render_html(this_week, upcoming, borderline, week_of, sources_used):
    this_week_html = "\n".join(listing_html(i) for i in this_week) or "<p style=\"color:#8f8878;font-size:13px;\">Nothing this week.</p>"
    upcoming_html = "\n".join(listing_html(i) for i in upcoming) or "<p style=\"color:#8f8878;font-size:13px;\">Nothing further out yet.</p>"
    borderline_html_block = "\n".join(borderline_html(i) for i in borderline)
    borderline_section = f"""<tr><td style="padding:20px 28px 0;">
<div style="border:1px solid #5c1a1a;padding:14px 16px;background:#1f1512;">
<div style="color:#a5514f;font-size:13px;letter-spacing:1px;text-transform:uppercase;margin-bottom:8px;">Borderline &mdash; worth a second look</div>
{borderline_html_block}
</div>
</td></tr>""" if borderline else ""

    return f"""<div style="background:#14100f;padding:24px 0;font-family:Georgia,'Times New Roman',serif;">
<table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;margin:0 auto;background:#1a1614;border:1px solid #c9a227;">
<tr><td style="padding:26px 28px;text-align:center;border-bottom:1px solid #c9a227;">
<div style="color:#8a7350;font-size:11px;letter-spacing:4px;text-transform:uppercase;margin-bottom:8px;">Weekly casting notes</div>
<div style="color:#c9a227;font-size:28px;letter-spacing:1px;">The Audition Digest</div>
<div style="color:#8f6b6b;font-size:13px;margin-top:8px;font-style:italic;">Week of {week_of}</div>
</td></tr>
<tr><td style="padding:20px 28px 4px;color:#c9bfae;font-size:13px;line-height:1.6;">
{len(this_week) + len(upcoming)} listings made it through this week, pulled from {sources_used}.
</td></tr>
<tr><td style="padding:18px 28px 0;">
<div style="color:#2f6f52;font-size:14px;letter-spacing:2px;text-transform:uppercase;border-bottom:1px solid #2f6f52;padding-bottom:6px;">This week</div>
</td></tr>
<tr><td style="padding:12px 28px 0;color:#ece6da;font-size:14px;line-height:1.5;">
{this_week_html}
</td></tr>
<tr><td style="padding:18px 28px 0;">
<div style="color:#2f6f52;font-size:14px;letter-spacing:2px;text-transform:uppercase;border-bottom:1px solid #2f6f52;padding-bottom:6px;">Upcoming</div>
</td></tr>
<tr><td style="padding:12px 28px 0;color:#ece6da;font-size:14px;line-height:1.5;">
{upcoming_html}
</td></tr>
{borderline_section}
<tr><td style="padding:20px 28px 24px;color:#6b6355;font-size:11px;line-height:1.6;border-top:1px solid #3a332c;margin-top:10px;">
Sources: {sources_used}. NYCastings and Mandy.com aren't in this feed yet, both need a headless-browser fetch. Stage manager postings and non-performer categories were filtered out before classification ran.
</td></tr>
</table>
</div>"""


def send_email(html, subject):
    # GMAIL_ADDRESS is the sending account only, it logs in and sends but
    # is never the destination. GMAIL_RECIPIENT is where the digest actually
    # lands. These are deliberately two different Gmail accounts.
    sender_address = os.environ["GMAIL_ADDRESS"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient_address = os.environ["GMAIL_RECIPIENT"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_address
    msg["To"] = recipient_address
    msg.attach(MIMEText("This email requires HTML to view. Open it in a normal email client.", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_address, app_password)
        server.send_message(msg)
    log(f"Sent digest from {sender_address} to {recipient_address}")


def main():
    today = date.today()
    week_of = today.strftime("%B %d, %Y")
    cutoff = today + timedelta(days=6)  # "this week" window

    all_listings = fetch_broadwayworld() + fetch_playbill()
    all_listings = drop_expired(all_listings, today)
    log(f"{len(all_listings)} performer listings after pre-filter and expiry check")

    all_listings = classify(all_listings)

    kept = [i for i in all_listings if i["decision"] == "keep" and not i.get("ambiguous")]
    borderline = [i for i in all_listings if i.get("ambiguous")]

    def in_this_week(item):
        d = parse_date(item["deadline"])
        return d is None or d <= cutoff

    this_week = [i for i in kept if in_this_week(i)]
    upcoming = [i for i in kept if not in_this_week(i)]

    sources_used = ", ".join(sorted(set(i["source"] for i in all_listings))) or "no sources"
    html = render_html(this_week, upcoming, borderline, week_of, sources_used)

    subject = f"Audition Digest — week of {week_of}"
    send_email(html, subject)


if __name__ == "__main__":
    main()
