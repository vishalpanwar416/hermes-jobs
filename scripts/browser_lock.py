"""One-at-a-time access to the shared Chrome automation profile.

Every social script drives the same persistent profile at
~/.hermes/data/chrome_automation_profile, and each one begins by deleting
SingletonLock/SingletonSocket/SingletonCookie so a stale lock cannot block it.

That is safe only when a single script runs at a time. It is not: several cron
jobs overlap by schedule, so script B deletes the lock belonging to script A's
live browser, both drive the same profile, and one of them dies. That is what
produced the intermittent linkedin_feed_monitor traceback while the feed itself
was perfectly reachable.

This gives them a real mutex. Chrome's own lock files stay as they are; this
just makes sure only one process reaches them.

    from browser_lock import profile_lock

    with profile_lock('x_feed_monitor'):
        ...launch playwright, do the work...

Waiting is the default because these are background jobs: being late is fine,
failing is not. If the lock cannot be acquired within `timeout`, it raises
BrowserBusy so the caller can exit cleanly instead of corrupting the profile.
"""

import os
import sys
import time
import errno
import fcntl
from contextlib import contextmanager

LOCK_PATH = os.path.expanduser('~/.hermes/data/.chrome_profile.lock')
PROFILE_DIR = os.path.expanduser('~/.hermes/data/chrome_automation_profile')

# Longest a browser job is expected to hold the profile. A holder older than
# this is treated as dead, since a wedged Chrome should not block every job
# forever.
STALE_AFTER = 900


class BrowserBusy(RuntimeError):
    """Another automation job holds the Chrome profile."""


def _holder():
    try:
        with open(LOCK_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return '?'


def clear_chrome_singletons():
    """Remove Chrome's own stale lock files.

    Only safe to call while holding the profile lock, which is why it lives
    here rather than being copy-pasted into every script.
    """
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        path = os.path.join(PROFILE_DIR, name)
        try:
            os.remove(path)
        except OSError as e:
            if e.errno not in (errno.ENOENT,):
                pass


@contextmanager
def profile_lock(owner='automation', timeout=600, poll=3.0):
    """Hold the Chrome profile exclusively for the duration of the block."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, 'a+')
    deadline = time.time() + timeout
    waited = False

    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            # Break a lock whose holder is clearly gone, otherwise one wedged
            # Chrome would stall every browser job indefinitely.
            try:
                age = time.time() - os.path.getmtime(LOCK_PATH)
            except OSError:
                age = 0
            if age > STALE_AFTER:
                print(f"[lock] breaking stale lock held by {_holder()} "
                      f"({int(age)}s old)", file=sys.stderr)
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    pass

            if time.time() >= deadline:
                fh.close()
                raise BrowserBusy(
                    f"Chrome profile held by {_holder()}; waited {timeout}s")

            if not waited:
                print(f"[lock] {owner} waiting for Chrome profile "
                      f"(held by {_holder()})", file=sys.stderr)
                waited = True
            time.sleep(poll)

    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{owner} pid={os.getpid()} since={int(time.time())}")
        fh.flush()
        os.utime(LOCK_PATH, None)
        clear_chrome_singletons()
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# Headless vs headed.
#
# The job-board scripts used to run a headed Chrome inside a private Xvfb
# display, because headless Chrome announces itself as "HeadlessChrome" in the
# user agent and Naukri's Akamai wall rejected it (2026-08-23). Chrome's new
# headless mode is the same browser binary, so presenting the normal user agent
# is enough, and it skips Xvfb's software compositing on every run.
#
# HERMES_HEADED=1 brings back the Xvfb path for one run or for good.
HEADED = os.environ.get('HERMES_HEADED') == '1'


def _chrome_major():
    import subprocess
    try:
        out = subprocess.run(['google-chrome', '--version'], capture_output=True,
                             text=True, timeout=10).stdout
        return out.split()[-1].split('.')[0]
    except Exception:
        return '156'


def headless_launch_kwargs():
    """launch_persistent_context kwargs for the current mode."""
    if HEADED:
        return {'headless': False}
    return {
        'headless': True,
        'user_agent': ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       f'(KHTML, like Gecko) Chrome/{_chrome_major()}.0.0.0 '
                       'Safari/537.36'),
    }


class _NoDisplay:
    def terminate(self):
        pass


def virtual_display(start_xvfb):
    """Start Xvfb only when running headed. Returns something with terminate()."""
    if not HEADED:
        return _NoDisplay()
    proc, display = start_xvfb()
    os.environ['DISPLAY'] = display
    os.environ.pop('WAYLAND_DISPLAY', None)
    return proc


if __name__ == '__main__':
    hold = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    with profile_lock(f'selftest-{os.getpid()}', timeout=30):
        print(f'acquired, holding {hold}s')
        time.sleep(hold)
    print('released')
