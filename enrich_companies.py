#!/usr/bin/env python3
"""
Company-side enrichment via Mamba GTM Suite (Apify).

Reads a list of company domains (from CLI args or scored_jobs.csv companies
mapped to domains), runs aggregate_signals + icp_fit for each, writes a
ranked CSV with composite signal + ICP fit score.

Combines with scored_jobs.csv to produce a final "priority queue":
  good JD score + good company timing + good ICP fit = top of list.

Usage:
    python3 enrich_companies.py stripe.com vercel.com supabase.com
    python3 enrich_companies.py --from-scored-csv
"""
import os
import csv
import sys
import argparse
from pathlib import Path

# Local mamba client
sys.path.insert(0, str(Path(__file__).parent))
import mamba

# Pull ICP description directly from profile.py so it stays in sync
from profile import PROFILE

ICP_DESCRIPTION = f"""
{PROFILE['summary'].strip()}

Target stages: {', '.join(PROFILE['target_stages'])}
Focus: {PROFILE['current_focus']}
Sectors: AI/ML, cybersecurity, devtools, B2B SaaS with PLG motion
""".strip()


COMPANY_DOMAIN_MAP = {
    # Hand-curated where the inferred .com is wrong
    "moengage":   "moengage.com",
    "gnani.ai":   "gnani.ai",
    "sap":        "sap.com",
    "writesonic": "writesonic.com",
    "appknox":    "appknox.com",
    "sprinto":    "sprinto.com",
    "infisical":  "infisical.com",
    "tide":       "tide.co",
}


def name_to_domain(name: str) -> str:
    key = name.strip().lower()
    if key in COMPANY_DOMAIN_MAP:
        return COMPANY_DOMAIN_MAP[key]
    if "." in key:
        return key
    return f"{key.replace(' ', '')}.com"


def enrich_one(domain: str) -> dict:
    out = {"domain": domain}
    try:
        agg = mamba.aggregate_signals(domain)
        if agg and isinstance(agg, list):
            a = agg[0]
            out.update({
                "composite_signal": a.get("composite_signal"),
                "composite_score":  a.get("composite_score"),
                "gtm_role_count":   a.get("gtm_role_count"),
                "top_gtm_role":     a.get("top_gtm_role"),
                "ats_platform":     a.get("ats_platform"),
                "signal_summary":   a.get("gtm_signal_summary"),
            })
    except Exception as e:
        out["aggregate_error"] = str(e)[:200]

    try:
        fit = mamba.icp_fit(domain, ICP_DESCRIPTION, template="b2b_saas")
        if fit and isinstance(fit, list):
            f = fit[0]
            out.update({
                "icp_score":       f.get("score") or f.get("icp_score"),
                "icp_tier":        f.get("tier"),
                "icp_explanation": (f.get("explanation") or "")[:300],
            })
    except Exception as e:
        out["icp_error"] = str(e)[:200]

    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("domains", nargs="*", help="company domains to enrich")
    p.add_argument("--from-scored-csv", action="store_true",
                   help="read unique companies from scored_jobs.csv")
    p.add_argument("--out", default="enriched_companies.csv")
    args = p.parse_args()

    domains: list[str] = list(args.domains)

    if args.from_scored_csv:
        csv_path = Path(__file__).parent / "scored_jobs.csv"
        if not csv_path.exists():
            sys.exit("scored_jobs.csv not found")
        seen = set()
        with csv_path.open() as fh:
            for row in csv.DictReader(fh):
                name = (row.get("company") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                domains.append(name_to_domain(name))

    if not domains:
        sys.exit("no domains. Pass them as args or use --from-scored-csv")

    # Dedupe while preserving order
    domains = list(dict.fromkeys(domains))

    print(f"Enriching {len(domains)} companies via Mamba GTM Suite...")
    if not os.getenv("APIFY_TOKEN"):
        sys.exit("APIFY_TOKEN not set; export it before running")

    rows: list[dict] = []
    for d in domains:
        print(f"  → {d}")
        rows.append(enrich_one(d))

    out_path = Path(__file__).parent / args.out
    fieldnames = [
        "domain", "composite_signal", "composite_score",
        "gtm_role_count", "top_gtm_role", "ats_platform", "signal_summary",
        "icp_score", "icp_tier", "icp_explanation",
        "aggregate_error", "icp_error",
    ]
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"\n✓ Wrote {out_path}")
    # Sort + display top 5 by composite_score desc
    rows.sort(key=lambda r: float(r.get("composite_score") or 0), reverse=True)
    print("\nTop 5 by GTM signal:")
    for r in rows[:5]:
        print(f"  {r['domain']:<25} signal={r.get('composite_signal','-'):<8} "
              f"score={r.get('composite_score','-'):<5} roles={r.get('gtm_role_count','-'):<3} "
              f"icp={r.get('icp_score','-')}")


if __name__ == "__main__":
    main()
