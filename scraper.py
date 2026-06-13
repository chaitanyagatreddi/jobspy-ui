"""
Job scraper for growth/marketing leadership roles — India + Global Remote.

Usage:
    python3 scraper.py                        # run all buckets
    python3 scraper.py --bucket cybersecurity # run one bucket
    python3 scraper.py --dry-run              # print queries, don't scrape
"""

import os
import csv
import time
import argparse
import requests
from datetime import datetime, timedelta
from scrapegraphai.graphs import SearchGraph

# ── Bucket definitions ────────────────────────────────────────────────────────

BUCKETS = {
    "devtools": {
        "titles": [
            "Head of Growth", "Growth PM", "Head of Marketing",
            "Product Marketing Manager",
        ],
        "keywords": ["developer tools", "devtools"],
    },
    "cybersecurity_oss": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["open source security", "cybersecurity"],
    },
    "cybersecurity_mobile": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["mobile app security", "cybersecurity"],
    },
    "cybersecurity_cloud": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["cloud security", "cybersecurity"],
    },
    "cybersecurity_webapp": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["web app security", "cybersecurity"],
    },
    "cybersecurity_compliance": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["compliance", "cybersecurity"],
    },
    "cybersecurity_grc": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
            "Head of Demand Gen", "GTM Lead",
        ],
        "keywords": ["GRC", "governance risk compliance", "cybersecurity"],
    },
    "ai_ml": {
        "titles": [
            "Head of Growth", "Growth PM", "Product Marketing Manager",
        ],
        "keywords": ["AI", "machine learning", "artificial intelligence"],
    },
    "nlp": {
        "titles": [
            "Head of Growth", "Product Marketing Manager", "Head of AI Marketing",
        ],
        "keywords": ["NLP", "natural language processing", "conversational AI"],
    },
    "deeptech": {
        "titles": [
            "Head of Growth", "Head of Marketing", "Product Marketing Manager",
        ],
        "keywords": ["deep tech", "deeptech"],
    },
    "productivity": {
        "titles": [
            "Head of Growth", "Head of Marketing",
            "Product Marketing Manager", "Head of SEO",
        ],
        "keywords": ["productivity", "workflow automation", "SaaS productivity"],
    },
    "martech": {
        "titles": [
            "Head of Growth", "Head of Performance Marketing",
            "Head of SEO", "Product Marketing Manager",
        ],
        "keywords": ["marketing technology", "martech", "adtech"],
    },
    "revops": {
        "titles": [
            "GTM Lead", "Head of RevOps", "Revenue Operations Manager",
            "Head of Growth",
        ],
        "keywords": ["revenue operations", "RevOps", "GTM"],
    },
    "content_platforms": {
        "titles": [
            "Head of Content", "Head of Growth",
            "Head of SEO", "Product Marketing Manager",
        ],
        "keywords": ["content platform", "media", "publishing"],
    },
    "founders_office": {
        "titles": [
            "Chief of Staff", "Head of Special Projects",
            "Founder's Office Lead", "Strategy and Operations Lead",
            "GTM Lead", "Head of Growth",
        ],
        "keywords": ["startup", "founder's office", "early stage"],
    },
}

LOCATIONS = ["India", "Remote", "Global Remote"]
DAYS_BACK = 15
VALID_FUNDING = {"seed", "series_a"}
FLAG_FUNDING  = {"series_b"}

GRAPH_CONFIG = {
    "llm": {
        "api_key": os.environ.get("ANTHROPIC_API_KEY"),
        "model": "anthropic/claude-haiku-4-5-20251001",
    },
    "verbose": False,
    "headless": True,
}

APOLLO_API_KEY = os.environ.get("APOLLO_API_KEY", "")

CSV_FIELDS = [
    "bucket", "title", "company", "company_domain",
    "location", "posted_date", "job_url",
    "funding_stage", "flag_series_b",
    "founder_name", "founder_email",
    "talent_name", "talent_email",
    "scraped_at",
]

# ── Search ─────────────────────────────────────────────────────────────────────

def build_prompt(title: str, keywords: list[str]) -> str:
    cutoff = (datetime.now() - timedelta(days=DAYS_BACK)).strftime("%B %d, %Y")
    kw = " OR ".join(keywords)
    locs = " OR ".join(LOCATIONS)
    return (
        f'Find job postings for "{title}" at {kw} companies in ({locs}), '
        f"posted after {cutoff}. "
        f"For each result return JSON with keys: "
        f"title, company, company_domain, location, posted_date, job_url. "
        f"Only include results where the company is clearly in the {kw} space."
    )


def scrape_jobs(bucket_name: str, bucket: dict) -> list[dict]:
    results = []
    for title in bucket["titles"]:
        prompt = build_prompt(title, bucket["keywords"])
        try:
            graph = SearchGraph(prompt=prompt, config=GRAPH_CONFIG)
            raw = graph.run()
            jobs = raw if isinstance(raw, list) else next(
                (v for v in raw.values() if isinstance(v, list)), []
            ) if isinstance(raw, dict) else []
            for job in jobs:
                job["bucket"] = bucket_name
                job.setdefault("title", title)
                results.append(job)
            print(f"  [{bucket_name}] '{title}' → {len(jobs)} results")
        except Exception as e:
            print(f"  [{bucket_name}] '{title}' → ERROR: {e}")
        time.sleep(2)
    return results


# ── Apollo enrichment ─────────────────────────────────────────────────────────

def apollo_search_people(domain: str, titles: list[str]) -> list[dict]:
    if not APOLLO_API_KEY:
        return []
    url = "https://api.apollo.io/v1/mixed_people/search"
    payload = {
        "api_key": APOLLO_API_KEY,
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "per_page": 5,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return r.json().get("people", [])
    except Exception:
        return []


def apollo_enrich_person(person_id: str) -> dict:
    if not APOLLO_API_KEY:
        return {}
    url = "https://api.apollo.io/v1/people/match"
    payload = {"api_key": APOLLO_API_KEY, "id": person_id}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        return r.json().get("person", {})
    except Exception:
        return {}


def get_contacts(domain: str) -> dict:
    """Returns founder + talent contact info for a company domain."""
    out = {
        "founder_name": "", "founder_email": "",
        "talent_name": "", "talent_email": "",
    }
    if not domain or not APOLLO_API_KEY:
        return out

    # founders
    founders = apollo_search_people(
        domain, ["founder", "co-founder", "ceo", "chief executive officer"]
    )
    if founders:
        p = apollo_enrich_person(founders[0]["id"])
        out["founder_name"] = p.get("name", "")
        out["founder_email"] = p.get("email", "")

    time.sleep(1)

    # talent
    talent = apollo_search_people(
        domain, [
            "head of talent", "head of people", "talent acquisition",
            "senior talent acquisition manager", "recruiter",
        ]
    )
    if talent:
        p = apollo_enrich_person(talent[0]["id"])
        out["talent_name"] = p.get("name", "")
        out["talent_email"] = p.get("email", "")

    return out


# ── Funding filter ─────────────────────────────────────────────────────────────

def apollo_funding_stage(domain: str) -> str:
    """Returns normalized funding stage string from Apollo org enrich."""
    if not domain or not APOLLO_API_KEY:
        return "unknown"
    url = "https://api.apollo.io/v1/organizations/enrich"
    try:
        r = requests.get(url, params={"api_key": APOLLO_API_KEY, "domain": domain}, timeout=15)
        r.raise_for_status()
        org = r.json().get("organization", {})
        stage = (org.get("latest_funding_stage") or "").lower().replace(" ", "_")
        return stage or "unknown"
    except Exception:
        return "unknown"


def funding_ok(stage: str) -> tuple[bool, bool]:
    """Returns (include, flag_series_b)."""
    if stage in VALID_FUNDING:
        return True, False
    if stage in FLAG_FUNDING:
        return True, True
    return False, False


# ── Dedup ──────────────────────────────────────────────────────────────────────

def dedupe(jobs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for j in jobs:
        key = (j.get("company", "").lower(), j.get("title", "").lower())
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


# ── Main ───────────────────────────────────────────────────────────────────────

def run(buckets_to_run: dict, dry_run: bool):
    if not GRAPH_CONFIG["llm"]["api_key"]:
        raise EnvironmentError("ANTHROPIC_API_KEY not set.")

    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    all_rows = []

    for bucket_name, bucket in buckets_to_run.items():
        print(f"\n── {bucket_name} ──")
        if dry_run:
            for t in bucket["titles"]:
                print(f"  would search: {t} | {bucket['keywords']}")
            continue

        jobs = scrape_jobs(bucket_name, bucket)
        jobs = dedupe(jobs)

        for job in jobs:
            domain = job.get("company_domain", "")
            stage = apollo_funding_stage(domain)
            include, flagged = funding_ok(stage)
            if not include:
                print(f"  skip {job.get('company')} ({stage})")
                continue

            contacts = get_contacts(domain)
            row = {
                "bucket": bucket_name,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "company_domain": domain,
                "location": job.get("location", ""),
                "posted_date": job.get("posted_date", ""),
                "job_url": job.get("job_url", ""),
                "funding_stage": stage,
                "flag_series_b": "YES" if flagged else "",
                "scraped_at": datetime.now().isoformat(timespec="seconds"),
                **contacts,
            }
            all_rows.append(row)
            print(f"  ✓ {row['company']} | {row['title']} | {stage}")
            time.sleep(1)

        # save per-bucket CSV
        bucket_file = f"jobs_{bucket_name}_{date_str}.csv"
        with open(bucket_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            bucket_rows = [r for r in all_rows if r["bucket"] == bucket_name]
            writer.writerows(bucket_rows)
        print(f"  saved → {bucket_file}")

    # combined CSV
    if not dry_run and all_rows:
        combined = f"jobs_ALL_{date_str}.csv"
        with open(combined, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nCombined → {combined} ({len(all_rows)} jobs)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", help=f"Run one bucket. Options: {', '.join(BUCKETS)}")
    parser.add_argument("--dry-run", action="store_true", help="Print queries without scraping")
    args = parser.parse_args()

    buckets_to_run = (
        {args.bucket: BUCKETS[args.bucket]} if args.bucket and args.bucket in BUCKETS
        else BUCKETS
    )
    run(buckets_to_run, args.dry_run)
