#!/usr/bin/env python3
"""
Job Watcher (multi-company)
---------------------------
Checks many consulting/advisory career sites for NEW openings that fit a CV and
a target experience band, then emails only the relevant ones.

Each company in config.yaml names a `platform`; one reusable fetcher handles
each platform:
    pwc        - bespoke PwC Acceleration Centres (Phenom). LIVE/verified.
    deloitte   - bespoke Deloitte USI (Avature). LIVE/verified.
    phenom     - generic Phenom careers site (needs phenom_domain)
    workday    - generic Workday site (needs workday_tenant/dc/site)
    greenhouse - needs token
    lever      - needs token
    ashby      - needs token
    smartrecruiters - needs token
    linkedin   - name-based fallback (BEST-EFFORT; may be blocked from CI IPs)

Pipeline: fetch newest jobs per company -> diff vs seen.json -> for each NEW job
read its full description -> LLM scores relevance + experience fit -> email matches.

Usage:
    python main.py            Normal run (first run = silent baseline).
    python main.py --test     Fetch + print per-company counts; no email.
    python main.py --discover Same as --test but prints a compact PASS/FAIL
                              table to confirm which companies' feeds work.
                              Run this ONCE on GitHub to verify `verify: true`
                              entries (your build machine can't reach them).
"""

import os
import re
import sys
import json
import html
import datetime as dt
from pathlib import Path
from urllib.parse import quote_plus

import yaml
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
CV_PATH = ROOT / "cv.txt"
STATE_PATH = ROOT / "seen.json"

MODEL = "claude-haiku-4-5-20251001"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def clean_text(s: str, limit: int = 6000) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


# ===========================================================================
# Platform fetchers. Each returns a list of normalised jobs:
#   {id, title, url, location, description, company}
# plus an optional "_detail_url" used to fetch full text for NEW jobs only.
# Namespaced IDs ("<platform-or-slug>:<id>") keep them unique across sites.
# ===========================================================================

# ---- Phenom (PwC and any other Phenom site, e.g. BCG) ----
def fetch_phenom(company: str, domain: str, size: int = 100, idns: str = None):
    idns = idns or domain.split(".")[0]
    sess = new_session()
    sess.headers.update({"Accept": "application/json, text/plain, */*"})
    refnum, csrf = None, None
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
        print(f"  ({company}) phenom page preload note: {e}")
    if not refnum:
        # Fallback guess some Phenom sites accept; discovery will reveal if wrong.
        refnum = (idns + idns + "GLOBAL").upper()

    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["x-csrf-token"] = csrf
    payload = {
        "lang": "en_global", "deviceType": "desktop", "country": "global",
        "pageName": "search-results", "size": size, "from": 0,
        "jobs": True, "counts": True, "all_fields": ["category", "country", "city", "type"],
        "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
        "pageId": "page1", "siteType": "external", "keywords": "", "global": True,
        "selected_fields": {}, "sort": {"order": "desc", "field": "postedDate"},
        "locationData": {}, "refNum": refnum, "ddoKey": "refineSearch",
    }
    r = sess.post(f"https://{domain}/widgets", json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    rs = data.get("refineSearch") or data.get("eagerLoadRefineSearch") or {}
    raw = (rs.get("data", {}) or {}).get("jobs", []) or []
    jobs = []
    for j in raw:
        jid = j.get("jobId") or j.get("id") or j.get("jobSeqNo")
        if not jid:
            continue
        url = j.get("applyUrl") or j.get("jobUrl") or f"https://{domain}/global/en/job/{jid}"
        loc = (j.get("cityStateCountry") or j.get("location")
               or ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x))
        jobs.append({
            "id": f"{idns}:{jid}",
            "title": (j.get("title") or "").strip(),
            "url": url, "location": loc,
            "description": clean_text(j.get("description") or j.get("descriptionTeaser") or ""),
            "company": company,
            "_detail_url": j.get("jobUrl") or url,
            "_detail_kind": "phenom",
        })
    return jobs


def fetch_pwc(company="PwC Acceleration Centres"):
    return fetch_phenom(company, "jobs-ta.pwc.com", idns="pwc")


# ---- Deloitte USI (Avature, HTML) ----
def fetch_deloitte(company="Deloitte USI", seen=None, max_pages=5):
    seen = seen or {}
    base = "https://usijobs.deloitte.com"
    sess = new_session()
    jobs, found = [], set()
    for page in range(max_pages):
        url = (f"{base}/en_US/careersUSI/SearchJobs/"
               f"?jobRecordsPerPage=10&jobOffset={page*10}")
        try:
            r = sess.get(url, timeout=30); r.raise_for_status()
        except Exception as e:
            print(f"  (Deloitte USI) page {page+1} failed: {e}"); break
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
                "id": f"deloitte:{jid}", "title": a.get_text(strip=True),
                "url": full, "location": "", "description": "", "company": company,
                "_detail_url": full, "_detail_kind": "deloitte",
            })
            if f"deloitte:{jid}" not in seen:
                page_new += 1
        if page_new == 0 and seen:
            break
    return jobs


# ---- Workday (generic CXS JSON API) ----
def fetch_workday(company, tenant, dc, site, limit=20):
    base = f"https://{tenant}.{dc}.myworkdayjobs.com"
    cxs = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    sess = new_session()
    # Workday's CXS endpoint rejects requests that don't look like they came
    # from the site itself; a matching Referer/Origin + JSON headers fix the 400.
    sess.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": base,
        "Referer": f"{base}/{site}",
    })
    payload = {"appliedFacets": {}, "limit": limit, "offset": 0, "searchText": ""}
    r = sess.post(cxs, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    jobs = []
    for p in data.get("jobPostings", []) or []:
        path = p.get("externalPath", "")
        jid = path.rstrip("/").split("/")[-1] or p.get("bulletFields", [""])[0]
        url = base + path if path else base
        jobs.append({
            "id": f"{tenant}:{jid}", "title": (p.get("title") or "").strip(),
            "url": url, "location": p.get("locationsText", ""),
            "description": "", "company": company,
            "_detail_url": f"{base}/wday/cxs/{tenant}/{site}{path}",
            "_detail_kind": "workday",
        })
    return jobs


# ---- Greenhouse / Lever / Ashby / SmartRecruiters (clean public APIs) ----
def fetch_greenhouse(company, token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": f"gh-{token}:{j.get('id')}", "title": (j.get("title") or "").strip(),
            "url": j.get("absolute_url", ""), "location": (j.get("location") or {}).get("name", ""),
            "description": clean_text(j.get("content", "")), "company": company,
        })
    return out


def fetch_lever(company, token):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30); r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories", {}) or {}
        out.append({
            "id": f"lever-{token}:{j.get('id')}", "title": (j.get("text") or "").strip(),
            "url": j.get("hostedUrl", ""), "location": cats.get("location", ""),
            "description": j.get("descriptionPlain", "") or clean_text(j.get("description", "")),
            "company": company,
        })
    return out


def fetch_ashby(company, token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30); r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "id": f"ashby-{token}:{j.get('id')}", "title": (j.get("title") or "").strip(),
            "url": j.get("jobUrl") or j.get("applyUrl", ""), "location": j.get("location", ""),
            "description": j.get("descriptionPlain", "") or clean_text(j.get("descriptionHtml", "")),
            "company": company,
        })
    return out


def fetch_smartrecruiters(company, token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30); r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location", {}) or {}
        locs = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
        out.append({
            "id": f"sr-{token}:{j.get('id')}", "title": (j.get("name") or "").strip(),
            "url": (j.get("ref") or {}).get("jobAd", "") or f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}",
            "location": locs, "description": "", "company": company,
            "_detail_url": f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j.get('id')}",
            "_detail_kind": "smartrecruiters",
        })
    return out


# ---- LinkedIn guest fallback (best-effort; may be blocked from CI IPs) ----
def fetch_linkedin(company, location, pages=2):
    import time, random
    sess = new_session()
    sess.headers.update({"Accept": "text/html"})
    out, seen_ids = [], set()
    cname = company.split(" (")[0].lower()  # drop parentheticals like "(PwC)"
    for page in range(pages):
        url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
               f"?keywords={quote_plus(company)}&location={quote_plus(location)}"
               f"&start={page*25}")
        # Small randomized pause spreads requests out; LinkedIn throttles bursts
        # from one IP (the cause of the 429 cascade across many companies).
        time.sleep(random.uniform(1.5, 3.5))
        r = None
        for attempt in range(2):
            try:
                r = sess.get(url, timeout=30)
            except Exception as e:
                print(f"  ({company}) linkedin failed: {e}"); r = None; break
            if r.status_code == 429 and attempt == 0:
                time.sleep(8)   # back off once, then retry
                continue
            break
        if r is None:
            break
        if r.status_code != 200:
            if page == 0:
                print(f"  ({company}) linkedin returned HTTP {r.status_code} "
                      f"(often a block from CI IPs)")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("li")
        if not cards:
            break
        for li in cards:
            a = li.select_one("a.base-card__full-link") or li.select_one("a[href*='/jobs/view/']")
            if not a:
                continue
            href = a.get("href", "").split("?")[0]
            m = re.search(r"/jobs/view/(?:.*-)?(\d+)", href) or re.search(r"(\d{8,})", href)
            jid = m.group(1) if m else href
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            title = (li.select_one("h3") or a).get_text(strip=True)
            listed_co = (li.select_one("h4") or li.select_one(".base-search-card__subtitle"))
            listed_co = listed_co.get_text(strip=True) if listed_co else ""
            loc = li.select_one(".job-search-card__location")
            loc = loc.get_text(strip=True) if loc else location
            # Keep only cards whose listed company reasonably matches the target,
            # so a search for one firm doesn't email jobs from another.
            if listed_co and cname.split()[0] not in listed_co.lower() \
               and listed_co.lower().split()[0] not in cname:
                continue
            out.append({
                "id": f"li:{jid}", "title": title,
                "url": f"https://www.linkedin.com/jobs/view/{jid}/",
                "location": loc, "description": "",
                "company": f"{company} (via LinkedIn)",
                "_detail_url": f"https://www.linkedin.com/jobs/view/{jid}/",
                "_detail_kind": "linkedin",
            })
    return out


# ===========================================================================
# Detail fetchers — pull the FULL posting text for NEW jobs only.
# ===========================================================================
def _jsonld_description(soup):
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        flat = []
        for it in items:
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                flat.extend(it["@graph"])
            else:
                flat.append(it)
        for it in flat:
            if isinstance(it, dict) and str(it.get("@type", "")).lower() == "jobposting" \
               and it.get("description"):
                return clean_text(it["description"])
    return ""


def fill_description(job, sess):
    kind = job.get("_detail_kind")
    url = job.get("_detail_url") or job.get("url")
    if not kind or not url:
        return
    try:
        if kind == "workday":
            r = sess.get(url, headers={"Accept": "application/json"}, timeout=30)
            r.raise_for_status()
            d = r.json()
            desc = (d.get("jobPostingInfo", {}) or {}).get("jobDescription", "")
            if desc:
                job["description"] = clean_text(desc)
            return
        if kind == "smartrecruiters":
            r = sess.get(url, timeout=30); r.raise_for_status()
            d = r.json()
            parts = []
            ja = (d.get("jobAd", {}) or {}).get("sections", {}) or {}
            for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
                txt = (ja.get(key, {}) or {}).get("text", "")
                if txt:
                    parts.append(txt)
            if parts:
                job["description"] = clean_text(" ".join(parts))
            return
        # phenom, deloitte, linkedin -> HTML page; try JSON-LD then visible text
        r = sess.get(url, timeout=30); r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        desc = _jsonld_description(soup)
        if not desc:
            for t in soup(["script", "style", "nav", "header", "footer"]):
                t.decompose()
            desc = clean_text(soup.get_text(" ", strip=True), 6000)
        if desc and len(desc) > len(job.get("description", "")):
            job["description"] = desc
    except Exception as e:
        print(f"  ({job.get('company')}) detail fetch failed for {job['id']}: {e}")


# ===========================================================================
# Dispatcher
# ===========================================================================
def fetch_company(c, cfg, seen):
    """Return (jobs, ok_bool, detail_msg) for one company config entry."""
    plat = c.get("platform")
    name = c.get("name", "?")
    try:
        if plat == "pwc":
            return fetch_pwc(name), True, ""
        if plat == "deloitte":
            return fetch_deloitte(name, seen, int(cfg.get("deloitte_max_pages", 5))), True, ""
        if plat == "phenom":
            return fetch_phenom(name, c["phenom_domain"],
                                idns=c.get("idns") or c["phenom_domain"].split(".")[0]), True, ""
        if plat == "workday":
            return fetch_workday(name, c["workday_tenant"], c.get("workday_dc", "wd1"),
                                 c["workday_site"]), True, ""
        if plat == "greenhouse":
            return fetch_greenhouse(name, c["token"]), True, ""
        if plat == "lever":
            return fetch_lever(name, c["token"]), True, ""
        if plat == "ashby":
            return fetch_ashby(name, c["token"]), True, ""
        if plat == "smartrecruiters":
            return fetch_smartrecruiters(name, c["token"]), True, ""
        if plat == "linkedin":
            if not cfg.get("linkedin_fallback_enabled", True):
                return [], True, "linkedin disabled"
            return fetch_linkedin(name, cfg.get("linkedin_location", "India"),
                                  int(cfg.get("linkedin_pages", 2))), True, ""
        return [], False, f"unknown platform '{plat}'"
    except KeyError as e:
        return [], False, f"missing config field {e}"
    except Exception as e:
        return [], False, f"{type(e).__name__}: {e}"


def fetch_all(cfg, seen):
    all_jobs, report = [], []
    for c in cfg.get("companies", []):
        if not c.get("enabled", True):
            continue
        name = c.get("name", "?")
        jobs, ok, msg = fetch_company(c, cfg, seen)
        all_jobs += jobs
        report.append((name, c.get("platform"), len(jobs), ok, msg, bool(c.get("verify"))))
        tag = "verify" if c.get("verify") else ""
        status = f"{len(jobs)} jobs" if ok else f"FAIL ({msg})"
        print(f"  [{c.get('platform'):<15}] {name:<32} {status} {tag}")
    return all_jobs, report


# ===========================================================================
# Matching, state, email  (unchanged logic from the single-company version)
# ===========================================================================
def score_job(client, cv, job, exp_min, exp_max):
    prompt = f"""You are screening job postings for a candidate. Compare the candidate's CV with the job posting below.

The candidate is targeting roles that require roughly {exp_min} to {exp_max} years of professional experience.

Return ONLY a JSON object (no markdown), exactly:
{{"score": <integer 0-100>, "experience_fit": <true|false>, "stated_experience": "<years required, or 'not specified'>", "reason": "<one short sentence>"}}

- "score": relevance to this candidate's background/interests (0-100). >70 = worth applying.
- "experience_fit": true if required experience overlaps {exp_min}-{exp_max} years (e.g. "{exp_min}+ years", a band overlapping it, or a seniority level usually needing ~{exp_min}-{exp_max} yrs). false if clearly junior (0-3 yrs) or clearly senior (10+/director). If unstated, infer from seniority.

=== CANDIDATE CV ===
{cv}

=== JOB POSTING ===
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
Description: {job['description'][:5000]}
"""
    resp = client.messages.create(model=MODEL, max_tokens=250,
                                  messages=[{"role": "user", "content": prompt}])
    text = re.sub(r"^```(?:json)?|```$", "", resp.content[0].text.strip()).strip()
    try:
        d = json.loads(text)
        return (int(d.get("score", 0)), bool(d.get("experience_fit", False)),
                str(d.get("stated_experience", "")), str(d.get("reason", "")))
    except Exception:
        return (0, False, "", "Could not parse model response.")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return None


def save_state(seen):
    STATE_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True))


def send_email(subject, html_body):
    api_key = os.environ["RESEND_API_KEY"]
    to = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "onboarding@resend.dev")
    r = requests.post("https://api.resend.com/emails",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      json={"from": from_addr, "to": [to], "subject": subject, "html": html_body},
                      timeout=30)
    r.raise_for_status()


def build_digest(matches):
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
  <p style="color:#999;font-size:12px;margin-top:20px">Sent by your Job Watcher</p>
</div>"""


# ===========================================================================
# Orchestration
# ===========================================================================
def main():
    test = "--test" in sys.argv
    discover = "--discover" in sys.argv
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    threshold = int(cfg.get("match_threshold", 70))
    exp_min = int(cfg.get("experience_min_years", 5))
    exp_max = int(cfg.get("experience_max_years", 7))

    state = load_state()
    enabled = [c for c in cfg.get("companies", []) if c.get("enabled", True)]
    print(f"Fetching {len(enabled)} enabled companies ...")
    all_jobs, report = fetch_all(cfg, state if state else {})

    if test or discover:
        ok = sum(1 for *_2, o, _m, _v in [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in report] if o)
        print("\n================ DISCOVERY / TEST SUMMARY ================")
        print(f"{'PLATFORM':<16}{'COMPANY':<34}{'JOBS':>5}  STATUS")
        for name, plat, n, okx, msg, ver in sorted(report, key=lambda r: (-r[2], r[0])):
            status = "ok" if okx else f"FAIL: {msg}"
            flag = "  <-- verify" if ver else ""
            print(f"{str(plat):<16}{name[:33]:<34}{n:>5}  {status}{flag}")
        total = sum(r[2] for r in report)
        fails = [r for r in report if not r[3]]
        zero = [r for r in report if r[3] and r[2] == 0]
        print(f"\nTotal jobs fetched: {total} across {len(report)} companies")
        print(f"Companies returning 0 jobs: {len(zero)}  |  hard failures: {len(fails)}")
        if fails:
            print("  Failed:", ", ".join(f"{r[0]} ({r[4]})" for r in fails[:20]))
        print("Note: 0 jobs can mean no openings, a wrong token/site, or a CI-IP block (LinkedIn).")
        return

    today = dt.date.today().isoformat()
    if state is None:
        save_state({j["id"]: today for j in all_jobs})
        print(f"\nBaseline run complete: recorded {len(all_jobs)} current jobs. No email sent.")
        return

    new_jobs = [j for j in all_jobs if j["id"] not in state]
    print(f"\n{len(new_jobs)} new job(s) since last run")

    matches = []
    if new_jobs:
        cv = CV_PATH.read_text()
        client = Anthropic()
        web = new_session()
        for j in new_jobs:
            if j.get("_detail_url"):
                fill_description(j, web)
            score, fit, stated, reason = score_job(client, cv, j, exp_min, exp_max)
            ok = score >= threshold and fit
            print(f"  [{score:>3}|exp:{'Y' if fit else 'N'}|{'MATCH' if ok else 'skip'}] "
                  f"{j['company']} - {j['title']}")
            if ok:
                matches.append({"job": j, "score": score,
                                "stated_experience": stated, "reason": reason})

    for j in all_jobs:
        state.setdefault(j["id"], today)
    save_state(state)

    if matches:
        matches.sort(key=lambda m: m["score"], reverse=True)
        n = len(matches)
        send_email(f"{n} new job match{'es' if n != 1 else ''}", build_digest(matches))
        print(f"\nEmailed {n} match(es).")
    else:
        print("\nNo new matches above threshold; no email sent.")


if __name__ == "__main__":
    main()
