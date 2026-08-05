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
    """Check a single Workday location string like 'New York, NY, USA' or
    'Culver City, California'. Only trusts the state-position field, so a
    town that happens to be named 'New York' (e.g. 'New York, CA, USA')
    is not mistake
