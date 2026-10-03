"""Keep the Table Tap Instagram token alive.

Instagram long-lived tokens last 60 days. Nothing warns you when one lapses:
the posting job simply starts failing with an OAuth error, and by then the token
is dead and has to be re-issued by hand through the Meta dashboard.

Refreshing resets the clock to a fresh 60 days, so running this weekly means the
token can never get close to expiry. Instagram requires a token to be at least
24 hours old before it can be refreshed, which weekly comfortably satisfies.

Usage:
    python ig_token_refresh.py            # refresh and write back to .env
    python ig_token_refresh.py --check    # report expiry only, change nothing
"""

import os
import sys
import json
import shutil
import argparse
import datetime
import urllib.request
import urllib.error
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/ig_token_refresh/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('ig_token_refresh')
    except Exception:
        pass


ENV_PATH = os.path.expanduser('~/Development/Aarambh/Media-man/.env')
# Every brand .env holding its own IG_ACCESS_TOKEN gets refreshed on the same
# weekly schedule. Add new brands here.
ENV_PATHS = {
    'tabletap': ENV_PATH,
    'sanero': os.path.expanduser(
        '~/Development/Aarambh/Media-man/brands/sanero/.env'),
}
REFRESH_URL = 'https://graph.instagram.com/refresh_access_token'
# Refresh when fewer than this many days remain; a weekly run keeps it far off.
RENEW_UNDER_DAYS = 45


def read_env(path=ENV_PATH):
    env = {}
    with open(path) as fh:
        for line in fh:
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


def write_token(new_token, path=ENV_PATH):
    """Rewrite only IG_ACCESS_TOKEN, preserving everything else verbatim."""
    shutil.copy2(path, path + '.bak')
    with open(path) as fh:
        lines = fh.read().splitlines()
    out, replaced = [], False
    for line in lines:
        if line.startswith('IG_ACCESS_TOKEN='):
            out.append(f'IG_ACCESS_TOKEN={new_token}')
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f'IG_ACCESS_TOKEN={new_token}')
    with open(path, 'w') as fh:
        fh.write('\n'.join(l for l in out if l.strip()) + '\n')
    os.chmod(path, 0o600)


def notify_failure(brands, results):
    """Desktop notification on this laptop when a token cannot be refreshed.

    Added 2026-10-03: this job used to report only through Hermes' WhatsApp
    delivery. With WhatsApp off, a dead token (Sanero's, 2026-09-27, after a
    password change) failed silently every week until posting broke. A dead
    token cannot be fixed here — a new one has to be generated in the Meta
    dashboard — so the job's only useful move is to make sure someone sees it.
    """
    import subprocess
    lines = [f"{b}: {results[b].get('error')} — {results[b].get('action', '')}" for b in brands]
    try:
        subprocess.run(['notify-send', '--urgency=critical', '--app-name=Hermes',
                        'Instagram token refresh failed', '\n'.join(lines)],
                       timeout=10, check=False,
                       env={**os.environ, 'DBUS_SESSION_BUS_ADDRESS': os.environ.get(
                           'DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus')})
    except Exception as e:                      # never let alerting break the run
        print(f'notify-send failed: {e}', file=sys.stderr)


def refresh(check_only=False, path=ENV_PATH):
    env = read_env(path)
    token = env.get('IG_ACCESS_TOKEN', '')
    if not token:
        return {'error': 'IG_ACCESS_TOKEN missing from .env'}

    url = f'{REFRESH_URL}?grant_type=ig_refresh_token&access_token={token}'
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:220]
        return {'error': f'refresh failed ({e.code})', 'detail': body,
                'action': 'token may be dead; re-issue it in the Meta dashboard'}
    except Exception as e:
        return {'error': f'refresh error: {str(e)[:140]}'}

    secs = int(data.get('expires_in', 0))
    days = round(secs / 86400, 1)
    expiry = (datetime.datetime.now() +
              datetime.timedelta(seconds=secs)).strftime('%Y-%m-%d')
    out = {'days_remaining': days, 'expires_on': expiry}

    new_token = data.get('access_token')
    if check_only:
        out['action'] = 'check only, token not written'
        return out
    if not new_token:
        out['action'] = 'no new token returned, .env unchanged'
        return out

    write_token(new_token, path)
    out['action'] = f'token refreshed, valid until {expiry}'
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true',
                    help='report expiry without writing a new token')
    args = ap.parse_args()
    results = {brand: refresh(check_only=args.check, path=path)
               for brand, path in ENV_PATHS.items()}
    print(json.dumps(results, indent=2))
    failed = [b for b, r in results.items() if r.get('error')]
    if failed:
        notify_failure(failed, results)
    sys.exit(1 if failed else 0)
