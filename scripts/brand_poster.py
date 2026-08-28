"""Composite the real Table Tap logo onto generated posters.

An image model cannot reproduce a logo reliably. Asked to draw one it invents a
near-miss wordmark, which is worse than no logo: it looks like the brand but is
not the brand. So the generator is told not to draw one, and the actual asset
from table-tap.in is stamped on here.

The logo has a transparent background and a dark mark, so on a dark poster it
would disappear. This samples the pixels underneath and, when that area is dark,
lays a soft cream plate behind the logo first.

Usage:
    python brand_poster.py                     # brand every un-branded queued poster
    python brand_poster.py --path poster.png   # brand one file
    python brand_poster.py --force             # re-brand even if already marked
"""

import os
import sys
import shutil
import json
import argparse

from PIL import Image
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Capture this run's full stdout and stderr to
# ~/.hermes/logs/pipelines/brand_poster/ so a failed or silent run can be
# diagnosed afterwards instead of vanishing.
if __name__ == '__main__':
    # Only when run directly. Firing on import made every script that
    # imports this module log its own run under this pipeline's name.
    try:
        import pipeline_log as _plog
        _plog.start('brand_poster')
    except Exception:
        pass


MEDIA_ROOT = os.path.expanduser('~/Development/Aarambh/Table-Tap media')
# Multi-brand: BRAND_DIR (same contract as the Node backend) points at a brand
# folder holding its own assets/logo.png and content/. Unset = Table Tap.
BRAND_ROOT = os.environ.get('BRAND_DIR') or MEDIA_ROOT
LOGO_PATH = os.path.join(BRAND_ROOT, 'assets', 'logo.png')
QUEUE_DIR = os.path.join(BRAND_ROOT, 'content', 'queue')

# Corner placement, not bottom-centre. The image model keeps putting its own
# caption across the bottom-centre despite being told to leave it clear, and a
# logo landing on top of that text looked like a mistake.
LOGO_WIDTH_RATIO = 0.30
MARGIN_RATIO = 0.045
# Mean luminance below this counts as a dark background.
DARK_THRESHOLD = 140
CREAM = (253, 246, 236)  # brand cream #FDF6EC
INK = (12, 13, 16)       # sanero ink #0C0D10
# 'dark-mark' (Table Tap): dark logo, mono-cream on dark posters.
# 'light-mark' (Sanero): pale logo, keep colour on dark, mono-ink on light.
LOGO_STYLE = os.environ.get('BRAND_LOGO_STYLE', 'dark-mark')


def _is_dark(poster, box):
    """Mean luminance of the area the logo will sit on."""
    region = poster.convert('RGB').crop(box)
    px = list(region.getdata())
    if not px:
        return False
    return (sum(sum(p) / 3 for p in px) / len(px)) < DARK_THRESHOLD


def _mono(logo, colour):
    """Recolour the logo to a single colour, preserving its alpha.

    The asset has a dark mark on transparency, so on a dark poster it vanishes.
    Laying a pale rectangle behind it fixed legibility but looked pasted on. A
    mono silhouette is what a brand kit would ship as its reversed variant.
    """
    solid = Image.new('RGBA', logo.size, colour + (255,))
    solid.putalpha(logo.getchannel('A'))
    return solid


def brand_one(poster_path, logo_path=LOGO_PATH, force=False):
    if not os.path.isfile(logo_path):
        return {'path': poster_path, 'error': f'logo missing at {logo_path}'}

    marker = poster_path + '.branded'
    # A stale marker is worse than no marker: the generator reuses a slug when
    # it picks the same title twice, overwriting poster.png while the old
    # marker survives, and the fresh poster then ships unbranded. Trust the
    # marker only if it is newer than the poster it claims to describe.
    marker_valid = (os.path.exists(marker) and
                    os.path.getmtime(marker) >= os.path.getmtime(poster_path))
    if marker_valid and not force:
        return {'path': poster_path, 'action': 'already branded, skipped'}

    # Branding writes in place, so keep a pristine copy the first time and
    # always composite from that. Without it, --force stamps a second logo onto
    # an already-stamped poster instead of redoing the job cleanly.
    original = poster_path + '.orig'
    if not os.path.exists(original):
        shutil.copy2(poster_path, original)
    poster = Image.open(original).convert('RGBA')
    logo = Image.open(logo_path).convert('RGBA')

    target_w = int(poster.width * LOGO_WIDTH_RATIO)
    scale = target_w / logo.width
    logo = logo.resize((target_w, max(1, int(logo.height * scale))),
                       Image.LANCZOS)

    margin = int(poster.width * MARGIN_RATIO)
    x = poster.width - logo.width - margin
    y = poster.height - logo.height - margin
    box = (x, y, x + logo.width, y + logo.height)

    dark_backdrop = _is_dark(poster, box)
    if LOGO_STYLE == 'light-mark':
        # Pale mark: already legible on dark, silhouette it in ink on light.
        recoloured = not dark_backdrop
        if recoloured:
            logo = _mono(logo, INK)
        variant = 'ink reversed' if recoloured else 'original'
    else:
        recoloured = dark_backdrop
        if recoloured:
            logo = _mono(logo, CREAM)
        variant = 'cream reversed' if recoloured else 'original'

    poster.alpha_composite(logo, (x, y))
    poster.convert('RGB').save(poster_path, 'PNG')
    with open(marker, 'w') as fh:
        fh.write('branded\n')

    return {'path': poster_path, 'action': 'branded',
            'logo_width': logo.width,
            'variant': variant}


def brand_queue(force=False):
    out = []
    if not os.path.isdir(QUEUE_DIR):
        return out
    for name in sorted(os.listdir(QUEUE_DIR)):
        p = os.path.join(QUEUE_DIR, name, 'poster.png')
        if os.path.isfile(p):
            out.append(brand_one(p, force=force))
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--path', help='brand a single poster file')
    ap.add_argument('--force', action='store_true', help='re-brand already-branded posters')
    args = ap.parse_args()

    if args.path:
        res = [brand_one(args.path, force=args.force)]
    else:
        res = brand_queue(force=args.force)
    print(json.dumps(res, indent=2))
    sys.exit(1 if any(r.get('error') for r in res) else 0)
