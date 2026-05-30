#!/usr/bin/env python3
"""
Job Watcher
-----------
Checks two career sites for NEW openings that fit your CV and a target
experience range, then emails you only the relevant ones:

  * PwC Acceleration Centres   -> jobs-ta.pwc.com   (Phenom "widgets" API)
  * Deloitte USI               -> usijobs.deloitte.com (Avature, HTML)

Pipeline:  fetch newest jobs  ->  compare with seen.json (so you only ever
hear about genuinely new ones)  ->  ask an LLM to score each new job for
relevance + experience fit  ->  email the matches.

Usage:
  python main.py          Normal run. (The very first run is a silent
                          "baseline" -- it records what's currently open and
                          sends NO email, so you don't get flooded.)
  python main.py --test   Just fetch and print what each site returns.
                          Sends no email. Use this to confirm both sites work.
"""

import os
import re
import sys
import json
import html
import datetime as dt
from pathlib import Path

import yaml
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
CV_PATH = ROOT / "cv.txt"
STATE_PATH = ROOT / "seen.json"

# Cheap, fast model -- plenty good for relevance scoring.
MODEL = "claude-haiku-4-5-20251001"

# Look like a normal browser so the sites don't reject the request.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def clean_text(s: str, limit: int = 6000) -> str:
    """Strip HTML tags, unescape entities, collapse whitespace, truncate."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


# --------------------------------------------------------------------------
# PwC Acceleration Centres  (Phenom People "widgets" API)
# --------------------------------------------------------------------------
def fetch_pwc(cfg: dict, company: str = "PwC Acceleration Centres"):
    domain = cfg.get("pwc_domain", "jobs-ta.pwc.com")
    refnum_fallback = cfg.get("pwc_refnum", "PACPACGLOBAL")
    size = int(cfg.get("pwc_size", 100))

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA,
                         "Accept": "application/json, text/plain, */*"})

    # 1) Load the search page first: this sets cookies and lets us read the
    #    site's refNum and (if present) a CSRF token -- both help the API call.
    refnum, csrf = refnum_fallback, None
    try:
        page = sess.get(f"https://{domain}/global/en/search-results", timeout=30)
        m = re.search(r'"refNum"\s*:\s*"([A-Za-z0-9]+)"', page.text)
        if m:
            refnum = m.group(1)
        m = (re.search(r'"csrf[_-]?token"\s*:\s*"([^"]+)"', page.text, re.I)
             or re.search(r'name="csrf-token"\s+content="([^"]+)"', page.text, re.I))
        if m:
            csrf = m.group(1)
    except Exception as e:
        print(f"  (pwc) note: could not pre-load page ({e}); using fallback token")

    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["x-csrf-token"] = csrf

    payload = {
        "lang": "en_global", "deviceType": "desktop", "country": "global",
        "pageName": "search-results", "size": size, "from": 0,
        "jobs": True, "counts": True,
        "all_fields": ["category", "country", "state", "city", "type"],
        "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
        "pageId": "page1", "siteType": "external", "keywords": "",
        "global": True, "selected_fields": {},
        "sort": {"order": "desc", "field": "postedDate"},  # newest first
        "locationData": {}, "refNum": refnum, "ddoKey": "refineSearch",
    }

    r = sess.post(f"https://{domain}/widgets", json=payload,
                  headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    rs = data.get("refineSearch") or data.get("eagerLoadRefineSearch") or {}
    raw = (rs.get("data", {}) or {}).get("jobs", []) or []

    jobs = []
    for j in raw:
        jid = j.get("jobId") or j.get("id") or j.get("jobSeqNo")
        if not jid:
            continue
        url = (j.get("applyUrl") or j.get("jobUrl")
               or f"https://{domain}/global/en/job/{jid}")
        # The page to open for the FULL description (job page, not apply form).
        detail_url = (j.get("jobUrl") or j.get("applyUrl")
                      or f"https://{domain}/global/en/job/{jid}")
        loc = (j.get("cityStateCountry") or j.get("location")
               or ", ".join(x for x in [j.get("city"), j.get("state"),
                                        j.get("country")] if x))
        desc = j.get("description") or j.get("descriptionTeaser") or ""
        jobs.append({
            "id": f"pwc:{jid}",
            "title": (j.get("title") or "").strip(),
            "url": url,
            "location": loc,
            "description": clean_text(desc),
            "company": company,
            "_detail_url": detail_url,   # opened later, only for NEW jobs
        })
    return jobs


# --------------------------------------------------------------------------
# Deloitte USI  (Avature -- plain HTML, 10 jobs/page, newest first)
# --------------------------------------------------------------------------
def fetch_deloitte(cfg: dict, seen, company: str = "Deloitte USI"):
    seen = seen or {}
    base = "https://usijobs.deloitte.com"
    max_pages = int(cfg.get("deloitte_max_pages", 5))

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    jobs, found = [], set()
    for page in range(max_pages):
        offset = page * 10
        url = (f"{base}/en_US/careersUSI/SearchJobs/"
               f"?jobRecordsPerPage=10&jobOffset={offset}")
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"  (deloitte) page {page + 1} failed: {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        anchors = soup.select('a[href*="/careersUSI/JobDetail/"]')
        if not anchors:
            break

        page_new = 0
        for a in anchors:
            href = a.get("href", "")
            jid = href.rstrip("/").split("/")[-1]
            if not jid or jid in found:
                continue
            found.add(jid)
            full = href if href.startswith("http") else base + href
            jobs.append({
                "id": f"deloitte:{jid}",
                "title": a.get_text(strip=True),
                "url": full,
                "location": "",
                "description": "",
                "company": company,
                "_detail_url": full,   # fetched later, only for NEW jobs
            })
            if f"deloitte:{jid}" not in seen:
                page_new += 1

        # Newest-first, so once a whole page is already-seen we can stop.
        if page_new == 0 and seen:
            break
    return jobs


def fill_deloitte_description(job: dict, sess: requests.Session):
    """Fetch a Deloitte job's detail page to get its full text (new jobs only)."""
    try:
        r = sess.get(job["_detail_url"], timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        job["description"] = clean_text(soup.get_text(" ", strip=True), 6000)
    except Exception as e:
        print(f"  (deloitte) detail fetch failed for {job['id']}: {e}")


def fill_pwc_description(job: dict, sess: requests.Session):
    """Open a PwC job's page and read the FULL description (new jobs only).

    PwC (Phenom) job pages embed the complete posting as JSON-LD 'JobPosting'
    data (the same structured data Google for Jobs reads). We pull the
    description from there. If anything fails or the fetched text isn't more
    complete than what the search API already gave us, we keep the API text --
    so this can never make matching worse.
    """
    url = job.get("_detail_url") or job.get("url")
    if not url:
        return
    try:
        r = sess.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        full = ""
        for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = tag.string or tag.get_text() or ""
            try:
                data = json.loads(raw)
            except Exception:
                continue
            # Flatten possible lists / @graph wrappers, then look for JobPosting.
            items = data if isinstance(data, list) else [data]
            flat = []
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                    flat.extend(it["@graph"])
                else:
                    flat.append(it)
            for it in flat:
                if isinstance(it, dict) and str(it.get("@type", "")).lower() == "jobposting":
                    if it.get("description"):
                        full = clean_text(it["description"])
                        break
            if full:
                break

        # Only replace if we actually got something more complete.
        if full and len(full) > len(job.get("description", "")):
            job["description"] = full
    except Exception as e:
        print(f"  (pwc) full-description fetch failed for {job['id']}: {e}")


# --------------------------------------------------------------------------
# Matching  (relevance score + experience fit, via the LLM)
# --------------------------------------------------------------------------
def score_job(client: Anthropic, cv: str, job: dict, exp_min: int, exp_max: int):
    prompt = f"""You are screening job postings for a candidate. Compare the candidate's CV with the job posting below.

The candidate is targeting roles that require roughly {exp_min} to {exp_max} years of professional experience.

Return ONLY a JSON object (no markdown, no extra text), exactly in this form:
{{"score": <integer 0-100>, "experience_fit": <true or false>, "stated_experience": "<what the posting says about required years, or 'not specified'>", "reason": "<one short sentence>"}}

Guidance:
- "score": how well the role matches this candidate's background and likely interests (0-100). Above 70 means genuinely worth applying.
- "experience_fit": true if the role's required experience is within or overlaps {exp_min}-{exp_max} years (for example "{exp_min}+ years", "{exp_min - 1}-{exp_max} years", or a seniority level that usually needs about {exp_min}-{exp_max} years). false if it is clearly junior (e.g. 0-3 years) or clearly more senior (e.g. 10+ years, director-level). If years are not stated, infer from the role's seniority.

=== CANDIDATE CV ===
{cv}

=== JOB POSTING ===
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
Description: {job['description'][:5000]}
"""
    resp = client.messages.create(
        model=MODEL, max_tokens=250,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    try:
        d = json.loads(text)
        return (int(d.get("score", 0)),
                bool(d.get("experience_fit", False)),
                str(d.get("stated_experience", "")),
                str(d.get("reason", "")))
    except Exception:
        return (0, False, "", "Could not parse model response.")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return None  # None == first run -> baseline mode


def save_state(seen: dict):
    STATE_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def send_email(subject: str, html_body: str):
    """Send via Resend's API (no domain needed; delivers to your own address)."""
    api_key = os.environ["RESEND_API_KEY"]
    to = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"from": from_addr, "to": [to], "subject": subject, "html": html_body},
        timeout=30,
    )
    r.raise_for_status()


def build_digest(matches: list) -> str:
    rows = []
    for m in matches:
        j = m["job"]
        loc = f" &middot; {html.escape(j['location'])}" if j["location"] else ""
        rows.append(
            f"""<div style="margin-bottom:18px;padding-bottom:14px;border-bottom:1px solid #eee">
  <a href="{j['url']}" style="font-size:16px;font-weight:600;color:#1a56db;text-decoration:none">{html.escape(j['title'])}</a>
  <div style="color:#555;font-size:13px;margin:3px 0">{html.escape(j['company'])}{loc} &middot; match {m['score']}/100</div>
  <div style="color:#333;font-size:14px">{html.escape(m['reason'])}</div>
  <div style="color:#888;font-size:12px;margin-top:2px">Experience required: {html.escape(m['stated_experience'] or 'not specified')}</div>
</div>""")
    n = len(matches)
    return f"""<div style="font-family:system-ui,-apple-system,sans-serif;max-width:620px;margin:auto">
  <h2 style="font-size:18px">{n} new job{'s' if n != 1 else ''} matching your CV</h2>
  {''.join(rows)}
  <p style="color:#999;font-size:12px;margin-top:20px">Sent by your Job Watcher &middot; PwC Acceleration Centres + Deloitte USI</p>
</div>"""


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def fetch_all(cfg: dict, seen):
    all_jobs = []
    print("Fetching PwC Acceleration Centres ...")
    try:
        pj = fetch_pwc(cfg)
        print(f"  PwC: {len(pj)} jobs")
        all_jobs += pj
    except Exception as e:
        print(f"  !! PwC fetch FAILED: {e}")

    print("Fetching Deloitte USI ...")
    try:
        dj = fetch_deloitte(cfg, seen)
        print(f"  Deloitte: {len(dj)} jobs")
        all_jobs += dj
    except Exception as e:
        print(f"  !! Deloitte fetch FAILED: {e}")
    return all_jobs


def main():
    test = "--test" in sys.argv
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    threshold = int(cfg.get("match_threshold", 70))
    exp_min = int(cfg.get("experience_min_years", 5))
    exp_max = int(cfg.get("experience_max_years", 7))

    state = load_state()
    all_jobs = fetch_all(cfg, state)

    if test:
        print("\n--- TEST MODE (no email sent) ---")
        for src in ("pwc", "deloitte"):
            sample = [j for j in all_jobs if j["id"].startswith(src)][:5]
            print(f"\n{src.upper()} -- first {len(sample)} of "
                  f"{len([j for j in all_jobs if j['id'].startswith(src)])}:")
            for j in sample:
                print(f"   - {j['title']}  ({j['location'] or 'location n/a'})")
        print(f"\nTotal fetched: {len(all_jobs)}")
        if not all_jobs:
            print("Nothing fetched -- check the messages above for which site failed.")
        return

    today = dt.date.today().isoformat()

    # First ever run: just record what's open now; send nothing.
    if state is None:
        save_state({j["id"]: today for j in all_jobs})
        print(f"\nBaseline run complete: recorded {len(all_jobs)} current jobs. "
              "No email sent (this is expected on the first run).")
        return

    new_jobs = [j for j in all_jobs if j["id"] not in state]
    print(f"\n{len(new_jobs)} new job(s) since last run")

    matches = []
    if new_jobs:
        cv = CV_PATH.read_text()
        client = Anthropic()  # reads ANTHROPIC_API_KEY
        web = requests.Session()
        web.headers.update({"User-Agent": UA})
        for j in new_jobs:
            # For each NEW job, open its page and read the full posting.
            if j["id"].startswith("pwc:"):
                fill_pwc_description(j, web)
            elif j["id"].startswith("deloitte:") and not j["description"]:
                fill_deloitte_description(j, web)
            score, fit, stated, reason = score_job(client, cv, j, exp_min, exp_max)
            ok = score >= threshold and fit
            print(f"  [{score:>3} | exp:{'Y' if fit else 'N'} | "
                  f"{'MATCH' if ok else 'skip'}] {j['company']} - {j['title']}")
            if ok:
                matches.append({"job": j, "score": score,
                                "stated_experience": stated, "reason": reason})

    # Mark everything we saw as seen, so each job is scored only once.
    for j in all_jobs:
        state.setdefault(j["id"], today)
    save_state(state)

    if matches:
        matches.sort(key=lambda m: m["score"], reverse=True)
        n = len(matches)
        send_email(f"{n} new job match{'es' if n != 1 else ''} (PwC / Deloitte)",
                   build_digest(matches))
        print(f"\nEmailed {n} match(es).")
    else:
        print("\nNo new matches above your threshold; no email sent.")


if __name__ == "__main__":
    main()
