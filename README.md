# hermes-jobs

The automation that runs on top of [Hermes](https://github.com/NousResearch/hermes-agent): 20 scheduled jobs that apply to job postings, publish and reply to social posts, and keep API tokens alive — plus a small web UI for reading what they actually did.

Hermes itself keeps jobs in a single mutable `~/.hermes/cron/jobs.json` and their scripts loose in `~/.hermes/scripts`, neither of which is version controlled. This repo is the source of truth for both.

## Layout

```
jobs/            job definitions, one JSON per job, exported from the live scheduler
scripts/         every script the jobs run, plus the run_*.sh wrappers
ui/              read-only log viewer (FastAPI + one HTML page)
export_jobs.py   live jobs.json -> jobs/*.json
sync.sh          repo scripts/ -> ~/.hermes/scripts
```

## The log viewer

```sh
~/Development/Aarambh/hermes-agent/.venv/bin/python ui/server.py
# http://127.0.0.1:8787
```

`hermes cron list` gives you one line of status per job. When one fails, the detail is scattered across three places:

| Source | Holds |
|---|---|
| `~/.hermes/cron/executions.db` | one row per attempt: status, timings, the exception |
| `~/.hermes/logs/pipelines/<pipeline>/` | everything the script printed, one file per run |
| `~/.hermes/cron/output/<job_id>/` | the agent's final response text |

The viewer merges all three into a single timeline per job, newest first, with failing jobs sorted to the top. Click a run to read it.

It is strictly read-only — it opens the sqlite database with `immutable=1`, so refreshing the page can never block or disturb the running scheduler, and it has no endpoint that writes anything. Use `hermes cron` to actually change a job. It binds to `127.0.0.1` and serves files only from under the two log roots.

Requires `fastapi` and `uvicorn`, both already present in the Hermes venv.

### Mapping jobs to pipelines

A job's logs do not always live under its own name — `naukri_recommended_auto_apply` runs `naukri_auto_apply.py`, which logs to the `naukri_auto_apply` pipeline. Rather than guessing from the names, the viewer reads the mapping out of the code the job runs: `PIPE_NAME` for a shell wrapper, the module stem for a Python entrypoint, and the filename in the prompt for an agent job. Pipelines with no matching job are listed separately instead of being hidden.

## Editing a job

Job definitions are edited through Hermes, not by hand — the scheduler owns `jobs.json` and rewrites it on every tick.

```sh
hermes cron edit <job-id>       # change it live
python export_jobs.py           # bring jobs/ back in sync, then commit
python export_jobs.py --check   # exit 1 if the repo is stale (CI-friendly)
```

`export_jobs.py` keeps only the fields that define what a job *is*. Run state — `last_status`, `next_run_at`, `fire_claim`, `repeat.completed` — is dropped, so the export is stable between ticks and a diff only appears when something real changed.

### Delivery targets

This repo is public, so the WhatsApp group IDs that jobs deliver into are not committed. Each is replaced by a token (`@brands`, `@job_alerts`, `@naukri`, `@work_report`) resolved through `jobs/targets.local.json`, which is gitignored; `jobs/targets.example.json` is the template. The scrubber sweeps every exported string, not just the `deliver` field, because prompts name group IDs inline too. An ID with no mapping is masked as `@unknown-NNNN` rather than passing through, so forgetting to map a new target cannot leak it.

## Editing a script

`scripts/` is the source of truth. Hermes resolves a job's `script` field as a filename inside `~/.hermes/scripts`, so that directory has to hold real files — it cannot point at a checkout.

```sh
./sync.sh --dry-run   # what would change
./sync.sh             # repo -> ~/.hermes/scripts
./sync.sh --pull      # the other way, to capture a hotfix made in place
```

`sync.sh` only touches the files this repo tracks. The live directory also holds ad-hoc debugging scripts and `.env` files that deliberately stay out of git, so it never mirrors with `--delete`.

## How a job is wired

Most script jobs go through a `run_*.sh` wrapper rather than calling Python directly, because Hermes runs `script` as a *file*, not a shell command — anything multi-step needs a wrapper. Each one sources `_log_preamble.sh`, which tees the whole run to `~/.hermes/logs/pipelines/<name>/` while still writing to stdout, so the agent sees output exactly as before and the detail survives on disk. Python entrypoints get the same thing via `import pipeline_log`.

```
cron tick -> run_tabletap_instagram.sh -> _log_preamble.sh (tee to pipeline log)
                                       -> node src/index.js generate
                                       -> brand_poster.py   (stamp the logo)
                                       -> node src/index.js publish
```

Shared helpers: `pipeline_log.py` (run capture), `browser_lock.py` (one Playwright profile, many jobs — they must not open it concurrently), `social_voice.py` (LLM copy generation), `shared_dedupe.py` and `job_filter.py` (don't reply or apply twice).

## Secrets

No credential is hardcoded anywhere in `scripts/` — every one is read at runtime from a `.env` outside this tree (`INSTAHYRE_PASSWORD`, `IG_ACCESS_TOKEN`, `OPENROUTER_API_KEY`, and so on). `.gitignore` blocks `.env` files as a second line of defence.

`ig_token_refresh` exists because Instagram long-lived tokens expire after 60 days and nothing warns you: the posting job just starts failing with an OAuth error, and by then the token is unrecoverable. It runs weekly so the token can never get near expiry.

## External dependencies

Two jobs reach outside this repo:

- `cron_missed_replay` runs `python -m cron.missed_queue` from the hermes-agent checkout.
- `tabletap_instagram_post` and `sanero_instagram_post` run `node src/index.js` in the `Table-Tap media` project.

## Known gaps

- `jobs_social_feed_scan` references `auto_reply_linkedin.py` and `auto_reply_x.py`, and `social_x_post_publish` references `x_post_publish.py`. None of the three exist in `~/.hermes/scripts`; the prompts point at files that were renamed or removed.
- `naukri_enhance_profile` and `naukri_enhance_profile_v2` are exported but disabled.
