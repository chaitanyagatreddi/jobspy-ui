"""
Human-in-the-loop JD scorer.

Usage:
    python3 scorer.py                    # paste JD interactively
    python3 scorer.py --jd job.txt       # read JD from file
    python3 scorer.py --jd job.txt --company "CloudSEK" --bucket cybersecurity_grc
"""

import os
import sys
import json
import argparse
from openai import OpenAI
try:
    from profile_local import PROFILE  # personal profile, gitignored
except ImportError:
    from profile import PROFILE  # public template fallback

CLIENT = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

SYSTEM_PROMPT = """You are a brutally honest career coach helping a senior operator evaluate job fit.
You have deep knowledge of what Head of Growth, Head of Marketing, PMM, GTM Lead, Product Manager,
Chief of Staff, and Founder's Office roles actually require day-to-day.
Be direct. Don't inflate scores. A 7 means genuinely strong fit, not just "good enough".

CRITICAL SCORING RULES:
- Title history is NOT the primary signal. Impact and proof of work are.
- A candidate who built a 0-1 product, ran Fortune 50 POCs, shipped AI systems, and drove $4.4M revenue
  has more PM evidence than someone with "Product Manager" on a CV who maintained a backlog.
- Judge what the candidate actually DID, not what their title said.
- If proof of work directly maps to the JD requirements, score it as covered — regardless of title.
- Only penalize title gap if there is genuinely NO proof of work covering that requirement.
- Domain knowledge gaps are valid penalties. Title gaps without proof-of-work gaps are NOT.
"""

def ask_clarifying_questions(jd: str, profile: dict) -> list[str]:
    """Generate 2-3 clarifying questions based on JD vs profile gaps."""
    prompt = f"""
You are reviewing this job description against this candidate's profile.

JOB DESCRIPTION:
{jd}

CANDIDATE PROFILE SUMMARY:
{profile['summary']}

PROOF OF WORK HIGHLIGHTS:
{chr(10).join([f"- {p['headline']} ({p['company']})" for p in profile['proof_of_work']])}

Identify 2-3 specific things that are unclear or could tip the score up or down.
Ask sharp clarifying questions — things the candidate would know from their own experience
that aren't in the case studies. Focus on gaps or ambiguities in the JD match.

Return ONLY a JSON array of question strings, nothing else.
Example: ["Did you own pricing strategy or just positioning at Writesonic?", "Was the ABM at Wayleadr inbound or outbound-first?"]
"""
    response = CLIENT.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    try:
        return json.loads(response.choices[0].message.content)
    except Exception:
        return ["Can you describe your most relevant experience for this specific role?"]


def score_jd(jd: str, profile: dict, answers: list[dict], company: str = "", bucket: str = "") -> dict:
    """Score the JD against profile + clarification answers. Returns score + advice."""

    answers_text = "\n".join([f"Q: {a['q']}\nA: {a['a']}" for a in answers]) if answers else "No clarifications provided."

    # pass ALL proof of work, bucket-relevant ones first
    relevant_pow = profile["proof_of_work"]
    if bucket:
        relevant_pow = sorted(
            profile["proof_of_work"],
            key=lambda p: 1 if bucket in p.get("best_for", []) else 0,
            reverse=True
        )

    # always include ALL entries so scorer sees full picture
    pow_text = "\n\n".join([
        f"### {p['company']} — {p['title']}\n{p['headline']}\n" +
        "\n".join([f"- {h}" for h in p["highlights"]])
        for p in relevant_pow  # all entries, bucket-sorted
    ])

    # full technical background always included
    tech_text = "\n".join([f"- {t}" for t in profile.get("technical_background", [])])
    strengths_text = "\n".join([f"- {s}" for s in profile.get("strengths", [])])

    prompt = f"""
You are scoring a job application fit on a 0-10 scale.

SCORING RULES:
- 8-10: Exceptional fit. Almost every requirement is covered by direct proof of work.
- 7: Strong fit. Most core requirements covered, 1-2 gaps are learnable.
- 5-6: Partial fit. Strong on some dimensions, real gaps on others.
- 3-4: Weak fit. A stretch. Would need significant repositioning.
- 1-2: Wrong role. Fundamental mismatch.

IMPORTANT: Score < 7 means we SKIP this application entirely. Be honest.

---

JOB DESCRIPTION:
{jd}

COMPANY: {company or "Unknown"}
BUCKET/INDUSTRY: {bucket or "Unknown"}

---

CANDIDATE: {profile['name']}

SUMMARY:
{profile['summary']}

PROOF OF WORK (ALL ENTRIES — evaluate every one against the JD):
{pow_text}

ACQUISITIONS: {', '.join(profile['acquisitions'])}

TECHNICAL BACKGROUND:
{tech_text}

STRENGTHS:
{strengths_text}

KNOWN WEAK SIGNALS:
{chr(10).join(profile['weak_signals'])}

---

CLARIFICATION ANSWERS (use these to fill gaps not covered above):
{answers_text}

---

Return a JSON object with these exact keys:
{{
  "score": <0-10 integer>,
  "verdict": "<APPLY / SKIP>",
  "fit_summary": "<2-3 sentences on why this score>",
  "strongest_proof_of_work": ["<which case study/project to lead with and why>"],
  "cv_changes": ["<specific bullet to add or reframe in CV for this JD>"],
  "gaps": ["<real gap and how to address it or frame it>"],
  "outreach_angle": "<founder intro / talent outreach / cold email — and why>",
  "outreach_hook": "<1 sentence opening line for the outreach message, specific to this company>"
}}

Return ONLY the JSON. No preamble.
"""
    response = CLIENT.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=1500,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    )
    try:
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception:
        return {"error": "Failed to parse score", "raw": response.content[0].text}


def display_result(result: dict, company: str):
    score = result.get("score", 0)
    verdict = result.get("verdict", "SKIP")

    print("\n" + "="*60)
    print(f"  SCORE: {score}/10   |   {verdict}")
    print("="*60)

    if verdict == "SKIP":
        print(f"\n✗ Skip — score below 7.")
        print(f"\nWhy: {result.get('fit_summary', '')}")
        if result.get("gaps"):
            print("\nGaps:")
            for g in result["gaps"]:
                print(f"  - {g}")
        return

    print(f"\nFit: {result.get('fit_summary', '')}")

    if result.get("strongest_proof_of_work"):
        print("\n── Lead with this proof of work ──")
        for p in result["strongest_proof_of_work"]:
            print(f"  → {p}")

    if result.get("cv_changes"):
        print("\n── CV changes for this JD ──")
        for c in result["cv_changes"]:
            print(f"  • {c}")

    if result.get("gaps"):
        print("\n── Gaps to address ──")
        for g in result["gaps"]:
            print(f"  △ {g}")

    print(f"\n── Outreach ──")
    print(f"  Channel: {result.get('outreach_angle', '')}")
    print(f"  Hook: \"{result.get('outreach_hook', '')}\"")
    print()


def run(jd: str, company: str = "", bucket: str = ""):
    print(f"\nAnalysing JD{' for ' + company if company else ''}...")

    # step 1: generate clarifying questions
    questions = ask_clarifying_questions(jd, PROFILE)

    # step 2: ask human
    answers = []
    print("\n── A few quick questions before scoring ──\n")
    for q in questions:
        print(f"Q: {q}")
        answer = input("A: ").strip()
        answers.append({"q": q, "a": answer})
        print()

    # step 3: score
    print("Scoring...")
    result = score_jd(jd, PROFILE, answers, company, bucket)

    if "error" in result:
        print(f"Error: {result['error']}")
        print(result.get("raw", ""))
        return

    display_result(result, company)

    # step 4: ask to save
    save = input("Save this result? (y/n): ").strip().lower()
    if save == "y":
        import csv, datetime
        row = {
            "company": company,
            "bucket": bucket,
            "score": result.get("score"),
            "verdict": result.get("verdict"),
            "fit_summary": result.get("fit_summary"),
            "outreach_angle": result.get("outreach_angle"),
            "outreach_hook": result.get("outreach_hook"),
            "scored_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        file = "scored_jobs.csv"
        write_header = not __import__("os").path.exists(file)
        with open(file, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if write_header:
                writer.writeheader()
            writer.writerow(row)
        print(f"Saved → {file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--jd", help="Path to a text file containing the JD")
    parser.add_argument("--company", default="", help="Company name")
    parser.add_argument("--bucket", default="", help="Industry bucket")
    args = parser.parse_args()

    if args.jd:
        with open(args.jd, "r", encoding="utf-8") as f:
            jd_text = f.read()
    else:
        print("Paste the job description below. Press Enter twice when done:\n")
        lines = []
        while True:
            line = input()
            if line == "" and lines and lines[-1] == "":
                break
            lines.append(line)
        jd_text = "\n".join(lines).strip()

    if not jd_text:
        print("No JD provided.")
        sys.exit(1)

    run(jd_text, args.company, args.bucket)
