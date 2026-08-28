"""Analyse the Naukri profile against real job demand, and optionally update it.

Naukri recruiter search is keyword driven, and it ranks recently updated profiles
higher. So there are two separate levers:

  CONTENT   does the profile contain the keywords recruiters actually search for,
            in the roles Vishal is actually applying to
  FRESHNESS a profile touched recently surfaces above an identical stale one

Demand is measured from the 600+ Naukri and Indeed descriptions already in
jobs_tracker.db, not from guesses about what is hot.

THE HARD RULE: a skill is only ever suggested if it appears in VERIFIED_SKILLS.
Azure, Spring and Django are all in heavy demand and none of them go on this
profile, because he has not used them. Padding a professional profile with
keywords he cannot answer questions about fails at the interview instead of the
search, which is worse.

Usage:
    python naukri_profile_optimizer.py                 # analyse, change nothing
    python naukri_profile_optimizer.py --apply         # apply the safe updates
    python naukri_profile_optimizer.py --refresh-only  # just bump freshness
"""

import os
import re
import sys
import json
import sqlite3
import argparse
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/naukri_profile_optimizer/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('naukri_profile_optimizer')
    except Exception:
        pass

import naukri_auto_apply as base

from playwright.sync_api import sync_playwright

JOBS_DB = os.path.expanduser('~/.hermes/data/jobs_tracker.db')
PROFILE_URL = 'https://www.naukri.com/mnjuser/profile'

# Everything Vishal has genuinely worked with. Nothing outside this list is ever
# proposed, no matter how much demand it shows.
VERIFIED_SKILLS = {
    'golang', 'go', 'node.js', 'node', 'python', 'fastapi', 'javascript',
    'typescript', 'react.js', 'react', 'next.js', 'nextjs', 'kubernetes', 'k8s',
    'docker', 'terraform', 'gcp', 'aws', 'postgresql', 'postgres', 'mysql',
    'mongodb', 'redis', 'microservices', 'rest api', 'grpc', 'ci/cd', 'devops',
    'sre', 'linux', 'git', 'system design', 'distributed systems', 'rag', 'llm',
    'backend', 'front end',
}

# Skills the profile lists under slightly different names.
ALIASES = {
    'react.js': 'react', 'node.js': 'node', 'postgresql': 'postgres',
    'gcp cloud': 'gcp', 'nextjs': 'next', 'next.js': 'next', 'k8s': 'kubernetes',
    'go': 'golang',
}

# Naukri's icon spans leak into the chip list as pseudo-skills.
UI_ARTIFACTS = re.compile(r'OneTheme$|^edit$|^add$|^delete$', re.I)

DEMAND_TERMS = [
    'golang', 'go', 'node', 'react', 'next', 'typescript', 'javascript', 'python',
    'java', 'kubernetes', 'docker', 'terraform', 'aws', 'gcp', 'azure',
    'microservices', 'postgres', 'mysql', 'mongodb', 'redis', 'kafka', 'grpc',
    'rest api', 'system design', 'ci/cd', 'devops', 'sre', 'fastapi', 'django',
    'spring', 'graphql', 'elasticsearch', 'rabbitmq', 'linux', 'distributed',
]


def _norm(s):
    s = (s or '').strip().lower()
    return ALIASES.get(s, s)


# ---------------------------------------------------------------------------
# demand
# ---------------------------------------------------------------------------

def measure_demand(sources=('naukri', 'indeed')):
    conn = sqlite3.connect(JOBS_DB)
    marks = ','.join('?' * len(sources))
    rows = conn.execute(
        f"SELECT role_title, job_description FROM job_openings WHERE source IN ({marks})",
        tuple(sources)).fetchall()
    conn.close()
    blob = ' '.join(f"{a or ''} {b or ''}" for a, b in rows).lower()
    demand = Counter()
    for term in DEMAND_TERMS:
        n = len(re.findall(r'(?<![a-z])' + re.escape(term) + r'(?![a-z])', blob))
        if n:
            demand[term] = n
    return demand, len(rows)


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def read_profile(page):
    page.goto(PROFILE_URL, timeout=60000, wait_until='domcontentloaded')
    page.wait_for_timeout(9000)
    if 'login' in page.url.lower():
        raise RuntimeError('NAUKRI_NOT_LOGGED_IN')

    # Read the headline from the profile card, NOT the Quick links sidebar.
    # The sidebar lists "Resume headline" as a menu item, followed by "Key skills"
    # on the next line — that is never the actual headline text.
    body = page.inner_text('body')
    headline = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('.card.mt15, .card')];
      for (const card of cards) {
        const title = card.querySelector('.widgetTitle');
        if (title && (title.textContent||'').trim().toLowerCase() === 'resume headline') {
          const div = card.querySelector('.prefill div, .prefill');
          if (div) return (div.textContent||'').trim();
        }
      }
      return '';
    }""")

    # Read the skill chips as DOM elements. Naukri renders them with no
    # separator in innerText ("DevOpsGCP CloudAWS..."), and splitting that on
    # camel case shreds compound names: DevOps became Dev + Ops, TypeScript
    # became Type + Script, MongoDB became Mongo + DBPostgresql. The profile
    # then looked like it was missing skills it already had.
    skills = page.evaluate("""() => {
      const heads = [...document.querySelectorAll('span,div,h2,h3,label')]
        .filter(e => (e.textContent || '').trim().startsWith('Key skills')
                     && e.children.length < 6
                     && !e.closest('.quickLink'));
      for (const h of heads) {
        let sec = h.closest('div');
        for (let i = 0; i < 6 && sec; i++) {
          const kids = [...sec.querySelectorAll('span,li,a')]
            .filter(e => e.children.length === 0)
            .map(e => (e.textContent || '').trim())
            .filter(t => t && t.length < 28 && !t.startsWith('Key skills')
                         && !/OneTheme$/.test(t));
          if (kids.length >= 5) return [...new Set(kids)];
          sec = sec.parentElement;
        }
      }
      return [];
    }""")

    skills = [s.strip() for s in skills
              if s.strip() and not UI_ARTIFACTS.search(s.strip())]
    return {'headline': headline, 'skills': skills, 'body': body}


def analyse(profile, demand, total_jobs):
    have = {_norm(s) for s in profile['skills']}
    headline_l = (profile['headline'] or '').lower()

    missing = []
    for term, count in demand.most_common():
        t = _norm(term)
        if t in have:
            continue
        if t not in VERIFIED_SKILLS:
            continue  # in demand but not his, never suggest
        missing.append({'skill': term, 'demand': count})

    not_his = [{'skill': t, 'demand': c} for t, c in demand.most_common()
               if _norm(t) not in have and _norm(t) not in VERIFIED_SKILLS][:6]

    # Is the headline selling what he is applying for?
    top_lang = max(('golang', 'python', 'node'), key=lambda k: demand.get(k, 0))
    headline_issues = []
    if 'golang' not in headline_l and 'go ' not in headline_l and demand.get('golang', 0) > 80:
        headline_issues.append(
            f"Golang appears in {demand.get('golang')} of the roles you target but is "
            f"absent from the headline, which recruiters search on.")
    if 'mern' in headline_l and demand.get('golang', 0) > demand.get('react', 0) * 0.5:
        headline_issues.append(
            "Headline leads with MERN Stack while you are applying to backend and "
            "Golang roles, so it filters you out of the searches you want.")
    if 'sre' in headline_l or 'devops' in headline_l:
        headline_issues.append(
            "Headline foregrounds SRE/DevOps. That is a different recruiter pool "
            "than backend engineer, and it splits your relevance in both.")

    return {
        'jobs_analysed': total_jobs,
        'current_headline': profile['headline'],
        'current_skills': profile['skills'],
        'add_skills': missing,
        'high_demand_but_not_yours': not_his,
        'headline_issues': headline_issues,
    }


def suggest_headline(analysis, demand):
    """Only a suggestion. Never applied without --apply."""
    return ('Backend Engineer | Golang, Node.js, Python | Kubernetes, Docker, GCP & AWS | '
            'Microservices, Distributed Systems | 2+ Years Experience')


# ---------------------------------------------------------------------------
# updates
# ---------------------------------------------------------------------------


def _click_save(page, timeout_ms=15000):
    """Click the modal's Save button.

    page.locator('button:has-text("Save")').first timed out even though a Save
    button was present: the match includes hidden/disabled candidates, and the
    real one can sit below the fold inside the modal. Pick the visible enabled
    one, scroll it into view, and fall back to a direct DOM click.
    """
    try:
        ok = page.evaluate("""() => {
          const btns=[...document.querySelectorAll('button')]
            .filter(b => b.offsetParent !== null && !b.disabled
                         && (b.textContent||'').trim().toLowerCase() === 'save');
          if(!btns.length) return false;
          btns[0].scrollIntoView({block:'center'});
          btns[0].click();
          return true;
        }""")
        if ok:
            page.wait_for_timeout(4000)
            return True, 'saved'
        return False, 'no visible enabled Save button'
    except Exception as e:
        return False, f'save failed: {str(e)[:90]}'


def add_skills(page, skills):
    """Append skills to Key skills. Additive only, never removes."""
    done = []
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until='domcontentloaded')
        page.wait_for_timeout(7000)
        # Scope the edit click to the Key skills CARD. Using .nth(1) on a
        # page-wide edit selector opened whichever editor happened to be second
        # in the DOM, so the skills input was never on screen.
        opened = page.evaluate("""() => {
          const heads=[...document.querySelectorAll('span,div,h2,h3')]
            .filter(e=>(e.textContent||'').trim().startsWith('Key skills')
                       && e.children.length<6 && !e.closest('.quickLink'));
          if(!heads.length) return false;
          const card=heads[0].closest('div');
          const ed=[...card.querySelectorAll('span,em,a,button')]
            .filter(e=>/edit/i.test(e.className||''));
          if(!ed.length) return false;
          ed[0].click(); return true;
        }""")
        if not opened:
            return done, 'Key skills edit control not found'
        page.wait_for_timeout(4000)
        box = page.locator('input#keySkillSugg, input[placeholder="Add skills"]').first
        if not box.count():
            return done, 'skill input not found'
        for s in skills:
            box.click()
            box.fill('')
            box.type(s, delay=60)
            page.wait_for_timeout(1500)
            # Naukri only commits a chip picked from the autocomplete dropdown;
            # a typed value plus Enter stays uncommitted and Save drops it.
            # Click the matching suggestion, falling back to ArrowDown+Enter
            # (which also selects the highlighted suggestion) if none is found.
            picked = page.evaluate("""(skill) => {
              const boxes=[...document.querySelectorAll(
                '[id*="sugDrp" i], [class*="sugg" i], [class*="Sbtn" i], ul[class*="sug" i]')];
              const items=boxes.flatMap(b=>[...b.querySelectorAll('li,div[role="option"]')])
                .filter(e=>e.offsetParent!==null && (e.textContent||'').trim());
              if(!items.length) return false;
              const t=skill.toLowerCase();
              const hit=items.find(e=>e.textContent.trim().toLowerCase()===t)
                     || items.find(e=>e.textContent.trim().toLowerCase().includes(t))
                     || items[0];
              hit.click(); return true;
            }""", s)
            if not picked:
                page.keyboard.press('ArrowDown')
                page.wait_for_timeout(400)
                page.keyboard.press('Enter')
            page.wait_for_timeout(800)
            done.append(s)
        ok, detail = _click_save(page)
        if not ok:
            return done, detail
        # Verify by re-reading. Clicking Save is not proof: Naukri's skill field
        # needs the autocomplete suggestion picked, and typing plus Enter alone
        # leaves the chip uncommitted, so Save persists nothing while the click
        # itself still succeeds.
        page.wait_for_timeout(2500)
        after = read_profile(page)
        have = {_norm(x) for x in after['skills']}
        landed = [s for s in done if _norm(s) in have]
        if not landed:
            return [], ('save clicked but no skill persisted; the autocomplete '
                        'suggestion likely needs to be selected, not just typed')
        return landed, f'saved {len(landed)} of {len(done)}'
    except Exception as e:
        return done, f'error: {str(e)[:110]}'


def refresh_profile(page):
    """Re-save the profile so Naukri marks it recently updated.

    Naukri ranks a freshly updated profile above an identical stale one in
    recruiter search, so this is worth doing on its own even with no content
    change. Re-saves the headline with its existing text.
    """
    try:
        page.goto(PROFILE_URL, timeout=60000, wait_until='domcontentloaded')
        page.wait_for_timeout(7000)
        edit = page.locator('em.edit, span.edit, [class*="edit"]').first
        if not edit.count():
            return False, 'edit control not found'
        edit.click()
        page.wait_for_timeout(3000)
        ok, detail = _click_save(page)
        if not ok:
            try:
                page.keyboard.press('Escape')
            except Exception:
                pass
            return False, detail
        return True, 'profile re-saved, freshness bumped'
    except Exception as e:
        return False, f'error: {str(e)[:110]}'


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def run(apply_changes=False, refresh_only=False):
    demand, total = measure_demand()
    out = {'mode': 'apply' if apply_changes else ('refresh' if refresh_only else 'analyse')}

    lock = base.acquire_browser_lock(timeout_secs=900)
    xvfb, display = base.start_xvfb()
    os.environ['DISPLAY'] = display
    os.environ.pop('WAYLAND_DISPLAY', None)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=base.PROFILE_DIR, channel='chrome', headless=False,
                locale='en-IN', timezone_id='Asia/Kolkata',
                viewport={'width': 1440, 'height': 900},
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
            page = ctx.new_page()
            try:
                profile = read_profile(page)
            except RuntimeError as e:
                ctx.close()
                return {'error': str(e),
                        'action': 'run naukri_login.py from a desktop session'}

            analysis = analyse(profile, demand, total)
            out.update(analysis)
            out['suggested_headline'] = suggest_headline(analysis, demand)

            if refresh_only:
                ok, detail = refresh_profile(page)
                out['refresh'] = detail
            elif apply_changes:
                to_add = [s['skill'] for s in analysis['add_skills']][:5]
                if to_add:
                    added, detail = add_skills(page, to_add)
                    out['skills_added'] = added
                    out['skills_result'] = detail
                ok, detail = refresh_profile(page)
                out['refresh'] = detail
                # The headline is never auto-written: it is the single most
                # visible line on the profile and a bad one costs more than a
                # missing keyword. Suggested only.
                out['headline_note'] = 'suggested only, apply manually if you agree'
            ctx.close()
    finally:
        xvfb.terminate()
        lock.close()
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true', help='add missing skills and refresh')
    ap.add_argument('--refresh-only', action='store_true', help='only bump freshness')
    args = ap.parse_args()
    print(json.dumps(run(apply_changes=args.apply, refresh_only=args.refresh_only),
                     indent=2, ensure_ascii=False))
