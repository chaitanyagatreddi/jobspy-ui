#!/usr/bin/env python3
"""
Job Spy UI — Flask + JobSpy + Claude scoring.

One HTML page:
  - input keyword, location, hours_old, sites
  - POST /scrape calls jobspy.scrape_jobs, returns JSON
  - Inline table of results with title, company, salary, apply link
  - Optional "Score against profile" button per row uses scorer.py
"""
import os
import sys
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, Response

# ---------------------------------------------------------------------------
# Daily LinkedIn-hit cap (resets midnight UTC)
# Counts each /scrape (1 hit) and each /score that lazy-fetches a JD (1 hit).
# In-memory only — resets on service restart. Fine for free Render tier.
# ---------------------------------------------------------------------------
LINKEDIN_DAILY_CAP = int(os.environ.get("LINKEDIN_DAILY_CAP", "40"))
_cap_lock = threading.Lock()
_cap_state = {"day": "", "hits": 0}


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check_and_reserve_hit() -> tuple[bool, int]:
    """Try to reserve 1 LinkedIn hit. Returns (allowed, hits_used_after_reserve)."""
    with _cap_lock:
        today = _today_utc()
        if _cap_state["day"] != today:
            _cap_state["day"] = today
            _cap_state["hits"] = 0
        if _cap_state["hits"] >= LINKEDIN_DAILY_CAP:
            return (False, _cap_state["hits"])
        _cap_state["hits"] += 1
        return (True, _cap_state["hits"])


def _cap_status() -> dict:
    with _cap_lock:
        return {
            "day": _cap_state["day"] or _today_utc(),
            "used": _cap_state["hits"],
            "cap": LINKEDIN_DAILY_CAP,
            "remaining": max(0, LINKEDIN_DAILY_CAP - _cap_state["hits"]),
        }

try:
    from jobspy import scrape_jobs
except ImportError:
    sys.exit("pip install python-jobspy")

sys.path.insert(0, str(Path(__file__).parent))
try:
    from profile_local import PROFILE  # personal profile, gitignored
except ImportError:
    from profile import PROFILE  # public template fallback
from scorer import score_jd

# Lazy LinkedIn JD fetcher (uses jobspy internals)
import re
import urllib.request
import urllib.parse
_li_scraper = None
_li_company_id_cache: dict[str, str] = {}


def _resolve_linkedin_company_id(name: str) -> str:
    """Resolve a company name (or LinkedIn slug) → numeric LinkedIn company ID.

    Strategy: hit `linkedin.com/company/<slug>` and parse the numeric ID from the HTML
    (urn:li:fsd_company:<id> or "companyId":<id>). Returns '' on failure.
    """
    if not name:
        return ""
    key = name.strip().lower()
    if key in _li_company_id_cache:
        return _li_company_id_cache[key]
    # Slugify: lowercase, spaces → hyphens, strip junk
    slug = re.sub(r"[^a-z0-9\-]", "", key.replace(" ", "-").replace("_", "-"))
    url = f"https://www.linkedin.com/company/{slug}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            html = r.read().decode(errors="ignore")
    except Exception as e:
        print(f"[resolve_li_id] {name}: {e}")
        return ""

    # Try multiple patterns LinkedIn uses
    patterns = [
        r"urn:li:fsd_company:(\d+)",
        r"urn:li:company:(\d+)",
        r'"companyId"\s*:\s*"?(\d+)"?',
        r"f_C=(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            cid = m.group(1)
            _li_company_id_cache[key] = cid
            return cid

    _li_company_id_cache[key] = ""  # negative-cache so we don't retry hopeless slugs
    return ""

def _fetch_linkedin_jd(job_url: str) -> str:
    """Fetch the full JD description for a LinkedIn job URL. Returns '' on failure."""
    global _li_scraper
    m = re.search(r"/jobs/view/(\d+)", job_url or "")
    if not m:
        return ""
    job_id = m.group(1)
    try:
        if _li_scraper is None:
            from jobspy.linkedin import LinkedIn
            from jobspy.model import ScraperInput, Site, DescriptionFormat
            _li_scraper = LinkedIn()
            # Minimal scraper_input so DescriptionFormat reference works
            _li_scraper.scraper_input = ScraperInput(
                site_type=[Site.LINKEDIN], search_term="dummy",
                description_format=DescriptionFormat.MARKDOWN,
            )
        details = _li_scraper._get_job_details(job_id)
        return (details.get("description") or "").strip()
    except Exception as e:
        print(f"[fetch_linkedin_jd] {e}")
        return ""

app = Flask(__name__)


def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.route("/scrape", methods=["POST"])
def scrape():
    body = request.get_json(force=True, silent=True) or {}
    search_term = (body.get("search_term") or "").strip()
    location    = (body.get("location") or "Remote").strip()
    hours_old   = int(body.get("hours_old") or 72)
    sites       = body.get("sites") or ["indeed", "linkedin", "google"]
    results_wanted = int(body.get("results_wanted") or 25)
    company_filter = (body.get("company") or "").strip()
    linkedin_company_id = (body.get("linkedin_company_id") or "").strip()

    # Auto-resolve company name → LinkedIn ID if no manual ID given
    resolved_from_name = False
    if company_filter and not linkedin_company_id:
        resolved = _resolve_linkedin_company_id(company_filter)
        if resolved:
            linkedin_company_id = resolved
            resolved_from_name = True

    if not search_term and not linkedin_company_id and not company_filter:
        return jsonify({"error": "search_term, Company name, or LinkedIn Company ID required"}), 400
    # If only company ID/name provided, use a permissive default so JobSpy still runs
    if not search_term:
        search_term = "*"

    # Reserve a LinkedIn daily-hit budget slot (if LinkedIn is in the sites list)
    if "linkedin" in sites:
        allowed, used = _check_and_reserve_hit()
        if not allowed:
            return jsonify({
                "error": f"Daily LinkedIn hit cap reached ({LINKEDIN_DAILY_CAP}). Resets midnight UTC.",
                "cap": _cap_status(),
            }), 429

    # Company filter is applied post-scrape, NOT appended to search_term
    # (LinkedIn returns 0 results when company name is in the query)
    if company_filter and not linkedin_company_id:
        # Widen the result pool so the post-filter has more to match against
        results_wanted = max(results_wanted, 100)

    # Normalize common Indian city typos in the location string BEFORE handing to JobSpy
    _TYPO_TO_CITY = {
        "hyderbad": "Hyderabad", "hydrabad": "Hyderabad", "hydrebad": "Hyderabad",
        "banglore": "Bangalore", "bengluru": "Bengaluru",
        "gurgoan": "Gurgaon", "gurugaon": "Gurugram",
    }
    for typo, canon in _TYPO_TO_CITY.items():
        if typo in location.lower():
            location = location.lower().replace(typo, canon.lower()).title()
            break

    # Auto-detect country from location to avoid Indeed misrouting Indian cities to US
    INDIAN_CITIES = {"bangalore", "bengaluru", "hyderabad", "mumbai", "delhi", "noida",
                     "gurgaon", "gurugram", "pune", "chennai", "kolkata", "ahmedabad",
                     "jaipur", "kochi", "thiruvananthapuram", "indore"}
    loc_l = location.lower().strip()
    if body.get("country_indeed"):
        country = body["country_indeed"]
    elif "india" in loc_l or any(city in loc_l for city in INDIAN_CITIES):
        country = "India"
    elif "remote" in loc_l or "anywhere" in loc_l:
        country = "USA"  # default for global remote; Indeed needs SOME country
    else:
        country = "USA"

    google_search_term = body.get("google_search_term") or f"{search_term} jobs in {location} posted in last {hours_old} hours"

    scrape_kwargs = dict(
        site_name=sites,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        results_wanted=results_wanted,
        hours_old=hours_old,
        country_indeed=country,
        linkedin_fetch_description=False,
    )
    if linkedin_company_id and linkedin_company_id.isdigit():
        scrape_kwargs["linkedin_company_ids"] = [int(linkedin_company_id)]

    site_errors: dict[str, str] = {}
    df = None
    try:
        df = scrape_jobs(**scrape_kwargs)
    except Exception as e:
        # The batch call failed (e.g. one site like Google returns 429).
        # Retry each site in isolation and merge what survives.
        import pandas as pd
        frames = []
        for site in sites:
            try:
                solo_kwargs = {**scrape_kwargs, "site_name": [site]}
                solo_df = scrape_jobs(**solo_kwargs)
                if solo_df is not None and not solo_df.empty:
                    frames.append(solo_df)
            except Exception as sub_e:
                msg = str(sub_e)
                # Trim the verbose Google /sorry URL noise
                if "/sorry/index" in msg:
                    msg = "rate-limited by Google (shared IP — try LinkedIn/Indeed)"
                site_errors[site] = msg[:240]
        if frames:
            df = pd.concat(frames, ignore_index=True)
        else:
            return jsonify({
                "error": "all selected sites failed",
                "site_errors": site_errors,
            }), 502

    # DataFrame → JSON-safe list of dicts + dedupe + location filter
    seen = set()
    jobs = []
    # Build a location filter: keep only jobs whose location matches the search city,
    # OR is broadly "Remote/India". Skip if user searched a generic location.
    user_city = loc_l.replace(",", " ").split()[0] if loc_l else ""
    GENERIC_LOC = {"remote", "anywhere", "india", "usa", "global", "world"}
    # Location filter respects what the user typed. Leave blank for no filter.
    # Common typos → canonical spelling
    CITY_TYPOS = {
        "hyderbad": "hyderabad", "hydrabad": "hyderabad", "hydrebad": "hyderabad",
        "banglore": "bangalore", "bengluru": "bengaluru",
        "gurgoan": "gurgaon", "gurugaon": "gurugram",
    }
    user_city = CITY_TYPOS.get(user_city, user_city)
    do_filter = bool(user_city) and user_city not in GENERIC_LOC
    INDIAN_CITY_VARIANTS = {"bangalore": ["bangalore", "bengaluru"],
                            "bengaluru": ["bangalore", "bengaluru"],
                            "gurgaon": ["gurgaon", "gurugram"],
                            "gurugram": ["gurgaon", "gurugram"]}
    # Only accept the city itself + "remote" — DO NOT fall back to "india" or it leaks every Indian job
    accepted_terms = INDIAN_CITY_VARIANTS.get(user_city, [user_city]) + ["remote", "anywhere"]

    filtered_out = 0
    for _, row in df.iterrows():
        job = {k: _clean(v) for k, v in row.items()}
        key = ((job.get("company") or "").strip().lower(), (job.get("title") or "").strip().lower())
        if key in seen or not key[0]:
            continue
        if do_filter:
            jl = (job.get("location") or "").lower() + " " + (job.get("city") or "").lower()
            if not any(t in jl for t in accepted_terms):
                filtered_out += 1
                continue
        # Company filter (case-insensitive substring match)
        if company_filter:
            jc = (job.get("company") or "").lower()
            if company_filter.lower() not in jc:
                filtered_out += 1
                continue
        seen.add(key)
        jobs.append(job)

    return jsonify({
        "count": len(jobs),
        "jobs": jobs,
        "filtered_out": filtered_out,
        "resolved_linkedin_company_id": linkedin_company_id if resolved_from_name else "",
        "cap": _cap_status(),
        "site_errors": site_errors,
    })


@app.route("/limits")
def limits():
    return jsonify(_cap_status())


@app.route("/fetch-jd", methods=["POST"])
def fetch_jd():
    """Fetch full JD description without scoring. Respects daily LinkedIn cap."""
    body = request.get_json(force=True, silent=True) or {}
    job_url = (body.get("job_url") or "").strip()
    if "linkedin.com" not in job_url:
        return jsonify({"error": "linkedin job_url required"}), 400
    allowed, _ = _check_and_reserve_hit()
    if not allowed:
        return jsonify({
            "error": f"Daily LinkedIn hit cap reached ({LINKEDIN_DAILY_CAP}). Resets midnight UTC.",
            "cap": _cap_status(),
        }), 429
    desc = _fetch_linkedin_jd(job_url)
    return jsonify({"description": desc, "chars": len(desc), "cap": _cap_status()})


# ---- Email capture (lead gate) ---------------------------------------------
_CAPTURES_PATH = Path(__file__).parent / "captures.csv"
_capture_lock = threading.Lock()

@app.route("/capture-email", methods=["POST"])
def capture_email():
    body = request.get_json(force=True, silent=True) or {}
    email = (body.get("email") or "").strip()
    company = (body.get("company") or "").strip()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "valid email required"}), 400
    if not company:
        return jsonify({"error": "company required"}), 400
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    ua = request.headers.get("User-Agent", "")[:200]
    ts = datetime.now(timezone.utc).isoformat()
    row = [ts, email, company, ip, ua]
    with _capture_lock:
        new_file = not _CAPTURES_PATH.exists()
        with _CAPTURES_PATH.open("a", encoding="utf-8") as fh:
            if new_file:
                fh.write("timestamp,email,company,ip,user_agent\n")
            # Trivial CSV escape
            esc = lambda s: '"' + str(s).replace('"', '""') + '"'
            fh.write(",".join(esc(c) for c in row) + "\n")
    return jsonify({"ok": True})


@app.route("/score", methods=["POST"])
def score():
    """Score a single job against the profile. Lazy-fetches LinkedIn JD if missing."""
    body = request.get_json(force=True, silent=True) or {}
    title = (body.get("title") or "").strip()
    company = (body.get("company") or "").strip()
    location = (body.get("location") or "").strip()
    description = (body.get("description") or "").strip()
    job_url = (body.get("job_url") or "").strip()
    if not title and not description:
        return jsonify({"error": "title or description required"}), 400

    # Hard-stop scoring once the LinkedIn daily cap is exhausted
    cap = _cap_status()
    if cap["remaining"] <= 0:
        return jsonify({
            "error": f"Daily LinkedIn hit cap reached ({LINKEDIN_DAILY_CAP}). Resets midnight UTC.",
            "cap": cap,
        }), 429

    # Lazy-fetch full JD from LinkedIn if not already provided (consumes 1 hit)
    jd_source = "title-only"
    if not description and "linkedin.com" in job_url:
        allowed, _ = _check_and_reserve_hit()
        if allowed:
            description = _fetch_linkedin_jd(job_url)
            if description:
                jd_source = "fetched"

    jd_text = f"Title: {title}\nCompany: {company}\nLocation: {location}\n\n{description or '(No description available — score from title + company + location only.)'}"
    try:
        result = score_jd(jd=jd_text, profile=PROFILE, answers=[], company=company, bucket="")
        result["_jd_source"] = jd_source
        result["_jd_chars"] = len(description)
        result["_jd_text"] = description  # so UI can show full JD inline
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"scoring failed: {e}"}), 500


@app.route("/profile")
def profile():
    return jsonify({
        "name": PROFILE["name"],
        "current_focus": PROFILE["current_focus"],
        "target_stages": PROFILE["target_stages"],
    })


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>JobSpy · Recent Roles</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg:#010101; --card:#09070D; --accent:#625DF6; --teal:#50E3C2; --muted:#888;
  }
  * { box-sizing:border-box; }
  html,body { margin:0; background:var(--bg); color:#fff; font-family:Inter,system-ui,sans-serif; min-height:100vh; }
  body {
    background-image: radial-gradient(circle at 1px 1px, rgba(98,93,246,0.18) 1px, transparent 0);
    background-size: 24px 24px; background-attachment: fixed;
  }
  nav {
    position:sticky; top:0; z-index:50; display:flex; justify-content:space-between; align-items:center;
    padding:18px 32px; border-bottom:1px solid rgba(255,255,255,0.1);
    backdrop-filter: blur(12px); background:rgba(1,1,1,0.85);
  }
  nav .brand { font-weight:600; letter-spacing:-0.01em; }
  nav .brand span { color:var(--teal); }
  nav a { color:#fff; text-decoration:none; padding:6px 12px; border-radius:8px; font-size:14px; border:1px solid rgba(255,255,255,0.1); }

  .hero { max-width:980px; margin:80px auto 24px; padding:0 24px; text-align:center; }
  .hero h1 { font-weight:600; letter-spacing:-0.02em; font-size:clamp(36px,5vw,64px); line-height:1.05; margin:0; }
  .hero h1 span { color:var(--teal); }
  .hero p { color:#a0a0a0; margin-top:18px; font-size:17px; }

  .form { max-width:980px; margin:32px auto; padding:0 24px; }
  .form-row { display:grid; grid-template-columns:2fr 1fr 1.5fr 1fr 1fr auto; gap:10px; align-items:end; }
  @media (max-width: 1100px) { .form-row { grid-template-columns:1fr 1fr; } }
  label { display:block; font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; margin-bottom:6px; }
  input, select {
    width:100%; padding:13px 14px; border-radius:10px;
    background:var(--card); border:1px solid rgba(255,255,255,0.1); color:#fff;
    font:14px/1 'JetBrains Mono', ui-monospace, monospace; outline:none;
  }
  input:focus, select:focus { border-color:var(--accent); }
  button.primary {
    padding:13px 22px; border-radius:10px; border:none;
    background:var(--accent); color:#fff; font-weight:600; cursor:pointer; font-size:14px;
    box-shadow: 0 8px 24px rgba(98,93,246,0.3);
  }
  button.primary:disabled { opacity:0.5; cursor:not-allowed; }
  .chips { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; justify-content:center; font-size:13px; color:var(--muted); }
  .chip { padding:6px 12px; border-radius:999px; border:1px solid rgba(255,255,255,0.1); cursor:pointer; user-select:none; }
  .chip.active { border-color:var(--teal); color:var(--teal); background:rgba(80,227,194,0.08); }

  .results { max-width:1200px; margin:32px auto; padding:0 24px; }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); border:1px solid rgba(255,255,255,0.06); border-radius:14px; overflow:hidden; }
  th { text-align:left; padding:14px 16px; font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:0.05em; background:rgba(255,255,255,0.02); border-bottom:1px solid rgba(255,255,255,0.06); }
  td { padding:14px 16px; border-bottom:1px solid rgba(255,255,255,0.04); vertical-align:top; }
  tr:last-child td { border-bottom:none; }
  tr:hover td { background:rgba(98,93,246,0.04); }
  td a { color:var(--teal); text-decoration:none; }
  td a:hover { text-decoration:underline; }
  .site-badge { font-size:11px; padding:2px 8px; border-radius:999px; background:rgba(98,93,246,0.15); color:var(--accent); text-transform:capitalize; }
  .score-btn { padding:6px 12px; border-radius:6px; border:1px solid var(--accent); background:transparent; color:var(--accent); cursor:pointer; font-size:12px; font-family:'JetBrains Mono', monospace; }
  .score-btn:hover { background:rgba(98,93,246,0.1); }
  .score-btn:disabled { opacity:0.5; cursor:wait; }
  .status { text-align:center; color:var(--muted); padding:40px; font-size:14px; }
  .error { background:rgba(220,60,60,0.1); border:1px solid rgba(220,60,60,0.3); color:#ff9b9b; padding:14px 18px; border-radius:10px; margin:20px 0; font-size:14px; }
  footer { text-align:center; color:var(--muted); font-size:13px; padding:40px 0; }
</style>
</head>
<body>

<!-- Email gate overlay (velt-style, copy ported from Gitradar) -->
<div id="emailGate" style="display:none;position:fixed;inset:0;background:rgba(1,1,1,0.92);backdrop-filter:blur(8px);z-index:200;align-items:center;justify-content:center;padding:24px;">
  <div style="background:#09070D;border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:36px;max-width:440px;width:100%;text-align:center;box-shadow:0 30px 80px rgba(0,0,0,0.6);">
    <div style="font-size:32px;margin-bottom:12px">🔎</div>
    <h2 style="font-size:24px;font-weight:600;color:#fff;margin:0 0 10px;letter-spacing:-0.015em">job<span style="color:#50E3C2">.spy</span></h2>
    <p style="color:#a0a0a0;font-size:14px;line-height:1.6;margin:0 0 24px">
      Used by job seekers, recruiters, and operators to find recent roles across LinkedIn, Indeed, Google &amp; Glassdoor — scored against your profile with a click.
    </p>
    <input type="email" id="gateEmail" placeholder="Work email" autocomplete="email" style="width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;background:#010101;border:1px solid rgba(255,255,255,0.1);color:#fff;font:14px 'JetBrains Mono',monospace;margin-bottom:10px;outline:none;" />
    <input type="text" id="gateCompany" placeholder="Company" autocomplete="organization" style="width:100%;box-sizing:border-box;padding:12px 14px;border-radius:10px;background:#010101;border:1px solid rgba(255,255,255,0.1);color:#fff;font:14px 'JetBrains Mono',monospace;margin-bottom:16px;outline:none;" />
    <button id="gateSubmit" style="width:100%;padding:13px;border-radius:10px;border:none;background:#625DF6;color:#fff;font-weight:600;font-size:14px;cursor:pointer;box-shadow:0 8px 24px rgba(98,93,246,0.3);">Get Access →</button>
    <p id="gateError" style="color:#ff9b9b;font-size:12px;margin-top:10px;display:none">Please enter a valid email and company.</p>
    <p style="color:#666;font-size:11px;margin-top:14px;line-height:1.5">No spam. Just helps us understand who's using the tool.</p>
  </div>
</div>

<!-- Wake notice (Render free tier may need 30-60s on first hit) -->
<div id="wakeNotice" style="display:none;background:rgba(255,160,90,0.08);border-bottom:1px solid rgba(255,160,90,0.3);padding:10px 24px;text-align:center;font-size:13px;color:#ffc28a;position:sticky;top:0;z-index:55;">
  ⏳ This space runs on a free server — first request after idle may take <strong>30–60 seconds</strong> to wake up.
  <button onclick="document.getElementById('wakeNotice').style.display='none'" style="margin-left:14px;background:none;border:none;color:#ffc28a;cursor:pointer;font-size:16px;line-height:1;">×</button>
</div>

<div id="capBanner" style="display:none;background:linear-gradient(90deg,rgba(255,160,90,0.15),rgba(98,93,246,0.12));border-bottom:1px solid rgba(255,160,90,0.4);color:#ffd9b8;padding:12px 24px;text-align:center;font-size:13px;font-weight:500;position:sticky;top:0;z-index:60;">
  <span id="capBannerText">Daily search cap reached — please come back tomorrow.</span>
  <span id="capBannerReset" style="color:#a0a0a0;font-weight:400;margin-left:8px"></span>
</div>
<nav>
  <div class="brand">job<span>.spy</span> <span id="modeBadge" style="font-size:11px;color:var(--teal);background:rgba(80,227,194,0.1);padding:3px 8px;border-radius:999px;margin-left:8px;font-weight:500">personalized</span></div>
  <div style="display:flex;align-items:center;gap:14px;">
    <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#a0a0a0;cursor:pointer;user-select:none;">
      <input type="checkbox" id="scoreToggle" style="accent-color:var(--accent);width:16px;height:16px;cursor:pointer;" checked />
      Score against my profile
    </label>
    <a href="https://github.com/speedyapply/JobSpy" target="_blank">JobSpy</a>
  </div>
</nav>

<section class="hero">
  <h1>Recent roles, <span>scored</span> against your profile.</h1>
  <p>Scrape LinkedIn, Indeed, Glassdoor concurrently. Filter by age. Rank by fit.</p>
</section>

<section class="form">
  <div class="form-row">
    <div>
      <label>Search term</label>
      <input id="q" placeholder="Head of Growth, GTM Lead" value="Head of Growth" />
    </div>
    <div>
      <label>Location</label>
      <input id="loc" placeholder="Remote, Bangalore" value="Remote" />
    </div>
    <div>
      <label>Company <span style="text-transform:none;color:#555">(optional)</span></label>
      <input id="company" placeholder="e.g. Razorpay" />
    </div>
    <div>
      <label>LinkedIn Co. ID <span style="text-transform:none;color:#555">(optional · precise)</span></label>
      <input id="li_id" placeholder="2510761" />
    </div>
    <div>
      <label>Posted in last</label>
      <select id="hours">
        <option value="24">24 hours</option>
        <option value="48">48 hours</option>
        <option value="72" selected>3 days</option>
        <option value="168">1 week</option>
      </select>
    </div>
    <div>
      <label>Results</label>
      <select id="count">
        <option value="15">15</option>
        <option value="25" selected>25</option>
        <option value="50">50</option>
      </select>
    </div>
    <button id="go" class="primary">Find →</button>
  </div>
  <div class="chips" id="sites">
    <div class="chip active" data-site="linkedin">LinkedIn</div>
    <div class="chip" data-site="indeed">Indeed</div>
    <div class="chip" data-site="glassdoor">Glassdoor</div>
    <div class="chip" data-site="zip_recruiter">ZipRecruiter</div>
  </div>
</section>

<section class="results" id="noticeBar" style="padding-bottom:0"></section>
<section class="results" id="results"></section>

<footer>Powered by JobSpy · scored against <span style="color:var(--teal)">your profile</span></footer>

<script>
const $ = s => document.querySelector(s);
const results = $('#results');

// ---- Email gate -------------------------------------------------------
function showGate() { $('#emailGate').style.display = 'flex'; }
function hideGate() { $('#emailGate').style.display = 'none'; }
function gateValid(email, company) {
  return /\S+@\S+\.\S+/.test(email) && company.trim().length >= 2;
}
async function submitGate() {
  const email = $('#gateEmail').value.trim();
  const company = $('#gateCompany').value.trim();
  if (!gateValid(email, company)) {
    $('#gateError').style.display = 'block';
    return;
  }
  $('#gateError').style.display = 'none';
  $('#gateSubmit').disabled = true; $('#gateSubmit').textContent = 'Saving…';
  try {
    await fetch('/capture-email', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({email, company}),
    });
  } catch {}
  localStorage.setItem('jobspy_gate_passed', '1');
  localStorage.setItem('jobspy_gate_email', email);
  hideGate();
  $('#gateSubmit').disabled = false; $('#gateSubmit').textContent = 'Get Access →';
}
if (!localStorage.getItem('jobspy_gate_passed')) {
  showGate();
}
$('#gateSubmit').onclick = submitGate;
$('#gateEmail').addEventListener('keydown', e => { if (e.key === 'Enter') submitGate(); });
$('#gateCompany').addEventListener('keydown', e => { if (e.key === 'Enter') submitGate(); });

// Wake notice — show only on deployed (non-localhost) host
if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
  $('#wakeNotice').style.display = 'block';
  // Auto-hide once they've performed at least one successful action
  setTimeout(() => { $('#wakeNotice').style.display = 'none'; }, 30000);
}

// ---- Cap banner -------------------------------------------------------
function timeUntilMidnightUTC() {
  const now = new Date();
  const t = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1, 0, 0, 0));
  const ms = t - now;
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  return `Resets in ${h}h ${m}m (00:00 UTC)`;
}
function showCapBanner(cap) {
  $('#capBanner').style.display = 'block';
  $('#capBannerReset').textContent = timeUntilMidnightUTC();
  $('#go').disabled = true;
  $('#go').textContent = 'Cap reached';
}
function hideCapBanner() {
  $('#capBanner').style.display = 'none';
}
async function checkCapOnLoad() {
  try {
    const r = await fetch('/limits');
    const d = await r.json();
    if (d.remaining === 0) showCapBanner(d);
  } catch {}
}
checkCapOnLoad();

// ---- Score toggle (persisted) ------------------------------------------
function applyScoreMode() {
  const on = $('#scoreToggle').checked;
  $('#modeBadge').style.display = on ? '' : 'none';
  // Column header swap
  const colHeader = document.querySelector('.score-col');
  if (colHeader) colHeader.textContent = on ? 'Fit Score' : 'JD';
  // Per-row buttons swap label between Score → and View JD →
  document.querySelectorAll('.action-btn').forEach(btn => {
    if (!btn.dataset.locked) {  // only flip default-state buttons
      btn.textContent = on ? 'Score →' : 'View JD →';
      btn.onclick = (e) => (on ? scoreJob : fetchJdOnly)(+btn.dataset.idx, btn);
    }
  });
  localStorage.setItem('jobspy_score_on', on ? '1' : '0');
}
const savedScoreMode = localStorage.getItem('jobspy_score_on');
if (savedScoreMode === '0') $('#scoreToggle').checked = false;
$('#scoreToggle').onchange = applyScoreMode;
applyScoreMode();  // ensure column header + button labels reflect initial state

async function fetchJdOnly(idx, btn) {
  const j = window._jobs[idx];
  btn.disabled = true; btn.textContent = 'Fetching…';
  try {
    const r = await fetch('/fetch-jd', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ job_url: j.job_url || '' }),
    });
    const data = await r.json();
    if (r.status === 429 && data.cap) { showCapBanner(data.cap); return; }
    if (!r.ok) throw new Error(data.error || 'fetch failed');
    const cell = document.getElementById(`score-${idx}`);
    const safeJd = (data.description || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    if (!safeJd) {
      cell.innerHTML = `<div style="color:#888;font-size:12px">No JD available (LinkedIn login wall)</div>`;
      return;
    }
    cell.innerHTML = `
      <div style="color:#50E3C2;font-size:11px;margin-bottom:6px">JD ${data.chars}ch</div>
      <div style="padding:10px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;background:rgba(255,255,255,0.02);max-height:300px;overflow:auto;font-size:11px;line-height:1.5;color:#c8c8c8;white-space:pre-wrap">${safeJd}</div>
    `;
    btn.dataset.locked = '1';
  } catch (e) {
    btn.textContent = 'Retry';
    btn.disabled = false;
    const cell = document.getElementById(`score-${idx}`);
    cell.innerHTML = `<div style="color:#ff9b9b;font-size:12px">${e.message}</div>`;
  }
}

document.querySelectorAll('.chip').forEach(c => {
  c.onclick = () => c.classList.toggle('active');
});

$('#go').onclick = async () => {
  const btn = $('#go');
  btn.disabled = true; btn.textContent = 'Scraping…';
  results.innerHTML = '<div class="status">Scraping job boards (15–45 sec)…</div>';

  const sites = [...document.querySelectorAll('.chip.active')].map(c => c.dataset.site);
  if (!sites.length) {
    results.innerHTML = '<div class="error">Pick at least one site.</div>';
    btn.disabled = false; btn.textContent = 'Find →';
    return;
  }

  try {
    const r = await fetch('/scrape', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        search_term: $('#q').value,
        location: $('#loc').value,
        company: $('#company').value,
        linkedin_company_id: $('#li_id').value,
        hours_old: +$('#hours').value,
        results_wanted: +$('#count').value,
        sites,
      })
    });
    const data = await r.json();
    if (r.status === 429 && data.cap) {
      showCapBanner(data.cap);
      results.innerHTML = '';
      return;
    }
    if (!r.ok) {
      if (data.site_errors) {
        const lines = Object.entries(data.site_errors)
          .map(([s, m]) => `<div>• <b>${s}</b>: ${m}</div>`).join('');
        throw new Error(`<div>${data.error || 'failed'}</div>${lines}`);
      }
      throw new Error(data.error || 'failed');
    }
    // Partial-success warning lives in its own bar above results so it isn't wiped
    const noticeBar = document.getElementById('noticeBar');
    if (data.site_errors && Object.keys(data.site_errors).length) {
      const warned = Object.entries(data.site_errors)
        .map(([s, m]) => `${s}: ${m}`).join(' · ');
      noticeBar.innerHTML = `<div class="error" style="background:rgba(255,160,90,0.08);border-color:rgba(255,160,90,0.4);color:#ffc28a;">⚠ Partial: ${warned}</div>`;
    } else {
      noticeBar.innerHTML = '';
    }
    renderJobs(data.jobs || []);
  } catch (e) {
    results.innerHTML = `<div class="error">${String(e.message || e)}</div>`;
  } finally {
    if (!$('#capBanner').style.display || $('#capBanner').style.display === 'none') {
      btn.disabled = false; btn.textContent = 'Find →';
    }
  }
};

function relTime(dt) {
  if (!dt) return '—';
  const t = new Date(dt).getTime();
  if (isNaN(t)) return dt;
  const diffH = Math.round((Date.now() - t) / 3600000);
  if (diffH < 1)  return 'just now';
  if (diffH < 24) return `${diffH}h ago`;
  const d = Math.round(diffH / 24);
  return `${d}d ago`;
}

async function scoreJob(idx, btn) {
  const j = window._jobs[idx];
  btn.disabled = true; btn.textContent = 'Fetching JD…';
  try {
    const r = await fetch('/score', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        title: j.title, company: j.company,
        location: j.location || j.city || '',
        description: j.description || '',
        job_url: j.job_url || '',
      })
    });
    const data = await r.json();
    if (r.status === 429 && data.cap) { showCapBanner(data.cap); return; }
    if (!r.ok) throw new Error(data.error || 'score failed');
    const cell = document.getElementById(`score-${idx}`);
    const verdictColor = (data.verdict || '').toUpperCase() === 'APPLY' ? 'var(--teal)' : '#ff9b9b';
    const sourceTag = data._jd_source === 'fetched'
      ? `<span style="color:var(--teal);font-size:10px">JD ${data._jd_chars}ch</span>`
      : `<span style="color:#888;font-size:10px">title only</span>`;
    const jdText = (data._jd_text || '').trim();
    const safeJd = jdText.replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const jdToggle = jdText
      ? `<div style="margin-top:8px"><a href="#" onclick="event.preventDefault(); const d=document.getElementById('jd-${idx}'); d.style.display = d.style.display==='block' ? 'none' : 'block';" style="color:var(--accent);font-size:11px">View full JD ↓</a>
         <div id="jd-${idx}" style="display:none;margin-top:8px;padding:10px;border:1px solid rgba(255,255,255,0.08);border-radius:8px;background:rgba(255,255,255,0.02);max-height:300px;overflow:auto;font-size:11px;line-height:1.5;color:#c8c8c8;white-space:pre-wrap">${safeJd}</div></div>`
      : '';
    cell.innerHTML = `
      <div style="font-weight:600;color:${verdictColor};font-size:13px">${data.score}/10 · ${data.verdict || '?'} ${sourceTag}</div>
      <div style="color:#a0a0a0;font-size:12px;margin-top:4px;line-height:1.4">${(data.fit_summary || '').slice(0, 200)}</div>
      ${data.outreach_hook ? `<div style="color:var(--teal);font-size:12px;margin-top:6px;font-style:italic">↳ ${data.outreach_hook}</div>` : ''}
      ${jdToggle}
    `;
  } catch (e) {
    btn.textContent = 'Retry';
    btn.disabled = false;
    const cell = document.getElementById(`score-${idx}`);
    cell.innerHTML = `<div style="color:#ff9b9b;font-size:12px">${e.message}</div>`;
  }
}

function renderJobs(jobs) {
  if (!jobs.length) {
    results.innerHTML = '<div class="status">No jobs found. Try a longer time window or different sites.</div>';
    return;
  }
  jobs.sort((a, b) => (b.date_posted || '').localeCompare(a.date_posted || ''));
  window._jobs = jobs;

  const rows = jobs.map((j, idx) => {
    const salary = (j.min_amount || j.max_amount)
      ? `${j.min_amount || '?'}–${j.max_amount || '?'} ${j.currency || ''}`
      : '—';
    return `
      <tr>
        <td><a href="${j.job_url}" target="_blank">${j.title || '—'}<span style="opacity:0.6;font-size:0.85em;margin-left:4px">↗</span></a></td>
        <td>${j.company || '—'}</td>
        <td>${j.location || j.city || '—'}</td>
        <td>${salary}</td>
        <td>${relTime(j.date_posted)}</td>
        <td><span class="site-badge">${j.site || '—'}</span></td>
        <td id="score-${idx}" class="score-cell">
          <button class="score-btn action-btn" data-idx="${idx}">Score →</button>
        </td>
      </tr>`;
  }).join('');
  results.innerHTML = `
    <div style="color:#a0a0a0;font-size:13px;margin-bottom:12px;">${jobs.length} roles found (deduped)</div>
    <table>
      <thead><tr>
        <th>Title</th><th>Company</th><th>Location</th><th>Salary</th><th>Posted</th><th>Source</th><th class="score-col">Fit Score</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  applyScoreMode();  // honor toggle for newly-rendered rows
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7861))
    print(f"\n🔎  JobSpy UI")
    print(f"   http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
