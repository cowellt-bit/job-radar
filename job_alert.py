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
]

ASHBY_COMPANIES = [
    {"name": "ElevenLabs", "slug": "elevenlabs"},
]


def title_matches(title: str) -> bool:
    lowered = title.lower()
    return any(pattern.search(lowered) for pattern in KEYWORD_PATTERNS)


def workday_location_text_matches(location_text: str | None) -> bool:
    # Checks a single Workday location string, e.g. New York, NY, USA or
    # Culver City, California. Only trusts the state-position field, so a
    # town that happens to be named New York (e.g. New York, CA, USA) is
    # not mistaken for New York state.
    if not location_text:
        return False
    lowered = location_text.lower()
    if "remote" in lowered:
        return True
    parts = [p.strip() for p in location_text.split(",")]
    if len(parts) == 3:
        # "City, ST, Country" - e.g. Disney's format
        state_part = parts[1]
        return state_part.upper() in TARGET_STATE_CODES
    if len(parts) == 2:
        # "City, State" (full name, US-only) or "City, Country"
        state_part = parts[1].strip().lower()
        return state_part in TARGET_STATE_NAMES
    return False


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
    if location_text and location_text.split()[0].isdigit() and "location" in location_text.lower():
        # e.g. "2 Locations" - no state shown, need the detail page
        return resolve_ambiguous_workday_location(job["_company"], job["_external_path"])
    return workday_location_text_matches(location_text)


def ashby_job_location_matches(job: dict) -> bool:
    raw = job["_raw"]
    if raw.get("workplaceType") == "Remote" or raw.get("isRemote"):
        return True
    regions = []
    addr = (raw.get("address") or {}).get("postalAddress") or {}
    if addr.get("addressRegion"):
        regions.append(addr["addressRegion"])
    for secondary in raw.get("secondaryLocations") or []:
        sec_addr
