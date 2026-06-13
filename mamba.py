"""
Mamba Labs GTM Suite — thin Python client over Apify REST.

Wraps 6 Apify actors that detect hiring signals, tech stack, and ICP fit.
No Node MCP server needed — calls Apify's run-sync-get-dataset-items endpoint
directly so each tool returns flat JSON in one HTTP round-trip.

Docs: https://apify.com/mambalabs
"""
import os
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import Any


APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
BASE = "https://api.apify.com/v2"


# Actor IDs published by mambalabs. Names follow the GitHub README.
ACTORS = {
    "hiring_signals":   "mambalabs/gtm-hiring-signal-scraper",
    "tech_stack":       "mambalabs/gtm-tech-stack-signal-scraper",
    "aggregate":        "mambalabs/gtm-signals-aggregator",
    "job_board":        "mambalabs/job-board-keyword-signal-scanner",
    "linkedin_resolve": "mambalabs/domain-to-linkedin-url-resolver",
    "icp_fit":          "mambalabs/icp-fit-scorer",
}


class MambaError(Exception):
    pass


def _run_actor_sync(actor_id: str, payload: dict, timeout: int = 90) -> Any:
    """Call Apify run-sync-get-dataset-items. Returns the dataset (list[dict])."""
    if not APIFY_TOKEN:
        raise MambaError("APIFY_TOKEN missing from env")
    # Apify uses tilde to separate org and actor name
    actor_slug = actor_id.replace("/", "~")
    url = f"{BASE}/acts/{actor_slug}/run-sync-get-dataset-items"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {APIFY_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300]
        raise MambaError(f"Mamba/Apify HTTP {e.code}: {body!r}")
    except Exception as e:
        raise MambaError(f"Mamba request failed: {e}")


# ---- Tool wrappers --------------------------------------------------------

def hiring_signals(domain: str, role_filter: str | None = None) -> list:
    payload = {"domain": domain}
    if role_filter:
        payload["role_filter"] = role_filter
    return _run_actor_sync(ACTORS["hiring_signals"], payload)


def tech_stack(domain: str, crawl_extra: bool = False) -> list:
    return _run_actor_sync(
        ACTORS["tech_stack"],
        {"domain": domain, "crawl_additional_pages": crawl_extra},
    )


def aggregate_signals(domain: str, include_summary: bool = True) -> list:
    """Composite hiring + tech-stack score in one call."""
    return _run_actor_sync(
        ACTORS["aggregate"],
        {"company_domain": domain, "include_summary": include_summary, "explain_mode": True},
    )


def icp_fit(domain: str, icp_description: str, template: str = "b2b_saas") -> list:
    return _run_actor_sync(
        ACTORS["icp_fit"],
        {
            "company_domain": domain,
            "template": template,
            "icp_description": icp_description,
            "fetch_signals": True,
            "include_explanation": True,
        },
    )


def resolve_linkedin(domain: str | None = None, name: str | None = None) -> list:
    if not domain and not name:
        raise MambaError("resolve_linkedin needs domain or name")
    payload = {}
    if domain:
        payload["company_domain"] = domain
    if name:
        payload["company_name"] = name
    return _run_actor_sync(ACTORS["linkedin_resolve"], payload)
