#!/usr/bin/env python3
"""Export the live Hermes cron jobs into version-controlled JSON.

Hermes keeps every job in a single ~/.hermes/cron/jobs.json that mixes the
definition (schedule, prompt, script) with mutable run state (last_status,
next_run_at, fire_claim). Committing that file whole would produce a diff on
every tick and would publish the WhatsApp group IDs jobs deliver into.

This writes one file per job under jobs/, keeping only the fields that define
what the job *is*, and replaces each concrete delivery target with a token
resolved through jobs/targets.local.json (gitignored). Run it after changing a
job with `hermes cron edit` to bring the repo back in sync.

    python export_jobs.py            # write jobs/*.json
    python export_jobs.py --check    # exit 1 if the repo is out of date
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
JOBS_DIR = REPO / "jobs"
LIVE_JOBS = Path(os.path.expanduser("~/.hermes/cron/jobs.json"))
TARGETS = JOBS_DIR / "targets.local.json"
TARGETS_EXAMPLE = JOBS_DIR / "targets.example.json"
EXPORTIGNORE = JOBS_DIR / ".exportignore"

# Fields that describe the job. Everything else in jobs.json is run state that
# the scheduler rewrites on every tick.
DEFINITION_FIELDS = [
    "name",
    "schedule",
    "schedule_display",
    "repeat",
    "enabled",
    "no_agent",
    "prompt",
    "script",
    "workdir",
    "skill",
    "skills",
    "enabled_toolsets",
    "model",
    "provider",
    "base_url",
    "context_from",
    "monitor_script",
    "monitor_url",
    "deliver",
]

# A WhatsApp identifier: group (@g.us), linked-device (@lid) or plain contact.
# These turn up in `deliver`, in `origin`, and free-hand inside prompts that
# tell the agent which chat to post into, so every exported string is swept.
WA_ID = re.compile(r"\b\d{12,}@(?:g\.us|lid|s\.whatsapp\.net)\b")


def load_ignored():
    """Job names this repo deliberately does not track.

    A job removed from jobs/ but still live in the scheduler would be recreated
    by the next export, so the exclusion has to be recorded rather than implied
    by the file's absence.
    """
    if not EXPORTIGNORE.exists():
        return set()
    names = set()
    for line in EXPORTIGNORE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def load_targets():
    """Map concrete delivery target -> token, e.g. whatsapp:123@g.us -> @brands.

    Bare identifiers are indexed alongside the `whatsapp:`-prefixed form so the
    same map also covers ids that appear inside prompt text.
    """
    if not TARGETS.exists():
        return {}
    data = json.loads(TARGETS.read_text())
    out = {}
    for token, value in data.items():
        out[value] = token
        out[value.split(":", 1)[-1]] = token
    return out


def tokenize(deliver, targets):
    """'local' and 'origin' are not addresses, so they survive verbatim."""
    if not deliver or deliver in ("local", "origin"):
        return deliver
    return targets.get(deliver, deliver)


def scrub(value, targets):
    """Recursively replace every WhatsApp id in a value with its token.

    An id with no entry in targets.local.json becomes @unknown-<last 4 digits>
    rather than passing through, so a target added to a job but never mapped
    cannot silently reach a public commit.
    """
    if isinstance(value, str):
        return WA_ID.sub(
            lambda m: targets.get(m.group(0), f"@unknown-{m.group(0)[:4]}"), value
        )
    if isinstance(value, list):
        return [scrub(v, targets) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, targets) for k, v in value.items()}
    return value


def export():
    if not LIVE_JOBS.exists():
        sys.exit(f"no live jobs file at {LIVE_JOBS}")
    targets = load_targets()
    ignored = load_ignored()
    live = json.loads(LIVE_JOBS.read_text())
    JOBS_DIR.mkdir(exist_ok=True)

    written = {}
    skipped = []
    for job in live["jobs"]:
        if job["name"] in ignored:
            skipped.append(job["name"])
            continue
        out = {k: job[k] for k in DEFINITION_FIELDS if job.get(k) not in (None, [], "")}
        out["deliver"] = tokenize(job.get("deliver"), targets)
        # repeat carries a `completed` counter the scheduler bumps every run;
        # only the cap is part of the definition.
        if isinstance(out.get("repeat"), dict):
            out["repeat"] = {"times": out["repeat"].get("times")}
        # `origin` records the chat the job was created from. The platform is
        # what `deliver: origin` needs; the chat and user ids are personal.
        if job.get("origin"):
            out["origin"] = {"platform": job["origin"].get("platform")}
        out = scrub(out, targets)
        # The id is how `hermes cron` addresses the job, so it is worth keeping,
        # but it sorts last to keep the human-readable fields at the top.
        out["id"] = job["id"]
        written[f"{job['name']}.json"] = json.dumps(out, indent=2) + "\n"

    unresolved = sorted(
        {m for body in written.values() for m in re.findall(r"@unknown-\d+", body)}
    )
    return written, unresolved, sorted(skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if jobs/ is stale")
    args = ap.parse_args()

    written, unresolved, skipped = export()

    if skipped:
        print(f"skipped {len(skipped)} job(s) per jobs/.exportignore: "
              + ", ".join(skipped))

    if unresolved:
        print(
            "warning: unmapped WhatsApp ids were masked as "
            + ", ".join(unresolved)
            + " — add them to jobs/targets.local.json for a readable name",
            file=sys.stderr,
        )

    existing = {p.name: p.read_text() for p in JOBS_DIR.glob("*.json")
                if p.name not in (TARGETS.name, TARGETS_EXAMPLE.name)}

    if args.check:
        if existing != written:
            stale = sorted(set(existing) ^ set(written)) or [
                n for n in written if existing.get(n) != written[n]
            ]
            print("jobs/ is out of date:", ", ".join(stale), file=sys.stderr)
            return 1
        print(f"jobs/ is up to date ({len(written)} jobs)")
        return 0

    for name in set(existing) - set(written):
        (JOBS_DIR / name).unlink()
        print(f"removed {name}")
    for name, body in sorted(written.items()):
        path = JOBS_DIR / name
        if existing.get(name) != body:
            path.write_text(body)
            print(f"wrote {name}")
    print(f"{len(written)} jobs exported")
    return 0


if __name__ == "__main__":
    sys.exit(main())
