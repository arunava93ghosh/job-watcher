# COMPANIES_STATUS — current state after the first discovery run

Discovery on GitHub returned **483 jobs across 28 working companies**. This doc
records what's confirmed, what was fixed, and what to do next.

## Tier A — direct feeds
| Company | Platform | Status |
|---|---|---|
| PwC Acceleration Centres | Phenom (bespoke) | LIVE, verified (100 jobs) |
| Deloitte USI | Avature (bespoke) | LIVE, verified (50 jobs) |
| Boston Consulting Group | Phenom `careers.bcg.com` | CONFIRMED in discovery (100 jobs) |
| Marsh McLennan (Marsh / Mercer / Oliver Wyman) | Phenom `careers.marsh.com` | NEW — verify next run |
| Accenture | Workday `accenture.wd103` / `AccentureCareers` | NEW — verify next run |
| IQVIA | Workday `iqvia.wd1` / `IQVIA` | bug fixed (was 400) — verify next run |

Note: the standalone LinkedIn entries for Marsh, Mercer, and Oliver Wyman are now
DISABLED, because the Marsh McLennan Phenom feed covers all three (no duplicates).

## Tier B — LinkedIn fallback (53 firms, best-effort)
Working in discovery (returned jobs): McKinsey (18), Bain (18), EY (18),
EY-Parthenon (18), KPMG (19), ZS Associates (18), Clarivate (20), BDO (20),
Marsh*, Grant Thornton (10), Oliver Wyman*, Kearney (4), Cytel (10),
Praxis Global Alliance (10), Everest Group (7), Ankura (4), Putnam (3), WTW (3),
Mercer*, L.E.K. (2), FTI (1), ISG (1), Korn Ferry (1), NelsonHall (1).
(* now served by the Marsh McLennan direct feed instead.)

### The LinkedIn 429 issue (important and expected)
In discovery, the first ~17 LinkedIn calls worked, then LinkedIn throttled
GitHub's IP and **every company after that got HTTP 429** (Accenture, Aon, Kroll,
Cognizant, Capco, GEP, etc. all showed 0 for this reason — not because they have
no jobs). Fixes applied:
- `main.py` now **pauses 1.5–3.5s between LinkedIn calls and retries once after a
  429** (8s backoff). This reduces, but cannot fully eliminate, IP throttling.
- The biggest firms are being **moved to direct feeds** (Accenture done; more
  below), which sidesteps LinkedIn entirely.

## What to do next
1. **Re-run discovery** to confirm the three new/fixed direct feeds (Marsh
   McLennan, Accenture, IQVIA) now return jobs, and to see how many more
   LinkedIn firms succeed with the new pacing.
2. **Send me the new table.** Next direct-feed candidates I can research/add
   (all currently best-effort): Aon, WTW, Kroll, FTI Consulting,
   Alvarez & Marsal, Grant Thornton, Capgemini, NTT DATA, Protiviti, GEP.
   (Cognizant's own jobs are on a custom site, not a clean API — likely stays
   LinkedIn/native-alert.)

## How to add a direct feed yourself
Same as before — identify the platform from the careers-page URL and edit
`config.yaml`:
- `myworkdayjobs.com` → `platform: workday` with `workday_tenant` / `workday_dc`
  (the `wd1`/`wd103`/etc part) / `workday_site` (case-sensitive, from the URL).
- `/global/en/search-results` or `phenompeople` → `platform: phenom`,
  `phenom_domain: careers.<firm>.com`.
- `boards.greenhouse.io/TOKEN` → greenhouse; `jobs.lever.co/TOKEN` → lever;
  `jobs.ashbyhq.com/TOKEN` → ashby; `jobs.smartrecruiters.com/TOKEN` → smartrecruiters.
Then re-run discovery to confirm the count is > 0. Or send me the URL and I'll
give you the exact lines.

## If LinkedIn stays heavily blocked
For boutiques with no clean feed (McKinsey, Bain, EY, Primus, Praxis, Avalon,
Acuvon, Kanvic, Redseer, Zinnov, Nexdigm), the most reliable option is a native
**LinkedIn job alert** (LinkedIn → Jobs → search the firm → Set alert), which
emails you directly and isn't subject to the CI-IP limit.
