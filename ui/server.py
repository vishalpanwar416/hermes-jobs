#!/usr/bin/env python3
"""A small read-only web UI for inspecting Hermes cron job runs.

`hermes cron list` shows one line of status per job. When something fails you
then have to go find the run: agent jobs record an execution row in
~/.hermes/cron/executions.db and dump their final response under
~/.hermes/cron/output/<job_id>/, while script jobs tee everything they printed
to ~/.hermes/logs/pipelines/<pipeline>/. This stitches those three sources into
one timeline per job so a failure is two clicks from the job list.

Read-only by design: it opens the sqlite database in immutable mode and never
writes to the Hermes data directory. Nothing here can start, stop, or edit a
job — use `hermes cron` for that.

    python ui/server.py                 # http://127.0.0.1:8787
    python ui/server.py --port 9000
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

HERMES = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
JOBS_JSON = HERMES / "cron" / "jobs.json"
EXECUTIONS_DB = HERMES / "cron" / "executions.db"
OUTPUT_ROOT = HERMES / "cron" / "output"
LOG_ROOT = HERMES / "logs" / "pipelines"

UI_DIR = Path(__file__).resolve().parent
REPO = UI_DIR.parent

# A log line is only worth truncating if it is genuinely enormous; a stack trace
# or a scraped job description is exactly the detail you opened the UI for.
MAX_LOG_BYTES = 2_000_000

app = FastAPI(title="Hermes Jobs")


def read_jobs():
    if not JOBS_JSON.exists():
        return []
    return json.loads(JOBS_JSON.read_text()).get("jobs", [])


def pipeline_dirs():
    if not LOG_ROOT.is_dir():
        return []
    return sorted(p.name for p in LOG_ROOT.iterdir() if p.is_dir())


PIPE_NAME_RE = re.compile(r"^PIPE_NAME=(\S+)", re.M)
SCRIPT_REF_RE = re.compile(r"([\w./-]+)\.py\b")


def _wrapper_pipeline(script):
    """Read PIPE_NAME out of a run_*.sh wrapper, checking the repo then live."""
    for base in (REPO / "scripts", HERMES / "scripts"):
        path = base / script
        if path.is_file():
            m = PIPE_NAME_RE.search(path.read_text(errors="replace"))
            if m:
                return m.group(1).strip("\"'")
            return None
    return None


def pipeline_for(job, known):
    """Map a job to the pipeline log directory its run actually writes to.

    The job name is usually the pipeline name, but not always:
    naukri_recommended_auto_apply runs naukri_auto_apply.py, which calls
    pipeline_log.start('naukri_auto_apply'). So rather than guessing from the
    names, the mapping is read from the code the job runs — PIPE_NAME for a
    shell wrapper, the module stem for a Python entrypoint.
    """
    name = job["name"]
    if name in known:
        return name

    script = job.get("script") or ""
    if script.endswith(".sh"):
        pipe = _wrapper_pipeline(script)
        if pipe in known:
            return pipe
    if script.endswith(".py") and Path(script).stem in known:
        return Path(script).stem

    # Agent jobs have no script field; the prompt names the file it should run.
    for stem in SCRIPT_REF_RE.findall(job.get("prompt") or ""):
        candidate = Path(stem).name
        if candidate in known:
            return candidate
    return None


def parse_ts(value):
    """Hermes writes ISO timestamps; return epoch seconds or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def executions_for(job_id, limit=100):
    if not EXECUTIONS_DB.exists():
        return []
    # immutable=1 means we never take a lock, so a running scheduler is never
    # blocked or disturbed by someone refreshing this page.
    uri = f"file:{EXECUTIONS_DB}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT id, status, claimed_at, started_at, finished_at, error, source"
            " FROM executions WHERE job_id = ?"
            " ORDER BY COALESCE(started_at, claimed_at) DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        return [{"kind": "error", "error": f"executions.db unreadable: {exc}"}]

    out = []
    for eid, status, claimed, started, finished, error, source in rows:
        began = started or claimed
        out.append(
            {
                "kind": "execution",
                "id": eid,
                "status": status,
                "started_at": began,
                "finished_at": finished,
                "duration_s": _duration(began, finished),
                "error": error,
                "source": source,
            }
        )
    return out


def _duration(start, end):
    a, b = parse_ts(start), parse_ts(end)
    return round(b - a, 1) if a and b else None


def logs_for(pipeline, limit=100):
    if not pipeline:
        return []
    d = LOG_ROOT / pipeline
    if not d.is_dir():
        return []
    files = [f for f in d.glob("*.log") if not f.is_symlink()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        stat = f.stat()
        out.append(
            {
                "kind": "log",
                "id": f.name,
                "pipeline": pipeline,
                "started_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).astimezone().isoformat(),
                "size": stat.st_size,
                "path": str(f.relative_to(LOG_ROOT)),
            }
        )
    return out


def outputs_for(job_id, limit=50):
    """Agent jobs dump their final response text under cron/output/<job_id>/."""
    d = OUTPUT_ROOT / job_id
    if not d.is_dir():
        return []
    files = sorted(d.glob("*"), key=lambda f: f.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        if not f.is_file():
            continue
        stat = f.stat()
        out.append(
            {
                "kind": "output",
                "id": f.name,
                "started_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).astimezone().isoformat(),
                "size": stat.st_size,
                "path": f"{job_id}/{f.name}",
            }
        )
    return out


@app.get("/api/jobs")
def api_jobs():
    known = pipeline_dirs()
    jobs = []
    claimed = set()
    for j in read_jobs():
        pipeline = pipeline_for(j, known)
        if pipeline:
            claimed.add(pipeline)
        jobs.append(
            {
                "id": j["id"],
                "name": j["name"],
                "enabled": j.get("enabled", True),
                "schedule": j.get("schedule_display") or "",
                "mode": "script" if j.get("no_agent") else "agent",
                "script": j.get("script"),
                "deliver": j.get("deliver"),
                "last_status": j.get("last_status"),
                "last_run_at": j.get("last_run_at"),
                "next_run_at": j.get("next_run_at"),
                "last_error": j.get("last_error"),
                "last_delivery_error": j.get("last_delivery_error"),
                "pipeline": pipeline,
            }
        )
    jobs.sort(key=lambda j: (j["last_status"] != "error", j["name"]))
    # Pipelines with no surviving job still hold logs worth reading, so they are
    # listed separately rather than silently dropped.
    orphans = [p for p in known if p not in claimed]
    return {"jobs": jobs, "orphan_pipelines": orphans}


@app.get("/api/runs")
def api_runs(job: str = Query(...)):
    known = pipeline_dirs()
    match = next((j for j in read_jobs() if j["name"] == job or j["id"] == job), None)

    if match is None:
        # An orphan pipeline: logs exist but the job is gone.
        if job in known:
            runs = logs_for(job)
            return {"job": {"name": job, "pipeline": job, "orphan": True}, "runs": runs}
        raise HTTPException(404, f"no job or pipeline named {job!r}")

    pipeline = pipeline_for(match, known)
    runs = executions_for(match["id"]) + logs_for(pipeline) + outputs_for(match["id"])
    runs.sort(key=lambda r: parse_ts(r.get("started_at")) or 0, reverse=True)
    return {
        "job": {
            "id": match["id"],
            "name": match["name"],
            "pipeline": pipeline,
            "schedule": match.get("schedule_display"),
            "mode": "script" if match.get("no_agent") else "agent",
            "prompt": match.get("prompt"),
            "script": match.get("script"),
        },
        "runs": runs,
    }


def _safe(root: Path, rel: str) -> Path:
    """Resolve rel under root, refusing anything that escapes it."""
    target = (root / rel).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep):
        raise HTTPException(400, "path outside allowed directory")
    if not target.is_file():
        raise HTTPException(404, "no such file")
    return target


@app.get("/api/content")
def api_content(kind: str = Query(...), path: str = Query(...)):
    roots = {"log": LOG_ROOT, "output": OUTPUT_ROOT}
    if kind not in roots:
        raise HTTPException(400, "kind must be log or output")
    target = _safe(roots[kind], path)
    size = target.stat().st_size
    with target.open("rb") as fh:
        if size > MAX_LOG_BYTES:
            fh.seek(size - MAX_LOG_BYTES)
            head = f"[truncated: showing last {MAX_LOG_BYTES // 1000}KB of {size // 1000}KB]\n"
        else:
            head = ""
        body = head + fh.read().decode("utf-8", errors="replace")
    return JSONResponse({"path": path, "size": size, "content": body})


@app.get("/")
def index():
    return FileResponse(UI_DIR / "index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1", help="bind address; keep local")
    args = ap.parse_args()

    if not HERMES.is_dir():
        sys.exit(f"no Hermes data directory at {HERMES} (set HERMES_HOME)")

    import uvicorn

    print(f"hermes-jobs log viewer -> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
