"""Capture every run of every pipeline to disk.

Hermes stores a cron job's final agent response in ~/.hermes/cron/output, but
nothing captures what the underlying script actually did. All the useful detail
lives on stderr: which post was rejected and why, which company was applied to,
which selector failed. That is thrown away, so a job that quietly did nothing
reports the same "[SILENT]" as a job that had nothing to do.

This tees stdout and stderr of a run into a timestamped file per pipeline while
still writing them through, so the agent and the terminal see output as before.

    import pipeline_log
    pipeline_log.start('naukri_auto_apply')

Layout:
    ~/.hermes/logs/pipelines/<pipeline>/2026-08-23_14-05-01.log
    ~/.hermes/logs/pipelines/<pipeline>/latest.log      -> newest run
    ~/.hermes/logs/pipelines/index.jsonl                -> one row per run

Inspect with:
    python pipeline_log.py --list
    python pipeline_log.py --tail naukri_auto_apply
"""

import io
import os
import sys
import json
import time
import atexit
import datetime
import traceback

LOG_ROOT = os.path.expanduser('~/.hermes/logs/pipelines')
INDEX_PATH = os.path.join(LOG_ROOT, 'index.jsonl')

# Keep this many runs per pipeline. Runs are small text files; the point of the
# cap is to stop an hourly job filling the disk over months.
KEEP_RUNS = 200

_state = {'started': False}


class _Tee(io.TextIOBase):
    """Write to the real stream and the log file at once."""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, data):
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._fh.write(data)
            self._fh.flush()
        except Exception:
            pass
        return len(data)

    def flush(self):
        for t in (self._stream, self._fh):
            try:
                t.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False


def _prune(pipeline_dir):
    try:
        runs = sorted(f for f in os.listdir(pipeline_dir)
                      if f.endswith('.log') and f != 'latest.log')
        for stale in runs[:-KEEP_RUNS]:
            os.remove(os.path.join(pipeline_dir, stale))
    except OSError:
        pass


def _index(row):
    try:
        os.makedirs(LOG_ROOT, exist_ok=True)
        with open(INDEX_PATH, 'a') as fh:
            fh.write(json.dumps(row) + '\n')
    except OSError:
        pass


def start(pipeline, argv=None):
    """Begin capturing this process's output for `pipeline`."""
    if _state.get('started'):
        return _state.get('path')

    # A run_*.sh wrapper already tees this whole process. Capturing again here
    # would write the same output to a second file under the same pipeline and
    # make the run look like it happened twice.
    outer = os.environ.get('PIPE_LOG')
    if outer:
        _state.update({'started': True, 'path': outer})
        return outer

    d = os.path.join(LOG_ROOT, pipeline)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    path = os.path.join(d, f'{stamp}.log')

    fh = open(path, 'a', buffering=1)
    fh.write(f"=== {pipeline} :: {datetime.datetime.now().isoformat()} ===\n")
    fh.write(f"argv: {' '.join(argv or sys.argv)}\n")
    fh.write(f"pid : {os.getpid()}\n\n")

    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)

    # A stable path for "what happened last time", so tailing does not require
    # knowing the timestamp.
    link = os.path.join(d, 'latest.log')
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(path, link)
    except OSError:
        pass

    _state.update({'started': True, 'path': path, 'pipeline': pipeline,
                   'fh': fh, 't0': time.time()})
    _prune(d)

    def _finish():
        dur = round(time.time() - _state['t0'], 1)
        exc = sys.exc_info()[0]
        try:
            fh.write(f"\n=== end :: {dur}s ===\n")
            if exc:
                fh.write(traceback.format_exc())
            fh.flush()
            fh.close()
        except Exception:
            pass
        _index({'pipeline': pipeline, 'started_at': stamp,
                'duration_s': dur, 'log': path,
                'failed': bool(exc)})

    atexit.register(_finish)
    return path


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------

def list_runs(limit=25):
    rows = []
    try:
        with open(INDEX_PATH) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-limit:]


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--list', action='store_true', help='recent runs across all pipelines')
    ap.add_argument('--tail', metavar='PIPELINE', help='print the latest log for a pipeline')
    ap.add_argument('--lines', type=int, default=60)
    args = ap.parse_args()

    if args.tail:
        p = os.path.join(LOG_ROOT, args.tail, 'latest.log')
        if not os.path.exists(p):
            print(f'no runs recorded for {args.tail}')
            return
        with open(p) as fh:
            print(''.join(fh.readlines()[-args.lines:]))
        return

    rows = list_runs()
    if not rows:
        print('No runs recorded yet.')
        return
    print(f"{'pipeline':30s} {'started':20s} {'secs':>6s}  status")
    for r in rows:
        print(f"  {r['pipeline'][:28]:28s} {r['started_at']:20s} "
              f"{r['duration_s']:>6}  {'FAILED' if r.get('failed') else 'ok'}")


if __name__ == '__main__':
    _cli()
