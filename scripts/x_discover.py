#!/usr/bin/env python3
"""Find tech posts on X that are actually worth replying to.

The reply job has had exactly one source since 2026-09-07: `from:` over the
handles in x_targets.json, inside a two hour window, every ninety minutes. With
five handles on the list that search returned nothing on 75 consecutive runs
between 09-10 and 09-15 — no replies posted, no reach, six days. The job was not
broken. It was starved, and the target list is a deliberate human gate
(X_GROWTH_PLAN.md section 3) that code must not widen by itself.

So this module widens the *post* sources without touching the handle list:

  targets   the same `from:` search, window opened 2h -> 6h
  feed      the home timeline, which is already curated by who he follows
  topic     keyword search, with the quality bar the old one never had

The old topic fallback is off by default for a documented reason: it hit about
4% relevance and drafted a reply to a "CANCEL your weekend plans" thread. The bar
here is different in kind, not degree. That version scored a truncated feed card
against a keyword list and posted. This one scores the card, then opens the
permalink and re-scores against the real post, the author's follower count and
how many replies are already underneath — the three facts that decide whether a
reply is seen by anyone, none of which a feed card carries.

Two stages, because the second one costs a page load:

  stage 1  score_card()  keyword signal and bait rejection, over every candidate
  stage 2  score_post()  reach and timing, over the handful stage 1 ranked top
"""
import os
import re
import sys
import json
import math
import random
import sqlite3
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import x_context

TARGETS_FILE = os.path.expanduser('~/Development/Aarambh/hermes-jobs/x_targets.json')

# Replying inside the first hour is worth more than anything else this system
# does, but a six hour old post from a small account still has a live reply
# section and a reader coming to it later. Two hours was tuned for a target list
# ten times this size; on five handles it is the whole reason nothing is found.
TARGET_WINDOW_HOURS = 6

# Non-target authors have to clear a reach floor, because a perfect reply to an
# account with 40 followers is a perfect reply nobody sees. Targets are exempt:
# they were chosen by hand for reasons the follower count does not capture.
MIN_FOLLOWERS_NON_TARGET = 1_200

# A post already carrying this many replies buries anything added now.
REPLY_SATURATION = 60

# Hard ceiling on age, checked against the post's own timestamp.
#
# X's `within_time:` operator is advisory at best: a dry run on 2026-09-16 asked
# for within_time:24h and got back a post 950 hours old, because the operator
# interacts badly with min_faves. The search filter is a hint, the timestamp on
# the post is the fact, and a reply on a five week old thread reaches nobody.
MAX_POST_AGE_HOURS = 72

# Bio terms that disqualify an author whatever they happened to post. The
# 950 hour old post above was a paid-looking thread about a "decentralized
# sensor network" from an account whose bio reads "Web3 Guy | Crypto Trader",
# and it cleared a keyword filter aimed at post text because the giveaway was
# never in the post text.
BIO_NOGO = [
    'web3', 'crypto', 'nft', 'defi', 'blockchain', 'token', 'airdrop', 'trader',
    'degen', 'memecoin', 'forex', 'bitcoin', 'ethereum', 'solana',
]


# ---------------------------------------------------------------------------
# topic signal
# ---------------------------------------------------------------------------

# Grouped so breadth can be rewarded: a post touching two of these is usually a
# real engineering discussion, one touching none is not.
SIGNAL = {
    'backend': ['backend', 'api', 'microservice', 'latency', 'throughput', 'p95', 'p99',
                'rps', 'benchmark', 'profiling', 'scaling', 'load test'],
    'go': ['golang', 'goroutine', 'sync.pool', 'pprof', 'gomemlimit', 'grpc'],
    'infra': ['kubernetes', 'k8s', 'terraform', 'docker', 'ci/cd', 'observability',
              'prometheus', 'grafana', 'sre', 'on-call', 'incident', 'outage', 'postmortem'],
    'data': ['postgres', 'postgresql', 'redis', 'kafka', 'sharding', 'index',
             'query plan', 'connection pool', 'replication', 'migration', 'cache'],
    'ai': ['llm', 'rag', 'mcp', 'model context protocol', 'inference', 'embedding',
           'vector', 'retrieval', 'eval', 'evals', 'agent', 'fine-tune', 'context window',
           'tool calling', 'structured output'],
    'design': ['system design', 'distributed', 'architecture', 'failover', 'idempotent',
               'circuit breaker', 'backpressure', 'race condition', 'deadlock',
               'thundering herd', 'consensus', 'raft'],
}

# Specific enough that the author is describing something they actually did.
# These are what separate a post worth a technical reply from a post about tech.
DEPTH = ['p95', 'p99', 'pprof', 'sync.pool', 'gomemlimit', 'connection pool',
         'thundering herd', 'backpressure', 'circuit breaker', 'query plan',
         'flame graph', 'heap profile', 'goroutine leak', 'memory leak', 'race condition',
         'cold start', 'tail latency', 'write amplification', 'n+1', 'deadlock']

# Shapes that never repay a reply, whoever posts them.
NOGO = [
    'follow for follow', 'f4f', 'like and follow', 'retweet to win', 'giveaway',
    'airdrop', 'nft', 'mint', 'crypto', 'dm for promo', 'link in bio', 'referral link',
    'sign up with my code', 'cash app', 'hiring alert', 'job opening', 'we are hiring',
    'apply now', 'drop a gm', 'gm fam', 'tag 3 friends', 'comment "yes"',
    '#motivation', 'grindset', 'hustle culture', 'onlyfans', 'casino', 'betting',
    # Web3 promotion dressed as systems engineering. "decentralized" alone is a
    # legitimate distributed systems word and is deliberately not in this list.
    'web3', 'on-chain', 'onchain', 'blockchain', 'tokenomics', 'presale', 'defi',
    'staking', 'memecoin', 'to the moon',
]

# Engagement bait: technical on the surface, no claim to engage with underneath.
BAIT = [
    'like if you', 'retweet if', 'follow for more', 'tag someone who',
    'comment down below', 'what is your favourite', 'what is your favorite',
    'reply with your', 'unpopular opinion:', 'a thread 🧵', 'thread below',
    'bookmark this', 'save this post', 'here are 10', 'here are 5',
]


def _has(text, terms):
    return any(t in text for t in terms)


def score_card(text):
    """Cheap first pass over a search or feed card. Negative means reject.

    Deliberately generous: this stage only decides which handful of posts are
    worth a page load, and the real gate is score_post() plus the relevance
    judge. Being strict here throws away posts whose card was truncated mid
    sentence, which is most of the good ones.
    """
    t = (text or '').lower()
    if len(t.strip()) < 40:
        return -1
    if _has(t, NOGO):
        return -1
    if _has(t, BAIT):
        return -1

    score = 0
    hit_groups = 0
    for _group, words in SIGNAL.items():
        if _has(t, words):
            hit_groups += 1
            score += 2
    if not hit_groups:
        return -1

    score += hit_groups                       # breadth
    score += 3 * sum(1 for d in DEPTH if d in t)

    # A question is an invitation, and answering one is the single most reliable
    # way a reply earns a response rather than a like.
    if '?' in t:
        score += 2
    # Numbers usually mean a measurement, which gives a reply something to grip.
    if re.search(r'\b\d+(\.\d+)?\s*(ms|s|rps|qps|gb|mb|k|x|%)\b', t):
        score += 2
    return score


def score_post(ctx, is_target=False, card_score=0):
    """Second pass, over a fetched context. Returns (score, reason) or (None, why).

    This is the part the old topic search had no way to do. Everything here is a
    fact about reach — who will see the reply — rather than about the subject.
    """
    if not ctx:
        return None, 'no context'

    text = (ctx.get('text') or '').strip()
    if len(text) < 40 and not ctx.get('images') and not ctx.get('quoted'):
        return None, 'post has no readable body'

    full = ' '.join([text, ctx.get('quoted', ''), ' '.join(ctx.get('images', []))]).lower()
    if _has(full, NOGO):
        return None, 'nogo term in full text'
    # The card may have been truncated before the bait line; check again now
    # that the whole post is visible.
    if _has(full, BAIT):
        return None, 'engagement bait in full text'

    age = ctx.get('age_hours')
    if age is not None and age > MAX_POST_AGE_HOURS:
        return None, f'{age:.0f}h old, past the {MAX_POST_AGE_HOURS}h ceiling'

    followers = ctx.get('author_followers') or 0
    unknown = ctx.get('author_unknown', False)
    if not is_target and not unknown and followers < MIN_FOLLOWERS_NON_TARGET:
        return None, f'{followers} followers, below {MIN_FOLLOWERS_NON_TARGET} for a non-target'

    # The target list is hand-picked, so a bio check on those handles would only
    # second-guess a human decision. Everyone else gets read.
    if not is_target:
        bio = (ctx.get('author_bio') or '').lower()
        hit = next((b for b in BIO_NOGO if b in bio), None)
        if hit:
            return None, f'author bio reads as {hit}'

    replies = ctx.get('reply_count') or 0
    if replies > REPLY_SATURATION:
        return None, f'{replies} replies already, a new one is buried'

    score = float(card_score)

    # Re-score the topic signal against the full text. A card cut at 280
    # characters routinely hides the only technical sentence in the post.
    score += score_card(text) * 0.5 if score_card(text) > 0 else 0

    # Note there is deliberately no bonus for being on the target list. The list
    # earns its privilege once, in the caller's floor (MIN_SCORE_TARGET is far
    # below MIN_SCORE_STRANGER), rather than twice. An earlier cut added +15
    # here as well and the two together made a stale target post outscore a
    # fresh, well aimed reply to a stranger with fifty thousand followers.

    # Reach. Log scaled, because the difference between 2k and 20k followers
    # matters and the difference between 200k and 2M does not.
    if followers:
        score += min(10.0, math.log10(max(followers, 10)) * 2.2)

    # Being early. This is the lever the plan is built around.
    if age is not None:
        if age < 1:
            score += 6
        elif age < 3:
            score += 4
        elif age < 8:
            score += 2
        elif age > 36:
            score -= 4

    # Room to be seen.
    if replies <= 3:
        score += 4
    elif replies <= 12:
        score += 2
    elif replies > 30:
        score -= 2

    # Proof the post has traction at all, so the reply lands somewhere alive.
    likes = ctx.get('like_count') or 0
    if likes:
        score += min(5.0, math.log10(max(likes, 1)) * 1.8)

    # A post nobody has answered yet, from someone with reach, posted minutes
    # ago, is the best thing this system can find.
    reason = (f'{followers:,} followers, {age if age is None else round(age, 1)}h old, '
              f'{replies} replies, {likes} likes')
    return round(score, 1), reason


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def load_targets():
    """Handles and the exclusion list, straight from the hand-maintained file."""
    try:
        data = json.load(open(TARGETS_FILE))
    except Exception as e:
        print(f'[discover] cannot read target list: {e}', file=sys.stderr)
        return [], set()
    handles, excluded = [], set()
    for key, val in data.items():
        if key.startswith('tier_') and isinstance(val, list):
            for e in val:
                h = (e.get('handle') or '').lstrip('@')
                if h:
                    handles.append(h)
    for h in (data.get('_excluded', {}) or {}).get('handles', []) or []:
        excluded.add(h.lower().lstrip('@'))
    return handles, excluded


TOPIC_QUERIES = [
    # Each pairs a subject with evidence that the author is describing real work,
    # and carries a like floor high enough that the post already has an audience.
    # min_faves is what the reply is riding on: it is the audience, not a quality
    # signal about the text.
    '("p99" OR "p95" OR "tail latency" OR "throughput") (backend OR api OR service) '
    '-filter:replies lang:en min_faves:15',

    '(golang OR "go concurrency" OR goroutine OR pprof OR "sync.Pool") '
    '-filter:replies lang:en min_faves:15',

    '(postgres OR postgresql OR redis) (index OR "query plan" OR cache OR locks OR '
    'replication OR "connection pool") -filter:replies lang:en min_faves:15',

    '(kubernetes OR k8s) (outage OR incident OR failover OR autoscal OR "cost" OR oom) '
    '-filter:replies lang:en min_faves:15',

    '("rag" OR "retrieval" OR "vector db" OR "embeddings") (eval OR evals OR chunking OR '
    'latency OR "in production") -filter:replies lang:en min_faves:15',

    '("mcp server" OR "model context protocol" OR "agent loop" OR "tool calling") '
    '(production OR eval OR "context window" OR debugging) -filter:replies lang:en min_faves:15',

    '("system design" OR "distributed systems") (idempoten OR "circuit breaker" OR '
    'backpressure OR "thundering herd" OR retries) -filter:replies lang:en min_faves:15',

    '(postmortem OR "root cause" OR "we found the bug" OR "took down prod" OR '
    '"memory leak" OR "race condition") -filter:replies lang:en min_faves:20',
]


def _cards_from_page(page, limit_scrolls=3):
    """Read (url, author, text) off whatever article cards are on screen."""
    out, seen = [], set()
    for _ in range(limit_scrolls):
        try:
            articles = page.locator('article[data-testid="tweet"]').all()
        except Exception:
            break
        for art in articles:
            try:
                text = art.inner_text(timeout=2500)
                url, author = '', ''
                for link in art.locator('a').all():
                    href = link.get_attribute('href') or ''
                    if '/status/' in href and not url:
                        url = ('https://x.com' + href) if href.startswith('/') else href
                        url = url.split('?')[0].split('/analytics')[0]
                        url = re.sub(r'/(photo|video)/\d+$', '', url)
                    elif (not author and href.startswith('/') and '/status/' not in href
                          and href.count('/') == 1 and href not in ('/home', '/explore',
                                                                    '/notifications', '/messages')):
                        author = href.strip('/')
                if url and url not in seen:
                    seen.add(url)
                    out.append({'url': url, 'author': author, 'text': text})
            except Exception:
                continue
        try:
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(2200)
        except Exception:
            break
    return out


def _search(page, query, sort='top'):
    url = ('https://x.com/search?q=' + urllib.request.quote(query) + f'&f={sort}')
    try:
        page.goto(url, timeout=35000, wait_until='domcontentloaded')
        page.wait_for_timeout(3500)
    except Exception as e:
        print(f'[discover] search failed: {e}', file=sys.stderr)
        return []
    return _cards_from_page(page)


def from_targets(page, handles):
    """The hand-picked list, newest first. Unchanged in kind, wider in window."""
    if not handles:
        return []
    cards = []
    pool = list(handles)
    random.shuffle(pool)
    for i in range(0, len(pool), 12):
        batch = pool[i:i + 12]
        q = ('(' + ' OR '.join(f'from:{h}' for h in batch) + ') -filter:replies '
             f'within_time:{TARGET_WINDOW_HOURS}h')
        print(f'[discover] targets: {q}', file=sys.stderr)
        # Author searches want the newest post, not the most engaged one; the
        # entire value here is arriving early.
        for c in _search(page, q, sort='live'):
            c['source'] = 'targets'
            cards.append(c)
    return cards


def from_feed(page):
    """The home timeline.

    Worth having as a source precisely because it is not algorithmic noise for
    this account: it is the output of who Vishal already chose to follow, which
    is a weaker version of the same human curation the target list encodes.
    """
    try:
        page.goto('https://x.com/home', timeout=45000, wait_until='domcontentloaded')
        page.wait_for_timeout(4000)
    except Exception as e:
        print(f'[discover] feed failed: {e}', file=sys.stderr)
        return []
    if any(x in page.url.lower() for x in ('login', 'i/flow', 'authorize')):
        print('[discover] not logged in', file=sys.stderr)
        return []
    # "Following" rather than "For you" when the tab is present — same reason.
    try:
        tab = page.get_by_role('tab', name=re.compile('^Following$'))
        if tab.count():
            tab.first.click(timeout=4000)
            page.wait_for_timeout(3000)
    except Exception:
        pass
    cards = _cards_from_page(page, limit_scrolls=4)
    for c in cards:
        c['source'] = 'feed'
    return cards


def from_topics(page, n_queries=2):
    cards = []
    for q in random.sample(TOPIC_QUERIES, min(n_queries, len(TOPIC_QUERIES))):
        dated = f'{q} within_time:24h'
        print(f'[discover] topic: {dated}', file=sys.stderr)
        for c in _search(page, dated, sort='top'):
            c['source'] = 'topic'
            cards.append(c)
    return cards


def discover(page, sources=('targets', 'feed', 'topic'), shortlist=8,
             skip_url=None, skip_author=None):
    """Collect cards from every enabled source and return the ranked shortlist.

    `skip_url` and `skip_author` are the caller's dedupe rules (already replied
    to this post, already replied to this account inside the cooldown). They run
    here so a duplicate never costs a page load.
    """
    handles, excluded = load_targets()
    target_set = {h.lower() for h in handles}

    cards = []
    if 'targets' in sources:
        cards += from_targets(page, handles)
    if 'feed' in sources:
        cards += from_feed(page)
    if 'topic' in sources:
        cards += from_topics(page)

    seen, ranked = set(), []
    for c in cards:
        url, author = c.get('url', ''), (c.get('author') or '').lower()
        if not url or url in seen:
            continue
        seen.add(url)
        if not author or author in excluded or author == 'vishalpanwarr':
            continue
        if skip_url and skip_url(url):
            continue
        if skip_author and skip_author(author):
            continue
        s = score_card(c['text'])
        if s <= 0:
            continue
        c['card_score'] = s
        c['is_target'] = author in target_set
        # A target's post outranks a stranger's before either is opened, so the
        # page loads are spent on the list first.
        c['rank'] = s + (100 if c['is_target'] else 0)
        ranked.append(c)

    ranked.sort(key=lambda c: c['rank'], reverse=True)

    # One candidate per author per run. The 48 hour cooldown only knows about
    # authors already replied to, so it cannot see two posts by the same account
    # inside a single shortlist: a 2026-09-16 dry run surfaced three posts by
    # @gp_pulipaka at once, which at two replies a run is the pair of replies to
    # one handle that the cooldown exists to prevent. Keeping the highest ranked
    # post also spends the run's page loads on distinct accounts.
    best_per_author, deduped = set(), []
    for c in ranked:
        a = (c.get('author') or '').lower()
        if a in best_per_author:
            continue
        best_per_author.add(a)
        deduped.append(c)
    ranked = deduped

    by_source = {}
    for c in ranked:
        by_source[c['source']] = by_source.get(c['source'], 0) + 1
    print(f'[discover] {len(cards)} cards, {len(ranked)} pass the card filter '
          f'({by_source or "none"})', file=sys.stderr)
    return ranked[:shortlist]
