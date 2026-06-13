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
from pathlib import Path
from flask import Flask, request, jsonify, Response

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

    try:
        df = scrape_jobs(**scrape_kwargs)
    except Exception as e:
        return jsonify({"error": f"jobspy failed: {e}"}), 500

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
    })


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

    # Lazy-fetch full JD from LinkedIn if not already provided
    jd_source = "title-only"
    if not description and "linkedin.com" in job_url:
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
  <p>Scrape LinkedIn, Indeed, Google, Glassdoor concurrently. Filter by age. Rank by fit.</p>
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
    <div class="chip active" data-site="google">Google</div>
    <div class="chip" data-site="indeed">Indeed</div>
    <div class="chip" data-site="glassdoor">Glassdoor</div>
    <div class="chip" data-site="zip_recruiter">ZipRecruiter</div>
  </div>
</section>

<section class="results" id="results"></section>

<footer>Powered by JobSpy · scored against <span style="color:var(--teal)">your profile</span></footer>

<script>
const $ = s => document.querySelector(s);
const results = $('#results');

// ---- Score toggle (persisted) ------------------------------------------
function applyScoreMode() {
  const on = $('#scoreToggle').checked;
  $('#modeBadge').style.display = on ? '' : 'none';
  document.querySelectorAll('.score-col, .score-cell').forEach(el => {
    el.style.display = on ? '' : 'none';
  });
  localStorage.setItem('jobspy_score_on', on ? '1' : '0');
}
const savedScoreMode = localStorage.getItem('jobspy_score_on');
if (savedScoreMode === '0') $('#scoreToggle').checked = false;
$('#scoreToggle').onchange = applyScoreMode;

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
    if (!r.ok) throw new Error(data.error || 'failed');
    renderJobs(data.jobs || []);
  } catch (e) {
    results.innerHTML = `<div class="error">${String(e.message || e)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = 'Find →';
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
        <td><a href="${j.job_url}" target="_blank">${j.title || '—'}</a></td>
        <td>${j.company || '—'}</td>
        <td>${j.location || j.city || '—'}</td>
        <td>${salary}</td>
        <td>${relTime(j.date_posted)}</td>
        <td><span class="site-badge">${j.site || '—'}</span></td>
        <td id="score-${idx}" class="score-cell">
          <button class="score-btn" onclick="scoreJob(${idx}, this)">Score →</button>
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
