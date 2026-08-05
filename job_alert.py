"""
Job alert agent: polls each company's ATS API, filters titles against
keyword list, and emails on new matches. Already-seen jobs are tracked
in seen_jobs.json so the same posting is never emailed twice.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

import requests

SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"

KEYWORDS = [
    # Tier 1
    "creative director", "head of creative", "head of brand",
    "executive creative director", "vp of creative", "vp", "creative",
    "director of brand",
    # Tier 2
    "principal narrative", "principal narrative designer",
    "head of narrative", "narrative lead", "creative director", "product",
    "brand systems lead", "ai creative lead", "gen ai creative",
    # Tier 3
    "creative director", "business marketing", "head of content",
    "director of content strategy", "editorial director",
]
KEYWORDS = sorted(set(k.lower() for k in KEYWORDS))
KEYWORD_PATTERNS = [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in KEYWORDS]

# Only alert for jobs in these US states, or fully remote roles.
TARGET_STATE_NAMES = {"new york", "pennsylvania"}
TARGET_STATE_CODES = {"NY", "PA"}

WORKDAY_COMPANIES = [
    {
        "name": "Sony Pictures",
        "host": "spe.wd1.myworkdayjobs.com",
        "tenant": "spe",
        "site": "SonyPicturesEntertainment",
    },
    {
        "name": "Disney",
        "host": "disney.wd5.myworkdayjobs.com",
        "tenant": "disney",
        "site": "disneycareer",
    },
    {
        "name": "Netflix",
        "host": "netflix.wd1.myworkdayjobs.com",
        "tenant": "netflix",
        "site": "Netflix",
    },
    {
        "name": "Conde Nast",
        "host": "condenast.wd5.myworkdayjobs.com",
        "tenant": "condenast",
        "site": "CondeCareers",
    },
    {
        "name": "Warner Bros Discovery",
        "host": "warnerbros.wd5.myworkdayjobs.com",
        "tenant": "warnerbros",
        "site": "global",
    },
]

ASHBY_COMPANIES = [
    {"name": "ElevenLabs", "slug": "elevenlabs"},
]


def title_matches(title: str) -> bool:
    lowered = title.lower()
    return any(pattern.search(lowered) for pattern in KEYWORD_PATTERNS)


def workday_location_text_matches(location_text: str | None, trust_remote_substring: bool = True) -> bool:
    # Checks a single Workday location string. Handles a few formats seen
    # in practice: "New York, NY, USA" (Disney), "Culver City, California"
    # (Sony), and "NY New York 230 Park Avenue South" (Warner Bros
    # Discovery - state code leads, no commas). Only trusts the
    # state-position field, so a town that happens to be named New York
    # (e.g. New York, CA, USA) is not mistaken for New York state.
    #
    # trust_remote_substring controls whether the word "remote" appearing
    # in the text counts as a remote match. Some companies (e.g. WBD) use
    # "Remote" as part of a literal office name even for Hybrid roles, so
    # when a company gives us an explicit remoteType field, that field is
    # used instead and this text-based guess is turned off.
    if not location_text:
        return False
    lowered = location_text.lower()
    if trust_remote_substring and "remote" in lowered:
        return True
    parts = [p.strip() for p in location_text.split(",")]
    if len(parts) == 3:
        # "City, ST, Country" - e.g. Disney's format
        state_part = parts[1]
        if state_part.upper() in TARGET_STATE_CODES:
            return True
    elif len(parts) == 2:
        # "City, State" (full name, US-only) or "City, Country"
        state_part = parts[1].strip().lower()
        if state_part in TARGET_STATE_NAMES:
            return True
    # "ST City Address" - no commas, state code is the first word
    first_word = location_text.split()[0].strip(",").upper()
    return first_word in TARGET_STATE_CODES


def resolve_ambiguous_workday_location(company: dict, external_path: str) -> bool:
    # For postings listed as "N Locations" with no state shown, fetch the
    # job detail page to get the real location list and remote status.
    url = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}{external_path}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        info = resp.json()["jobPostingInfo"]
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"[warn] Could not resolve locations for {company['name']} job {external_path}: {e}")
        return False

    remote_type = (info.get("remoteType") or "").lower()
    if "remote" in remote_type:
        return True

    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    return any(workday_location_text_matches(loc) for loc in locations)


def workday_job_location_matches(job: dict) -> bool:
    location_text = job["_raw_location_text"]
    remote_type = job.get("_remote_type")

    if remote_type:
        if "remote" in remote_type.lower():
            return True
        # An explicit non-remote type (e.g. "Onsite"/"Hybrid") means we
        # should not trust the word "remote" showing up in location text.
        trust_remote_substring = False
    else:
        trust_remote_substring = True

    if location_text and location_text.split()[0].isdigit() and "location" in location_text.lower():
        # e.g. "2 Locations" - no state shown, need the detail page
        return resolve_ambiguous_workday_location(job["_company"], job["_external_path"])
    return workday_location_text_matches(location_text, trust_remote_substring)


def ashby_job_location_matches(job: dict) -> bool:
    raw = job["_raw"]
    if raw.get("workplaceType") == "Remote" or raw.get("isRemote"):
        return True
    regions = []
    addr = (raw.get("address") or {}).get("postalAddress") or {}
    if addr.get("addressRegion"):
        regions.append(addr["addressRegion"])
    for secondary in raw.get("secondaryLocations") or []:
        sec_addr = (secondary.get("address") or {}).get("postalAddress") or {}
        if sec_addr.get("addressRegion"):
            regions.append(sec_addr["addressRegion"])
    return any(r.strip().lower() in TARGET_STATE_NAMES for r in regions)


def job_location_matches(job: dict) -> bool:
    if job["source"] == "workday":
        return workday_job_location_matches(job)
    return ashby_job_location_matches(job)


def fetch_workday_jobs(company: dict) -> list[dict]:
    url = f"https://{company['host']}/wday/cxs/{company['tenant']}/{company['site']}/jobs"
    jobs = []
    offset = 0
    limit = 20
    try:
        while True:
            resp = requests.post(
                url,
                json={"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            postings = data.get("jobPostings", [])
            if not postings:
                break
            for p in postings:
                if "externalPath" not in p or "title" not in p:
                    print(f"[warn] Skipping malformed {company['name']} posting: {p}")
                    continue
                jobs.append({
                    "id": f"workday:{company['tenant']}:{p['externalPath']}",
                    "title": p["title"],
                    "url": f"https://{company['host']}/{company['site']}{p['externalPath']}",
                    "company": company["name"],
                    "source": "workday",
                    "_raw_location_text": p.get("locationsText"),
                    "_remote_type": p.get("remoteType"),
                    "_external_path": p["externalPath"],
                    "_company": company,
                })
            offset += limit
            if offset >= data.get("total", 0):
                break
    except requests.RequestException as e:
        print(f"[warn] Could not fetch {company['name']} (Workday): {e}")
    return jobs


def fetch_ashby_jobs(company: dict) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company['slug']}"
    jobs = []
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("jobs", []):
            jobs.append({
                "id": f"ashby:{company['slug']}:{p['id']}",
                "title": p["title"],
                "url": p["jobUrl"],
                "company": company["name"],
                "source": "ashby",
                "_raw": p,
            })
    except requests.RequestException as e:
        print(f"[warn] Could not fetch {company['name']} (Ashby): {e}")
    return jobs


def fetch_all_jobs() -> list[dict]:
    jobs = []
    for company in WORKDAY_COMPANIES:
        jobs.extend(fetch_workday_jobs(company))
    for company in ASHBY_COMPANIES:
        jobs.extend(fetch_ashby_jobs(company))
    return jobs


def load_seen_ids() -> set[str]:
    if not SEEN_JOBS_FILE.exists():
        return set()
    return set(json.loads(SEEN_JOBS_FILE.read_text()))


def save_seen_ids(seen_ids: set[str]) -> None:
    SEEN_JOBS_FILE.write_text(json.dumps(sorted(seen_ids), indent=2))


def send_email(job: dict) -> None:
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_address = os.environ.get("ALERT_EMAIL", gmail_address)

    if not gmail_address or not gmail_app_password:
        print(f"[dry-run] Would email: {job['title']} at {job['company']} -> {job['url']}")
        return

    subject = f"New job match: {job['title']} at {job['company']}"
    body = (
        f"Title: {job['title']}\n"
        f"Company: {job['company']}\n"
        f"Apply: {job['url']}\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_address

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)
    print(f"[sent] {job['title']} at {job['company']}")


def main() -> None:
    seen_ids = load_seen_ids()
    first_run = not SEEN_JOBS_FILE.exists()

    all_jobs = fetch_all_jobs()
    print(f"Fetched {len(all_jobs)} total postings across all companies.")

    title_matches_list = [j for j in all_jobs if title_matches(j["title"])]
    print(f"{len(title_matches_list)} postings match your keywords.")

    matches = [j for j in title_matches_list if job_location_matches(j)]
    print(f"{len(matches)} of those are in NY, PA, or fully remote.")

    new_matches = [j for j in matches if j["id"] not in seen_ids]

    if first_run:
        print(
            f"First run: recording {len(new_matches)} existing matches as "
            "already-seen without emailing them. Future runs will only "
            "email brand-new postings."
        )
        for job in new_matches:
            seen_ids.add(job["id"])
    else:
        print(f"{len(new_matches)} are new since last run.")
        for job in new_matches:
            send_email(job)
            seen_ids.add(job["id"])

    save_seen_ids(seen_ids)


if __name__ == "__main__":
    main()
