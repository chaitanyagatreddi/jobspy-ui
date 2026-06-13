"""
Template profile for the JobSpy scorer.

To personalize:
  1. Copy this file to `profile_local.py` (gitignored).
  2. Fill in your real career details.
  3. The scorer auto-prefers `profile_local.py` if present.

The PROFILE dict is consumed by scorer.py — fields are illustrative; add or
remove keys to fit your own evaluation logic.
"""

PROFILE = {
    "name": "Your Name",
    "current_focus": "Target roles — e.g. Head of Growth / GTM Lead",
    "target_stages": ["seed", "series_a", "series_b"],

    "summary": """
    One paragraph of who you are. What do you build? What do you ship?
    Where have you operated? What are your strongest signals?
    Be specific. The scorer uses this to match JD requirements.
    """,

    "proof_of_work": [
        {
            "id": "example_role",
            "company": "Company Name",
            "title": "Your Title",
            "headline": "One-line result — e.g. $1M → $10M ARR in 12 months",
            "highlights": [
                "Specific shipped artifact with metrics",
                "Another concrete proof of work",
                "Cross-functional scope (team built, budget owned)",
            ],
            "best_for": ["growth", "pmm", "founder_office"],
        },
    ],

    "acquisitions": [],

    "technical_background": [
        "Engineering depth or technical literacy you can defend",
    ],

    "strengths": [
        "What you do better than 90% of people in this role",
    ],

    "weak_signals": [
        "Honest gaps — e.g. no formal PM title for 3+ years",
        "What a hiring manager will rightly flag",
    ],
}
