#!/usr/bin/env python3
"""social_linkedin_comment_outbound: find LinkedIn posts worth a comment, draft
one in Vishal's voice, and post it only if the relevance judge agrees.

    python run_linkedin_comments.py            # live: posts up to MAX_PER_RUN
    python run_linkedin_comments.py --dry-run  # drafts + judges, posts nothing

Until 2026-09-12 this cron job was an agent prompt that told the model to
"search LinkedIn" with only the terminal and file tools. It could not, so it
answered [SILENT] or wandered off (one run inventoried npm packages). The only
comments it ever produced, on 09-03, were canned lines pasted under unrelated
posts. This script drives linkedin_growth_comments.py's two phases itself:

  find_candidates  -> feed + content search, real URNs, dedupe against the DB
  draft_comment    -> social_voice writes a reply to what the post says, or SKIP
  post_comment     -> judge_relevance >= RELEVANCE_MIN, else rejected, unposted

Caps: MAX_PER_RUN comments per run (the job runs every 6h) and DAILY_CAP per
UTC day, counted from x_growth.db. Quiet runs are the expected outcome while
the target quality holds; volume is not the goal.

The whole run holds the shared Chrome profile lock, because find and post each
open the persistent profile and the other social jobs do too.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == '__main__':
    try:
        import pipeline_log as _plog
        _plog.start('social_linkedin_comment_outbound')
    except Exception:
        pass

from browser_lock import profile_lock, BrowserBusy  # noqa: E402
import linkedin_growth_comments as lgc  # noqa: E402
import social_voice as voice  # noqa: E402

MAX_PER_RUN = int(os.environ.get('LI_COMMENTS_PER_RUN', 2))
DAILY_CAP = int(os.environ.get('LI_COMMENTS_DAILY_CAP', 6))
MAX_CANDIDATES = int(os.environ.get('LI_COMMENT_CANDIDATES', 10))
MIN_REACTIONS = int(os.environ.get('LI_COMMENT_MIN_REACTIONS', 50))
DRY_RUN = '--dry-run' in sys.argv


def posted_today():
    try:
        conn = sqlite3.connect(lgc.DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM linkedin_comments WHERE status='posted' "
            "AND created_at >= date('now')").fetchone()[0]
        conn.close()
        return n
    except sqlite3.Error:
        return 0


def log(msg):
    print(f'[li-comments] {msg}', file=sys.stderr)


def main():
    summary = {'posted': [], 'rejected': [], 'skipped': [], 'errors': [],
               'candidates': 0, 'posted_today_before_run': posted_today(),
               'daily_cap': DAILY_CAP, 'dry_run': DRY_RUN}
    budget = min(MAX_PER_RUN, DAILY_CAP - summary['posted_today_before_run'])
    if budget <= 0 and not DRY_RUN:
        summary['note'] = 'daily cap reached'
        print(json.dumps(summary, indent=2))
        return 0

    try:
        with profile_lock('social_linkedin_comment_outbound', timeout=600):
            candidates = lgc.find_candidates(MAX_CANDIDATES, MIN_REACTIONS)
            summary['candidates'] = len(candidates)
            # Kept for diagnosis: which posts were seen, so a quiet run can be
            # checked against the feed without re-scraping it.
            summary['candidate_list'] = [
                {'author': c.get('author'), 'reactions': c.get('reactions'),
                 'source': c.get('source'), 'url': c.get('url') or c.get('urn')}
                for c in candidates]
            done = 0
            for c in candidates:
                if done >= max(budget, 1 if DRY_RUN else 0):
                    break
                ref = c.get('url') or c.get('urn')
                author = c.get('author', 'Unknown')
                if lgc.is_post_commented(c.get('urn') or ref):
                    summary['skipped'].append({'author': author, 'reason': 'already commented'})
                    continue
                # Draft from the permalink's full text, not the feed-card snippet:
                # the snippet made the voice SKIP every candidate (see fetch_post).
                try:
                    p_author, text, p_url = lgc.fetch_post(ref)
                except Exception as e:  # noqa: BLE001
                    summary['errors'].append({'author': author, 'url': ref, 'reason': f'fetch failed: {type(e).__name__}: {e}'})
                    continue
                if p_author and p_author != 'Unknown':
                    author = p_author
                if len(text) < 80:
                    summary['skipped'].append({'author': author, 'url': ref, 'reason': 'post text not found on permalink or too short'})
                    continue

                drafted = voice.draft_comment(author, text, platform='linkedin')
                if drafted is voice.SKIP or drafted is None:
                    summary['skipped'].append({'author': author, 'reason': 'voice declined (SKIP)' if drafted is voice.SKIP else 'draft call failed',
                                               'post_snippet': text[:120]})
                    continue
                log(f"draft for {author}: {drafted}")

                result = lgc.post_comment(ref, drafted, dry_run=DRY_RUN)
                status = result.get('status')
                entry = {'author': result.get('author') or author, 'url': result.get('url') or ref,
                         'comment': drafted, 'relevance': result.get('relevance'),
                         'post_snippet': (result.get('post_snippet') or text)[:160]}
                if status in ('posted', 'dry_run'):
                    summary['posted'].append(entry)
                    done += 1
                    time.sleep(20)
                elif status == 'rejected':
                    entry['reason'] = result.get('reason')
                    summary['rejected'].append(entry)
                elif status == 'skipped':
                    summary['skipped'].append({'author': author, 'reason': result.get('reason')})
                else:
                    entry['reason'] = result.get('reason')
                    summary['errors'].append(entry)
    except BrowserBusy as e:
        summary['note'] = f'Skipping run: {e}'

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
