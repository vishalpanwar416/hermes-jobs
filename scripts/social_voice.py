"""Shared voice layer for every social growth job.

One place that decides how Vishal sounds on X and LinkedIn, so replies, comments
and posts all read like the same person wrote them.

Two things happen here:

  draft(...)     asks the configured model for text, with a system prompt tuned
                 to sound like a working engineer rather than a content account.
  humanize(...)  scrubs the output afterwards. The model does not reliably obey
                 style rules, so the mechanical tells (dashes, bullets, hashtags,
                 "Great point!" openers) get stripped in code, not by asking nicely.

Anything that posts on Vishal's behalf should import from here rather than
growing its own copy of the persona.
"""

import os
import re
import sys
import json
import random
import urllib.error

HERMES_ENV = os.path.expanduser('~/.hermes/.env')
HERMES_CONFIG = os.path.expanduser('~/.hermes/config.yaml')

API_URL = 'https://openrouter.ai/api/v1/chat/completions'

# Drafting a two line social reply does not need the gateway's main model.
# Pinned rather than read from config so a future config change cannot silently
# put this hot path (it runs on every reply, comment and post) onto an expensive
# reasoning model again.
VOICE_MODEL = 'google/gemini-2.5-flash-lite'


class _Skip:
    """Sentinel: not worth replying to at all."""
    def __repr__(self):
        return '<SKIP>'


SKIP = _Skip()


# ---------------------------------------------------------------------------
# who he is
# ---------------------------------------------------------------------------

BACKGROUND = """Vishal Panwar. Backend and AI engineer.
Works in Go, Node.js, Python, FastAPI, Next.js, Kubernetes, GCP.
Real things he has shipped and can speak to honestly:
  Go APIs running past 1K RPS, cutting p95 latency by profiling allocations
  with pprof, sync.Pool for buffer reuse, bounded worker pools instead of
  unbounded goroutines, GOMEMLIMIT tuning.
  Redis and Postgres caching under load, stale while revalidate, avoiding
  thundering herd on cache expiry.
  Kubernetes failover drills on GCP, real disaster recovery testing.
  RAG and LLM agent work, semantic chunking, hybrid retrieval, evals.
  multi-qr, an open source npm package for QR codes that carry more than
  one action. github.com/vishalpanwar416/multi-qr
  Portfolio at vishalpanwar.in
Currently open to backend and AI engineer roles."""


# The style rules are deliberately blunt and negative. Positive instructions
# like "sound natural" do almost nothing; naming the exact tells works better.
VOICE = """Write the way a real engineer types a quick reply between tasks.

Never use these. They are what make text look machine written:
  Dashes as punctuation. No em dash, no en dash, no " - " joining clauses.
    Rewrite with a comma, a full stop, or split the sentence.
  Bullet points, arrows, numbered lists, "1." "2." "3.", bullets or arrows.
  Hashtags. Any hashtag at all.
  The "not just X, it's Y" construction. The "X isn't about Y. It's about Z"
    construction. Both are dead giveaways.
  Opening with praise: "Great point", "Absolutely", "This is spot on",
    "Couldn't agree more", "Well said", "100%", "Exactly this".
  Words like: delve, leverage, robust, seamless, elevate, unlock, harness,
    game changer, crucial, testament, landscape, realm, tapestry, underscore.
  Rhetorical question openers. Summary sign offs like "At the end of the day".
  Emoji, except very rarely and never more than one.

Do this instead:
  Start with the actual thought. No preamble.
  One concrete specific detail beats three general claims. Name the tool, the
    number, the failure mode.
  Contractions. Short sentences. A fragment is fine.
  It is fine to disagree, or to say what did not work for him.
  Sometimes two sentences is the whole reply. Do not pad to look thorough.
  Only mention his portfolio or repo if they actually asked for a link.
  Never invent employers, metrics, or projects beyond the background given."""


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_env_key(name='OPENROUTER_API_KEY'):
    val = os.environ.get(name)
    if val:
        return val.strip()
    try:
        with open(HERMES_ENV) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f'{name}='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def configured_model(default='google/gemini-3.7-flash'):
    try:
        with open(HERMES_CONFIG) as fh:
            in_model = False
            for line in fh:
                if line.startswith('model:'):
                    in_model = True
                    continue
                if in_model:
                    if line.startswith((' ', '\t')):
                        if 'default:' in line:
                            return line.split('default:', 1)[1].strip()
                    else:
                        break
    except OSError:
        pass
    return default


# ---------------------------------------------------------------------------
# humanizing
# ---------------------------------------------------------------------------

# Punctuation dashes only. Hyphens inside real compound words (real-time,
# low-latency, open-source) are ordinary English and are left alone.
_DASH_PATTERNS = [
    (re.compile(r'\s*[—–]\s*'), ', '),   # em dash, en dash
    (re.compile(r'\s+-{1,2}\s+'), ', '),           # " - " and " -- "
    (re.compile(r'^\s*[-•–—>*▪◦·]\s+', re.M), ''),        # list markers
    (re.compile(r'^\s*(?:→|=>|➜|▶|»)\s*', re.M), ''),      # arrow bullets
    (re.compile(r'^\s*\d+[.)]\s+', re.M), ''),             # "1." "2)" listicles
]

_STRIP_PREFIXES = [
    'great point', 'great question', 'great post', 'absolutely', 'exactly this',
    'exactly', 'this is spot on', "couldn't agree more", 'could not agree more',
    'well said', 'so true', 'love this', 'totally agree', 'spot on', 'this',
    'agreed', 'facts', 'nailed it', 'well put', 'so much this', '100%',
]

# The praise must be followed by punctuation, optionally after the person's name
# ("Great point, Shalini!"), otherwise "Exactly the same thing happened to us"
# would lose its first word. The name is consumed too, since stripping only the
# praise leaves a dangling "Shalini!" at the front.
_PRAISE_RE = re.compile(
    r'^(?:' + '|'.join(re.escape(p) for p in _STRIP_PREFIXES) + r')'
    r'(?:,?\s+[A-Z][a-z]+)?'
    r'\s*[,.!:;]+\s*',
    re.I,
)

_BANNED_WORDS = {
    # Multi word entries must come first: replacing "delve" alone inside
    # "delve into" leaves "look at into".
    'delve into': 'look at', 'delving into': 'looking at',
    'delve': 'look at', 'leverage': 'use', 'leveraging': 'using',
    'robust': 'solid', 'seamless': 'smooth', 'seamlessly': 'smoothly',
    'elevate': 'improve', 'unlock': 'open up', 'harness': 'use',
    'crucial': 'important', 'utilize': 'use', 'utilizing': 'using',
    'game changer': 'big improvement', 'game-changer': 'big improvement',
}


def _strip_opening_praise(text):
    rest = _PRAISE_RE.sub('', text, count=1).strip()
    if not rest:
        return text  # the praise was the entire message; leave it alone
    return rest[0].upper() + rest[1:]


def humanize(text, limit=None):
    """Scrub the mechanical tells out of model output.

    The model ignores style rules often enough that this has to run on every
    string before it is posted, including text a cron agent wrote rather than
    draft() below.
    """
    if not text:
        return text

    text = text.replace(' ', ' ')

    for pattern, repl in _DASH_PATTERNS:
        text = pattern.sub(repl, text)

    # Hashtags, including trailing hashtag blocks.
    text = re.sub(r'(?:^|\s)#\w+', ' ', text)

    # Neither X nor LinkedIn renders markdown, so `code`, **bold** and _italic_
    # all show up as literal punctuation in the posted text.
    text = re.sub(r'`{1,3}([^`]+)`{1,3}', r'\1', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'(?<!\w)[*_]([^*_\n]+)[*_](?!\w)', r'\1', text)

    for bad, good in _BANNED_WORDS.items():
        text = re.sub(rf'\b{re.escape(bad)}\b', good, text, flags=re.I)

    # "It's not just about speed, it's about reliability" -> keep the second half,
    # which is the part that actually says something.
    text = re.sub(
        r"\b(?:it'?s|this is|that'?s)\s+not\s+just\s+(?:about\s+)?[^,.;]{1,60}[,;]\s*"
        r"(?:it'?s|this is|that'?s)\s+(?:about\s+)?",
        '', text, flags=re.I)

    text = _strip_opening_praise(text.strip())

    # Collapse whitespace but keep deliberate paragraph breaks.
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r'\s+([,.!?])', r'\1', text)
    text = text.strip().strip('"').strip()

    # Rewrites above can leave the line starting mid sentence.
    if text and text[0].islower():
        text = text[0].upper() + text[1:]

    if limit and len(text) > limit:
        cut = text[:limit]
        # Prefer to end on a sentence rather than mid word.
        for sep in ('. ', '! ', '? ', '\n'):
            if sep in cut:
                cut = cut.rsplit(sep, 1)[0] + sep.strip()
                break
        text = cut.rstrip(' ,')

    return text


def has_ai_tells(text):
    """Report leftover tells. Used by the self check, not to block posting."""
    found = []
    if re.search(r'[—–]', text):
        found.append('em/en dash')
    if re.search(r'\s-{1,2}\s', text):
        found.append('spaced hyphen')
    if re.search(r'(?:^|\s)#\w+', text):
        found.append('hashtag')
    if re.search(r'(?:^|\n)\s*(?:[•>*▪◦·]|→|=>|➜|▶|»|\d+[.)])\s', text):
        found.append('bullet')
    if re.search(r'\bnot just\b.{0,40}\bit\'?s\b', text, re.I):
        found.append('"not just X, it\'s Y"')
    for word in _BANNED_WORDS:
        if re.search(rf'\b{re.escape(word)}\b', text, re.I):
            found.append(word)
    return found


# ---------------------------------------------------------------------------
# drafting
# ---------------------------------------------------------------------------

def draft(task, context, limit=240, temperature=0.85, allow_skip=True):
    """Ask the model for text in Vishal's voice.

    Returns the humanized string, SKIP when the model declines, or None when the
    call failed so the caller can fall back to whatever it did before.
    """
    api_key = load_env_key()
    if not api_key:
        return None

    import urllib.request

    skip_rule = ("\nIf there is nothing worth saying, or the input is spam, an ad, "
                 "engagement bait, or hostile, reply with exactly: SKIP") if allow_skip else ""

    system = f"{BACKGROUND}\n\n{VOICE}\n\nHard limit {limit} characters.{skip_rule}"

    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': f"{task}\n\n{context}"},
    ]

    def build(reasoning):
        return json.dumps({
            'model': VOICE_MODEL,
            'provider': {'only': ['google-ai-studio']},
            'reasoning': reasoning,
            'max_tokens': 700,
            'temperature': temperature,
            'messages': messages,
        }).encode('utf-8')

    def call(payload):
        req = urllib.request.Request(
            API_URL, data=payload,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))

    # Reasoning off entirely: for a short social reply it adds nothing and its
    # tokens bill at the output rate. Some Google endpoints refuse to disable it
    # (3.7-flash returns 400 "Reasoning is mandatory"), so fall back to the
    # smallest budget allowed rather than failing the draft.
    try:
        try:
            body = call(build({'enabled': False}))
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            print('[voice] endpoint requires reasoning, retrying minimal', file=sys.stderr)
            body = call(build({'effort': 'minimal', 'exclude': True}))

        choice = body['choices'][0]
        if choice.get('finish_reason') == 'length':
            print('[voice] hit token cap, discarding partial text', file=sys.stderr)
            return None
        raw = (choice['message'].get('content') or '').strip()
    except Exception as e:
        print(f'[voice] draft failed: {e}', file=sys.stderr)
        return None

    if not raw:
        return None
    if raw.upper().startswith('SKIP'):
        return SKIP

    cleaned = humanize(raw, limit=limit)
    if not cleaned:
        return None

    tells = has_ai_tells(cleaned)
    if tells:
        print(f"[voice] tells surviving scrub: {', '.join(sorted(set(tells)))}", file=sys.stderr)

    return cleaned


def match_length(their_text, ceiling):
    """Size the reply to what they actually wrote.

    A one line question gets a one line answer. Answering "nice work" with four
    technical sentences is the clearest tell that nobody is home, and answering a
    detailed technical comment with five words reads as dismissive.

    Returns (character_cap, instruction_for_the_model).
    """
    body = (their_text or '').strip()
    length = len(body)
    sentences = len([s for s in re.split(r'[.!?]+', body) if s.strip()])
    is_question = body.rstrip().endswith('?')
    # Does what they wrote actually contain something to engage with?
    technical = len(re.findall(
        r'\b(latency|throughput|cache|caching|postgres|redis|kubernetes|k8s|goroutine|'
        r'concurrency|schema|pipeline|deploy|scaling|memory|allocation|profil\w*|index|'
        r'query|architecture|rag|llm|embedding|retrieval|api|race|deadlock|shard\w*|'
        r'p9[59]|pprof|gc|sync\.pool|gomemlimit|grpc|kafka|docker|terraform|mutex|'
        r'throttl\w*|backpressure|idempoten\w*|migration|replica|failover)\b',
        body, re.I))

    # Questions are checked before the throwaway branch: a short question still
    # deserves a real answer, it just does not deserve a long one.
    if is_question:
        return min(200 if length < 160 else 280, ceiling), (
            "They asked a direct question. Answer it directly in one or two "
            "sentences. No preamble, no extra context they did not ask for.")

    if length < 80 and not technical:
        # "nice one", "congrats", "sounds fun"
        return min(120, ceiling), (
            "They wrote one short throwaway line. Reply in one short line, under "
            "twelve words. Do not add technical detail they did not ask for.")

    if length < 220 or (sentences <= 2 and technical <= 1):
        return min(240, ceiling), (
            "Keep it to one or two sentences. Make one specific point.")

    if technical >= 3 or length > 500:
        return ceiling, (
            "They wrote something substantial and technical. Match that. Three or "
            "four sentences, and engage with the specific thing they raised rather "
            "than the general topic.")

    return min(320, ceiling), (
        "Two or three sentences. Pick the one thing worth responding to.")


def draft_reply(author, their_text, platform):
    """A reply to something a specific person said."""
    ceiling = 240 if platform == 'x' else 400
    limit, length_rule = match_length(their_text, ceiling)
    return draft(
        task=(f"Someone on {platform} wrote this. Write Vishal's reply to them.\n"
              f"Length: {length_rule}"),
        context=f"Author: {author}\nWhat they wrote:\n{their_text[:1500]}",
        limit=limit,
    )


def draft_comment(author, post_text, platform='linkedin'):
    """A comment on a stranger's post, for reach."""
    ceiling = 400 if platform == 'linkedin' else 240
    limit, length_rule = match_length(post_text, ceiling)
    return draft(
        task=("Write Vishal's comment on this post. The goal is that other engineers "
              "reading the thread find it worth reading, so add a specific technical "
              "observation from his own experience. Do not compliment the author.\n"
              f"Length: {length_rule}"),
        context=f"Post author: {author}\nPost:\n{post_text[:2000]}",
        limit=limit,
    )


POST_ANGLES = [
    "a specific bug or failure that cost real time, and what actually fixed it",
    "something he believed about backend work that turned out to be wrong",
    "a concrete before and after number from profiling or load work, and the change behind it",
    "a small tool or trick he uses that most people do not know about",
    "an opinion about engineering practice he would defend in a code review",
    "what building multi-qr taught him that he did not expect",
    "a thing juniors get told that is bad advice in production",
    "a tradeoff he got wrong once and now handles differently",
]


def draft_post(recent_posts=(), platform='x'):
    """An original post. Recent posts are passed in so it stops repeating itself."""
    angle = random.choice(POST_ANGLES)
    avoid = ''
    if recent_posts:
        joined = '\n---\n'.join(p[:200] for p in recent_posts[:8])
        avoid = (f"\n\nHe has already posted these recently. Do not repeat their topic, "
                 f"their opening line, or their structure:\n{joined}")
    return draft(
        task=(f"Write one original {platform} post from Vishal. Angle: {angle}. "
              f"Write it as plain sentences and short paragraphs. No list, no bullets, "
              f"no numbered steps."),
        context=f"Write the post now.{avoid}",
        limit=270 if platform == 'x' else 700,
        temperature=0.95,
        allow_skip=False,
    )


if __name__ == '__main__':
    # Quick self check: python social_voice.py
    samples = [
        "Great point! This is a game changer - it will help you leverage robust "
        "pipelines and seamlessly delve into the landscape. #DevOps #Golang",
        "It's not just about speed, it's about reliability — and that matters.",
    ]
    print('humanize() self check\n')
    for s in samples:
        print('  in :', s)
        print('  out:', humanize(s))
        print('  tells left:', has_ai_tells(humanize(s)) or 'none')
        print()
