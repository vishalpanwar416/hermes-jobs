#!/usr/bin/env python3
"""Gather everything the x_daily_review cron job needs, by code.

Until 2026-09-12 the review was a six-step agent prompt: run x_metrics.py,
read the plan, query x_growth.db, diagnose, optionally edit caps or the target
list, append to X_TUNING_LOG.md, report. gemini-3.5-flash-lite could not carry
that: on 09-11 (Ollama fallback) and again on 09-12 (Gemini, no quota storm)
it read x_targets.json, described the dictionary and asked what to do next,
and the log entry was never written.

So the deterministic parts now happen here, and the agent only writes prose:
  - today's date, the x_metrics.py --report block verbatim
  - every reply posted today from x_growth.db, flagged target / off-list
  - the [judge] lines from today's social_x_reply_outbound pipeline logs
  - tier counts from x_targets.json
  - the previous X_TUNING_LOG.md heading
and it appends today's dated section to X_TUNING_LOG.md itself, numbers only,
"no change". Automated cap changes and candidate promotions are gone on
purpose: Vishal's standing rule is that quality bars do not move by themselves
and tier promotions are his call by hand.
"""
import datetime
import glob
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == '__main__':
    try:
        import pipeline_log as _plog
        _plog.start('x_daily_review')
    except Exception:
        pass

HOME = os.path.expanduser('~')
PY = f'{HOME}/Development/Aarambh/hermes-agent/.venv/bin/python'
SCRIPTS = f'{HOME}/.hermes/scripts'
JOBS_REPO = f'{HOME}/Development/Aarambh/hermes-jobs'
GROWTH_DB = f'{HOME}/.hermes/data/x_growth.db'
TUNING_LOG = f'{JOBS_REPO}/X_TUNING_LOG.md'
TARGETS = f'{JOBS_REPO}/x_targets.json'
CANDIDATES = f'{JOBS_REPO}/x_target_candidates.json'


def sh(cmd, timeout=90):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or '') + (('\n[stderr] ' + r.stderr.strip()) if r.returncode else '')
    except Exception as e:  # noqa: BLE001
        return f'[gather] {cmd[-1]} failed: {type(e).__name__}: {e}'


def target_handles():
    try:
        data = json.load(open(TARGETS))
    except Exception as e:  # noqa: BLE001
        return {}, f'[gather] cannot read x_targets.json: {e}'
    tiers = {k: v for k, v in data.items() if not k.startswith('_') and isinstance(v, list)}
    handles = {}
    for tier, entries in tiers.items():
        for ent in entries:
            h = ent.get('handle') if isinstance(ent, dict) else ent
            if isinstance(h, str):
                handles[h.lstrip('@').lower()] = tier
    counts = ', '.join(f'{t}={len(v)}' for t, v in tiers.items())
    return handles, counts


def replies_today(today, handles):
    try:
        c = sqlite3.connect(GROWTH_DB)
        rows = c.execute(
            "select author, tweet_text, reply_text, status, created_at from auto_replies "
            "where date(created_at) = ? order by created_at", (today,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return f'[gather] x_growth.db query failed: {e}'
    if not rows:
        return '(no replies posted today)'
    out = []
    for author, tweet, reply, status, at in rows:
        tier = handles.get((author or '').lstrip('@').lower())
        tag = f'TARGET {tier}' if tier else 'off-list'
        out.append(f'- {at[11:16]} @{author} [{status}] [{tag}]\n    post : {" ".join((tweet or "").split())[:220]}\n    reply: {" ".join((reply or "").split())[:220]}')
    return '\n'.join(out)


def judge_lines(today):
    files = sorted(glob.glob(f'{HOME}/.hermes/logs/pipelines/social_x_reply_outbound/{today}_*.log'))
    lines = []
    for f in files:
        try:
            lines += [ln.rstrip() for ln in open(f, errors='replace') if '[judge]' in ln]
        except OSError:
            pass
    return '\n'.join(lines[-12:]) if lines else '(no [judge] lines in today\'s reply-job logs: no drafts were scored today)'


def prev_heading():
    try:
        heads = [ln.strip() for ln in open(TUNING_LOG) if ln.startswith('## ')]
        return heads[-1] if heads else '(none)'
    except OSError:
        return '(log missing)'


def append_log(now, metrics, replies, tiers):
    section = (
        f"\n## {now:%Y-%m-%d %H:%M} — automated daily review, numbers by code\n\n"
        f"```\n{metrics.strip()}\n```\n\n"
        f"Target list: {tiers}. Replies posted today:\n{replies}\n\n"
        "Change: none. The automated review only reports since 2026-09-12; caps, RELEVANCE_MIN "
        "and tier promotions are changed by hand.\n"
    )
    try:
        with open(TUNING_LOG, 'a') as fh:
            fh.write(section)
        return 'appended'
    except OSError as e:
        return f'FAILED: {e}'


def main():
    now = datetime.datetime.now()
    today = now.strftime('%Y-%m-%d')
    metrics = sh([PY, f'{SCRIPTS}/x_metrics.py', '--report'])
    handles, tiers = target_handles()
    replies = replies_today(today, handles)
    judge = judge_lines(today)
    prev = prev_heading()
    t1 = sum(1 for t in handles.values() if t == 'tier_1_inner')
    try:
        cand = json.load(open(CANDIDATES))
        n_cand = len([k for k in cand if not str(k).startswith('_')]) if isinstance(cand, dict) else len(cand)
    except Exception:  # noqa: BLE001
        n_cand = 'n/a'
    logged = append_log(now, metrics, replies, tiers)

    print(f"=== DATE ===\n{now:%A %Y-%m-%d %H:%M IST}")
    print(f"\n=== X_METRICS_REPORT (verbatim, quote these numbers only) ===\n{metrics.strip()}")
    gate = (f"GATE LINE (copy verbatim): Tier 1 has {t1} of the 15 handles the plan requires; "
            f"the gate stays closed until Vishal seeds tier 1 by hand." if t1 < 15
            else "GATE LINE: tier 1 is fully seeded (15+ handles); the gate is open.")
    print(f"\n=== TARGET_LIST ===\n{tiers}\n{gate}\ncandidates awaiting Vishal: {n_cand}")
    print(f"\n=== REPLIES_POSTED_TODAY ===\n{replies}")
    print(f"\n=== JUDGE_LINES_TODAY ===\n{judge}")
    print(f"\n=== TUNING_LOG ===\nprevious heading: {prev}\ntoday's section: {logged} to {TUNING_LOG}")
    print("\n=== END ===")


if __name__ == '__main__':
    main()
