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

# Where the interactive pick-list page lives once GitHub Pages is turned on
# for this repo (Settings > Pages > Deploy from a branch > main > /docs).
PAGE_URL = "https://benasaykwee.github.io/Audition-Digest/"
PAGE_PATH = "docs/index.html"  # relative to the repo root, must match Pages source

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

Return a JSON array only, no other text, one object per listing, in the same order as given. The "decision" field must be exactly the string "keep" or the string "exclude", no other values:
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

BATCH_SIZE = 40  # keeps each classifier call's output well under the token cap

def classify_batch(client, batch, start_index):
    """Classifies one batch of listings (already-1-indexed relative to the
    full list via start_index). Returns {absolute_index: decision_dict}."""
    numbered = "\n".join(
        f"{start_index + i}. Title: {item['title']} | Company: {item['company']} | "
        f"Role: {item['role']} | Location: {item['location']} | "
        f"Deadline: {item['deadline']}"
        for i, item in enumerate(batch)
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8192,
        system=CLASSIFIER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": numbered}],
    )

    raw = response.content[0].text.strip()
    import json
    try:
        decisions = json.loads(raw)
    except json.JSONDecodeError:
        try:
            # model sometimes wraps JSON in a code fence despite instructions
            cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            decisions = json.loads(cleaned)
        except json.JSONDecodeError:
            # log a chunk of the raw response so a future failure is debuggable
            # from the Actions log alone, without needing another round trip
            log(f"Classifier returned unparseable JSON for batch starting at {start_index}. "
                f"First 500 chars: {raw[:500]!r}")
            raise

    return {d["id"]: d for d in decisions}

def classify(listings):
    """Calls the Claude API in batches (to keep each response well under the
    output token cap) and returns the same listings, each with 'decision',
    'ambiguous', and 'reason' added."""
    if not listings:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    by_id = {}
    for start in range(0, len(listings), BATCH_SIZE):
        batch = listings[start : start + BATCH_SIZE]
        by_id.update(classify_batch(client, batch, start + 1))

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
    """The email itself. Deliberately short: a count and a link to the
    interactive pick-list page (render_listing_page), rather than every
    listing inline, since a wall of ~150 listings in an inbox is the
    overwhelming thing this page exists to fix."""
    total = len(this_week) + len(upcoming)
    urgent = sum(1 for i in this_week if parse_date(i["deadline"]) == date.today())

    urgent_line = (
        f'<div style="color:#a5514f;font-size:13px;margin-top:10px;">{urgent} of these close TODAY.</div>'
        if urgent else ""
    )

    return f"""<div style="background:#14100f;padding:24px 0;font-family:Georgia,'Times New Roman',serif;">
<table role="presentation" width="600" align="center" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;margin:0 auto;background:#1a1614;border:1px solid #c9a227;">
<tr><td style="padding:26px 28px;text-align:center;border-bottom:1px solid #c9a227;">
<div style="color:#8a7350;font-size:11px;letter-spacing:4px;text-transform:uppercase;margin-bottom:8px;">Weekly casting notes</div>
<div style="color:#c9a227;font-size:28px;letter-spacing:1px;">The Audition Digest</div>
<div style="color:#8f6b6b;font-size:13px;margin-top:8px;font-style:italic;">Week of {week_of}</div>
</td></tr>
<tr><td style="padding:28px 28px 8px;text-align:center;color:#ece6da;font-size:16px;line-height:1.6;">
{total} listings made it through this week, {len(borderline)} flagged as borderline, pulled from {sources_used}.
{urgent_line}
</td></tr>
<tr><td style="padding:16px 28px 28px;text-align:center;">
<a href="{PAGE_URL}" style="display:inline-block;background:#c9a227;color:#1a1614;font-family:Georgia,serif;font-size:15px;font-weight:bold;text-decoration:none;padding:14px 28px;border:1px solid #8a7350;">Open this week's listings</a>
</td></tr>
<tr><td style="padding:0 28px 24px;color:#6b6355;font-size:11px;line-height:1.6;border-top:1px solid #3a332c;padding-top:16px;">
That page lets you check off what you want to pursue and dismiss the rest, then email yourself just your picks. Sources: {sources_used}. NYCastings and Mandy.com aren't in this feed yet, both need a headless-browser fetch.
</td></tr>
</table>
</div>"""

def render_listing_page(this_week, upcoming, borderline, week_of, sources_used, recipient_address):
    """The full interactive checklist page, published to GitHub Pages
    (see PAGE_URL / PAGE_PATH). Plain HTML/CSS/JS, no build step, no
    external libraries, so it keeps working with nothing to maintain.

    Each listing gets a checkbox ("I'm interested") and a dismiss button
    ("not for me"). Picks and dismissals are remembered per-week in the
    browser's local storage, so reloading the page during the week doesn't
    lose progress. "Email my picks" builds a plain-text summary of the
    checked items and hands off to the browser's own mail client, same
    inbox Quest Board already watches, no new integration needed."""

    def row(item, idx):
        where = item["company"] or item["location"] or item["source"]
        return f"""<div class="listing" data-key="{idx}" data-title="{html_attr(item['title'])}" data-where="{html_attr(where)}" data-deadline="{html_attr(item['deadline'])}" data-url="{html_attr(item['url'])}">
  <div class="row-top">
    <label class="pick-label"><input type="checkbox" class="pick"> <span class="pick-title">{item['title']}</span>{' <span class="pick-company">— ' + item['company'] + '</span>' if item['company'] else ''}</label>
    <button type="button" class="dismiss" title="Not for me">dismiss</button>
  </div>
  <div class="meta">{meta_line(item)}</div>
  <div class="reason">{item['reason']}</div>
</div>"""

    this_week_html = "\n".join(row(i, f"tw{n}") for n, i in enumerate(this_week)) or '<p class="empty">Nothing this week.</p>'
    upcoming_html = "\n".join(row(i, f"up{n}") for n, i in enumerate(upcoming)) or '<p class="empty">Nothing further out yet.</p>'
    borderline_html_block = "\n".join(row(i, f"bl{n}") for n, i in enumerate(borderline)) or '<p class="empty">Nothing borderline this week.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audition Digest — week of {week_of}</title>
<style>
  body {{ background:#14100f; color:#ece6da; font-family:Georgia,'Times New Roman',serif; margin:0; padding:24px 16px 80px; }}
  .wrap {{ max-width:640px; margin:0 auto; }}
  header {{ text-align:center; border-bottom:1px solid #c9a227; padding-bottom:20px; margin-bottom:20px; }}
  .tagline {{ color:#8a7350; font-size:11px; letter-spacing:4px; text-transform:uppercase; margin-bottom:8px; }}
  h1 {{ color:#c9a227; font-size:28px; letter-spacing:1px; margin:0; }}
  .weekof {{ color:#8f6b6b; font-size:13px; margin-top:8px; font-style:italic; }}
  .picks-bar {{ position:sticky; top:0; background:#1a1614; border:1px solid #c9a227; padding:12px 16px; margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
  .picks-count {{ color:#ece6da; font-size:14px; }}
  .picks-count b {{ color:#c9a227; }}
  button.send {{ background:#c9a227; color:#1a1614; font-family:Georgia,serif; font-size:14px; font-weight:bold; border:1px solid #8a7350; padding:10px 18px; cursor:pointer; }}
  button.send:disabled {{ opacity:0.4; cursor:default; }}
  h2 {{ color:#2f6f52; font-size:15px; letter-spacing:2px; text-transform:uppercase; border-bottom:1px solid #2f6f52; padding-bottom:6px; margin:28px 0 12px; }}
  .listing {{ border:1px solid #3a332c; padding:12px 14px; margin-bottom:10px; background:#1a1614; }}
  .listing.picked {{ border-color:#c9a227; background:#231c14; }}
  .listing.dismissed {{ display:none; }}
  .row-top {{ display:flex; justify-content:space-between; align-items:flex-start; gap:10px; }}
  .pick-label {{ display:flex; align-items:flex-start; gap:8px; cursor:pointer; font-size:15px; }}
  .pick-label input {{ margin-top:4px; }}
  .pick-title {{ color:#c9a227; }}
  .pick-company {{ color:#ece6da; }}
  .dismiss {{ background:none; border:1px solid #5c1a1a; color:#a5514f; font-size:11px; padding:4px 10px; cursor:pointer; white-space:nowrap; }}
  .meta {{ color:#8f8878; font-size:12px; margin:6px 0 4px 24px; }}
  .reason {{ color:#7a9d8a; font-size:12px; font-style:italic; margin-left:24px; }}
  .empty {{ color:#8f8878; font-size:13px; }}
  .borderline h2 {{ color:#a5514f; border-color:#5c1a1a; }}
  footer {{ color:#6b6355; font-size:11px; line-height:1.6; border-top:1px solid #3a332c; padding-top:16px; margin-top:32px; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div class="tagline">Weekly casting notes</div>
  <h1>The Audition Digest</h1>
  <div class="weekof">Week of {week_of}</div>
</header>

<div class="picks-bar">
  <div class="picks-count"><b id="pick-count">0</b> picked so far</div>
  <button type="button" class="send" id="send-btn" disabled>Email my picks</button>
</div>

<h2>This week</h2>
{this_week_html}

<h2>Upcoming</h2>
{upcoming_html}

<div class="borderline">
<h2>Borderline — worth a second look</h2>
{borderline_html_block}
</div>

<footer>
Sources: {sources_used}. Check things off as you go, they'll stay checked if you come back to this page later this week. "Email my picks" sends a plain list to {recipient_address}, the same inbox Quest Board reads from.
</footer>
</div>

<script>
(function() {{
  var weekKey = "digest-{week_of}".replace(/[^a-zA-Z0-9]/g, "-");
  var recipient = {recipient_address!r};

  function storageKey(el) {{ return weekKey + ":" + el.dataset.key; }}

  function updateCount() {{
    var picked = document.querySelectorAll(".listing .pick:checked").length;
    document.getElementById("pick-count").textContent = picked;
    document.getElementById("send-btn").disabled = picked === 0;
  }}

  document.querySelectorAll(".listing").forEach(function(el) {{
    var saved = localStorage.getItem(storageKey(el));
    var checkbox = el.querySelector(".pick");
    if (saved === "picked") {{
      checkbox.checked = true;
      el.classList.add("picked");
    }} else if (saved === "dismissed") {{
      el.classList.add("dismissed");
    }}

    checkbox.addEventListener("change", function() {{
      el.classList.toggle("picked", checkbox.checked);
      localStorage.setItem(storageKey(el), checkbox.checked ? "picked" : "");
      updateCount();
    }});

    el.querySelector(".dismiss").addEventListener("click", function() {{
      el.classList.add("dismissed");
      localStorage.setItem(storageKey(el), "dismissed");
      updateCount();
    }});
  }});

  document.getElementById("send-btn").addEventListener("click", function() {{
    var lines = [];
    document.querySelectorAll(".listing .pick:checked").forEach(function(cb) {{
      var el = cb.closest(".listing");
      lines.push("- " + el.dataset.title + " (" + el.dataset.where + ") — deadline " + (el.dataset.deadline || "n/a") + (el.dataset.url ? " — " + el.dataset.url : ""));
    }});
    var subject = "My audition picks — week of {week_of}";
    var body = "Picked from this week's audition digest:\\n\\n" + lines.join("\\n");
    window.location.href = "mailto:" + recipient + "?subject=" + encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
  }});

  updateCount();
}})();
</script>
</body>
</html>"""

def html_attr(s):
    """Minimal escaping for values placed inside HTML attributes."""
    return (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")

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
    recipient_address = os.environ["GMAIL_RECIPIENT"]

    page_html = render_listing_page(this_week, upcoming, borderline, week_of, sources_used, recipient_address)
    os.makedirs(os.path.dirname(PAGE_PATH), exist_ok=True)
    with open(PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(page_html)
    log(f"Wrote pick-list page to {PAGE_PATH}")

    email_html = render_html(this_week, upcoming, borderline, week_of, sources_used)
    subject = f"Audition Digest — week of {week_of}"
    send_email(email_html, subject)

if __name__ == "__main__":
    main()
