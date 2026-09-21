#!/usr/bin/env python3
"""Send NEW jobs from all_jobs.json to a Discord webhook.

- Stdlib only (no pip install needed).
- Sends every new job, regardless of fit score or years of experience.
- Remembers what it already sent in discord_sent.json (next to all_jobs.json),
  so each job is announced once, even if it shows up on several job boards.

Usage:
    python discord_digest.py                  # normal run
    python discord_digest.py --dry-run        # print messages, send nothing, save nothing
    python discord_digest.py --send-existing  # first run only: announce everything currently stored
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse

CANDIDATE_PATHS = ["output/all_jobs.json", "all_jobs.json"]
MAX_JOBS_PER_RUN = 60      # jobs listed in Discord per run (extras are counted, not listed)
MAX_MSG = 1900             # Discord's limit is 2000 characters per message
STATE_MAX = 20000          # cap on remembered keys
STATE_NAME = "discord_sent.json"

URL_FIELDS = ("url", "link", "job_url", "jobUrl", "apply_url", "href")
TITLE_FIELDS = ("title", "job_title", "position", "name")
COMPANY_FIELDS = ("company", "employer", "organization", "company_name", "agency")
LOCATION_FIELDS = ("location", "job_location", "city")
SOURCE_FIELDS = ("source", "site", "board")
SALARY_FIELDS = ("salary", "salary_text", "pay", "compensation")
DATE_FIELDS = ("date_posted", "posted", "posted_at", "date", "first_seen", "published")


def pick(job, names):
    for n in names:
        v = job.get(n)
        if v not in (None, "", [], {}):
            return str(v).strip()
    return ""


def find_jobs_file(explicit=None):
    paths = [explicit] if explicit else CANDIDATE_PATHS
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def load_jobs(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("jobs", "listings", "results", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [v for v in data.values() if isinstance(v, dict)]
    return [j for j in data if isinstance(j, dict)]


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def clean_url(u):
    """Drop tracking params but keep the ones that identify the job (e.g. Indeed's jk)."""
    p = urlparse(u)
    q = parse_qs(p.query)
    keep = {k: q[k] for k in ("jk", "id", "jobId", "job_id", "currentJobId") if k in q}
    return p.netloc.lower() + p.path.rstrip("/") + ("?" + urlencode(keep, doseq=True) if keep else "")


def keys_for(job):
    keys = set()
    url = pick(job, URL_FIELDS)
    if url:
        keys.add("url:" + clean_url(url))
    title = pick(job, TITLE_FIELDS)
    company = pick(job, COMPANY_FIELDS)
    loc = pick(job, LOCATION_FIELDS)
    if title and company:
        # catches the same job cross-posted on LinkedIn / Indeed / etc.
        keys.add("sig:" + "|".join((norm(company), norm(title), norm(loc))))
    return keys


def parse_date(job):
    v = pick(job, DATE_FIELDS)
    if not v:
        return datetime.min
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def esc(s):
    return re.sub(r"([*_`~|>])", r"\\\1", s)


def format_job(job):
    title = esc(pick(job, TITLE_FIELDS) or "(untitled)")[:150]
    meta = " · ".join(
        x for x in (
            esc(pick(job, COMPANY_FIELDS)),
            esc(pick(job, LOCATION_FIELDS)),
            esc(pick(job, SOURCE_FIELDS)),
            esc(pick(job, SALARY_FIELDS)),
        ) if x
    )
    url = pick(job, URL_FIELDS)
    out = f"• **{title}**"
    if meta:
        out += f"\n   {meta}"
    if url:
        out += f"\n   <{url}>"  # <> stops Discord from expanding a huge preview
    return out


def build_messages(new_jobs, extra_count, dashboard_url):
    header = f"🆕 **{len(new_jobs) + extra_count} new job(s)**"
    if dashboard_url:
        header += f" — dashboard: <{dashboard_url}>"
    messages, current = [], header
    for job in new_jobs:
        piece = format_job(job)
        if len(current) + len(piece) + 2 > MAX_MSG:
            messages.append(current)
            current = piece
        else:
            current += "\n\n" + piece
    if extra_count:
        tail = f"…and {extra_count} more. Open the dashboard to see them all."
        if len(current) + len(tail) + 2 > MAX_MSG:
            messages.append(current)
            current = tail
        else:
            current += "\n\n" + tail
    messages.append(current)
    return messages


def post(webhook, content):
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "job-scraper-discord/1.0"},
    )
    for _ in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(detail).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait + 0.5, 30))
                continue
            print(f"Discord returned HTTP {e.code}: {detail[:300]}")
            return False
        except Exception as e:  # network hiccup
            print(f"Network error: {e}")
            time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--send-existing", action="store_true")
    ap.add_argument("--jobs-file")
    args = ap.parse_args()

    path = find_jobs_file(args.jobs_file)
    if not path:
        print("Could not find all_jobs.json (looked in: " + ", ".join(CANDIDATE_PATHS) + ").")
        print("Run a scraper workflow first so the data file exists.")
        return 1

    jobs = load_jobs(path)
    print(f"Loaded {len(jobs)} job(s) from {path}")
    if jobs:
        print("Fields on first job:", ", ".join(sorted(jobs[0].keys())))
        if not pick(jobs[0], URL_FIELDS) or not pick(jobs[0], TITLE_FIELDS):
            print("WARNING: could not find a title or URL field on the first job. "
                  "Paste the line above to whoever is helping you.")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        print("DISCORD_WEBHOOK_URL is not set. Add it under Settings > Secrets and variables > Actions > Secrets.")
        return 1

    state_path = os.path.join(os.path.dirname(path) or ".", STATE_NAME)
    first_run = not os.path.exists(state_path)
    seen = set()
    if not first_run:
        with open(state_path, encoding="utf-8") as f:
            seen = set(json.load(f).get("sent", []))

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    dashboard = os.environ.get("DASHBOARD_URL", "")
    if not dashboard and "/" in repo:
        owner, name = repo.split("/", 1)
        dashboard = f"https://{owner.lower()}.github.io/{name}/triage.html"

    # Work out which jobs are new (also collapses duplicates inside this batch)
    new_jobs, new_keys = [], set()
    for job in sorted(jobs, key=parse_date, reverse=True):
        ks = keys_for(job)
        if not ks:
            continue
        if ks & seen or ks & new_keys:
            new_keys |= ks
            continue
        new_jobs.append(job)
        new_keys |= ks

    print(f"{len(new_jobs)} new job(s) since last run.")

    # First run: don't flood the channel with the whole backlog
    if first_run and not args.send_existing:
        print("First run: recording existing jobs as already seen.")
        msg = (f"✅ Job notifications are on. I'm tracking {len(new_jobs)} existing job(s); "
               "from now on you'll only get NEW ones here.")
        if dashboard:
            msg += f"\nDashboard: <{dashboard}>"
        if args.dry_run:
            print(msg)
            return 0
        if not post(webhook, msg):
            return 1
        save_state(state_path, seen | new_keys)
        return 0

    if not new_jobs:
        print("Nothing new. No message sent.")
        if not args.dry_run and first_run:
            save_state(state_path, seen)
        return 0

    shown, extra = new_jobs[:MAX_JOBS_PER_RUN], max(0, len(new_jobs) - MAX_JOBS_PER_RUN)
    messages = build_messages(shown, extra, dashboard)

    if args.dry_run:
        for i, m in enumerate(messages, 1):
            print(f"\n----- message {i}/{len(messages)} ({len(m)} chars) -----\n{m}")
        return 0

    for m in messages:
        if not post(webhook, m):
            print("Sending failed; state NOT saved, so these jobs will be retried next run.")
            return 1
        time.sleep(1)

    save_state(state_path, seen | new_keys)
    print(f"Sent {len(shown)} job(s) in {len(messages)} message(s).")
    return 0


def save_state(path, keys):
    keys = sorted(keys)[-STATE_MAX:]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sent": keys}, f)


if __name__ == "__main__":
    sys.exit(main())
