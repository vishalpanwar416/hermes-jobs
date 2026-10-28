# X Growth Plan — @VishalPanwarr

Goal: followers that convert into genuine dev peers, founder relationships, VC awareness, and
inbound job/consulting conversations. Not follower count for its own sake.

Written 2026-09-05. Supersedes the ad-hoc strategy previously encoded in
`~/.hermes/skills/social-media/dev-social-growth/SKILL.md`, which has been updated to match.

**Status: applied 2026-09-05. Revised 2026-09-06 after 36 hours of live data.** All seven jobs
are scheduled and firing. Outcomes have not moved and three mechanisms are inert — see the
revision note below, section 3, section 5, and [X_TUNING_LOG.md](X_TUNING_LOG.md). Section 8
(DMs) is deliberately not automated at all.

### Revision note — 2026-09-06, 19:50 IST

Two days of running the plan surfaced one config bug that invalidates most of the reply
strategy, one prospecting result that changes how the target list gets built, and one
unverified assumption about posting. In order of cost:

**The reply cron has never run the targeted code.** `social_x_reply_outbound` is an *agent* job
(`no_agent: false`) whose prompt still reads "Scan X notifications and feed." It names no
script, so the agent free-picks one: 21 historic runs chose `auto_reply_growth.py`, 7 chose
`x_feed_engage.py`, and **all four runs on 2026-09-06 chose the feed script.** Every targeting
change made on 09-05 — `from:` queries against `x_targets.json`, the 48-hour author cooldown,
`DAILY_REPLY_CAP` — lives in `auto_reply_growth.py` and is therefore dead code from the
scheduler's point of view. The tuning log's "the `from:` target query ran first in every pass"
came from a manual invocation, not from the cron. This is a config bug, not a strategy failure,
and it is the first thing to fix.

**The raised quality bar plus a feed source equals zero output.** Today's three evening runs all
exited `[SILENT]`, correctly: the feed offered "pitch me your startup" threads, engagement polls
and ads, and the bar rejected them. One reply went out in 24 hours, at 01:28. The bar is right.
The source is junk. Raising the bar without switching the source turns the engine off rather
than pointing it somewhere better — which is the honest reading of section 13 arriving early.

**Prospecting yields the wrong tier at the wrong rate.** `x_prospect` failed silently at its
06:00 slot and succeeded on a 16:15 retry, harvesting 4 candidates from 3 seeds. Three are
juniors or students (`danielcluff`, `josephsir73112`, `newageretronerd` — "cs student | 22"),
exactly the tier the plan exists to stop attracting. One is a real find: `ccgong`, investing at
Menlo Ventures, misfiled as tier 2 when she is tier 3. At 4 candidates a week from a weekly
cron, the 150-handle list is a nine-month project. That is not a schedule, it is a stall.

**Posting is unverified.** Zero posts published in the last 7 days per `x_metrics`, the 09-05
20:30 run failed outright (`RuntimeError: Hermes can't reach the model provider`) and nothing
retried it, and there is no `posts` table in `x_growth.db` to check against. The retimed slot —
"biggest single win, zero effort" — has still never successfully fired. It may be that the old
14:00 job was not publishing either, in which case the post cadence has been fictional for
longer than this plan has existed.

Sections 3, 5, 6, 10 and 12 are updated below to match. Nothing in sections 2, 4, 7, 8 or 11
changed — the strategy is not what broke.

---

## 1. What the data says

Pulled from `~/.hermes/data/x_growth.db` and the live scheduler on 2026-09-05.

| Signal | Current state |
| --- | --- |
| Outbound replies | 73 total, 20/day on Aug 29 decayed to 5-8/day now |
| Inbound replies received | 4. Total. Ever. |
| Likes given | 0 — no automation exists |
| Follows made | 0 — no automation exists |
| DMs sent | 0 — no automation exists |
| Posts | 1/day, `0 14 * * *` (14:00 IST = 01:30 PT) |
| Reply source | X topic search, `min_faves:5`, 3 sampled queries per run |

Four problems, in order of how much they cost you.

**Targeting is undirected.** `auto_reply_growth.py` runs eight topic searches with
`min_faves:5` and replies to whatever ranks. The machinery around it is genuinely good — a
model drafts against the actual tweet, `social_voice` strips the AI tells, there's dedupe and a
spam gate — but topic search finds whoever is loud on a subject, not whoever matters in it. The
result is visible in the top-authors table: `MentorWebDev`, `TanzilaSha9574`, `vibeonX69`,
`codewithkhalil`, `0xlelouch_`. Engagement-farm accounts, all of them. Out of 73 replies,
roughly three landed on someone who matters (`wagslane`, `venkateshdotdev`, `mihail_eric`). A
4% hit rate.

Nothing here needs rewriting. It needs pointing somewhere.

**Two of six levers are in use.** You reply and you post. You never like, never follow, never
quote-tweet, never DM, never bookmark. Following and liking are how X's graph learns who you
are, and they are how a target notices you before you ever speak. Replying cold to a stranger
with no prior signal reads as spam — because structurally it is.

**The content is commodity.** "If I were learning X from scratch today" is a copied viral
format with five hashtags stapled to it. Hashtags are downranked on X and mark a post as
marketing. This format attracts juniors and follow-farmers. Meanwhile you have actually shipped
things — a 22-job autonomous agent fleet, `multiqr` on npm, an embedding-based creator matching
engine, backend at Bluecopa — and none of it appears on your timeline. You are posting
tutorials about work instead of posting the work.

**4 inbound replies is the real number.** Everything else is vanity. Nobody is talking back,
which means nothing you post is a conversation opener.

---

## 2. The shift

Stop broadcasting to strangers. Start being visibly useful to 150 specific people.

Growth on X for a backend/AI engineer does not come from reach. It comes from a small number of
respected accounts recognising your name, because you showed up in their replies with something
they hadn't considered — repeatedly, over weeks. Followers, DMs and job reachouts are the
second-order effect of that. Reverse the order and you get 5,000 followers who can't help you.

Concretely: replace the home feed with a curated target list, add the four missing levers, and
replace tutorial posts with proof-of-work.

---

## 3. The target list — the core mechanism

Everything below depends on this file. Build it first.

`hermes-jobs/x_targets.json`, four tiers, ~150 accounts:

```json
{
  "tier_1_inner": [
    { "handle": "wagslane",       "why": "boot.dev founder, Go audience overlap", "cadence": "weekly" }
  ],
  "tier_2_founders": [],
  "tier_3_vc": [],
  "tier_4_peers":  []
}
```

**Tier 1 — Inner circle (15 accounts).** People whose replies you want to be a regular in.
Go/backend/AI-infra voices with 10k-200k followers who actually reply to their own threads.
Seeds: `wagslane`, `mihail_eric`, `venkateshdotdev`, plus Go core contributors, `pgvector` and
Redis maintainers, LangChain/LlamaIndex infra people, and the 3-4 Indian backend accounts with
genuine engineering depth. You engage with every one of these, every week, without fail.

**Tier 2 — Founders (50).** Seed/Series-A technical founders in dev tools, AI infra, and
fintech-adjacent data (your Bluecopa domain). Sources: YC W25/S25/W26 batch pages, Product Hunt
weekly winners, anyone shipping a changelog publicly. These are your future job and consulting
conversations. They notice engineers who understand their problem unprompted.

**Tier 3 — VC (25).** Partners and principals who post technical takes, not just deal
announcements. Focus on the ones who fund infra: Boldstart, Amplify, Essence, Blume, Peak XV
seed. VCs do not fund followers, but they do maintain mental lists of strong engineers for
their portfolio companies. That is the actual path from VC attention to a job.

**Tier 4 — Peers (60).** Engineers at your level building in public. Reciprocity is highest
here and this tier drives raw follower growth. Rotate — drop anyone who never engages back
after 6 weeks.

**Build it this way, not by hand.** For each tier-1 seed, pull their following list and their
recent repliers, filter for bio keywords (`founder`, `building`, `CTO`, `partner`, `infra`,
`Go`, `distributed`), drop anyone under 500 followers or over 500k, drop anyone whose last 20
posts are all threads-with-hashtags. That gets you to 150 in one pass. Review it manually once
— this list is the whole strategy, it deserves an hour of your attention.

Maintenance: monthly prune, add 10 / drop 10.

**Revised 2026-09-06 — the automated path does not get there on its own.** One real run of
`x_prospect` produced 4 candidates from 3 seeds, 3 of them students or juniors. Three things
are wrong with it and all three are fixable:

- *It only harvests repliers, not the seeds' following lists.* The paragraph above says both.
  The script implements the first half. A seed's *following* list is the higher-quality half —
  who `wagslane` chose to follow is a stronger signal than who showed up in his replies — and it
  is where tier 1, 2 and 3 candidates actually live. Repliers are a tier-4 pool almost by
  definition.
- *No follower floor is applied.* The plan says drop under 500 followers; the harvest recorded
  `"followers": null` for every candidate and kept them all. A student with 40 followers is
  noise, and worse, promoting one costs a follow slot that should go to a founder.
- *Weekly is the wrong cadence for a bootstrap.* Weekly is right for maintenance of a full list.
  While the list is under 50 handles, this should run daily; the yield problem compounds with
  the frequency problem otherwise.

**The list will not be fully automated, and planning as if it will is the mistake.** Tier 1 is
15 accounts and it is the tier everything else keys off. Fifteen handles is twenty minutes of
Vishal's attention with X open, and it unblocks `x_target_engage` (floor of 10), the `from:`
reply queries, and the whole warm-up sequence in section 7. Automation should be aimed at tiers
2 and 4, where volume matters and a wrong handle is cheap. Tier 1 and tier 3 are judgment calls
and should be seeded by hand.

**Promotion stays human, but give it a queue.** `x_target_candidates.json` is the right design —
nothing is followed or replied to until promoted. Keep that. Add the candidate's follower count
and last-5-post topics to each entry so a promotion decision takes seconds rather than requiring
a visit to the profile. `ccgong` is promotable today, to tier 3, not the tier 2 the script
suggested.

**Revised 2026-09-10 — the daily harvest was re-checking the same profiles.** Four daily runs
produced 19 candidates and then one a day, because the profile-check budget went to the first 25
handles of @wagslane's following list every morning: existing candidates and already-rejected
profiles did not count as "known". They do now, rejects are persisted with a reason, and the
walk seeds from tiers 1–3 rather than tier 1 alone, since a VC's following list is where the
founders and other VCs are. Students and brand accounts are filtered on bio; educator, DevRel
and company-shaped bios are kept but annotated, because those are the judgment calls. Promotion
is still human. `@sachaarbonel` was promoted to tier 2 on 09-10; everything else in the file is
DevRel, a brand account, or too small.

---

## 4. Posts

**Cadence: 5/week, not 7.** Mon/Tue/Wed/Thu + one Sunday long-form. Skipping days is fine;
posting filler is not.

**Timing: 09:00 IST and 20:30 IST.** 09:00 catches India morning, 20:30 IST is 08:00 PT and
catches the US west coast waking up — that is where the founders and VCs are. Your current
14:00 IST slot hits 01:30 PT and is the single worst hour of the day for that audience. Move
the daily post to 20:30 IST and it will do more work with zero content changes.

**Kill the hashtags.** All of them. They suppress reach on X and signal marketing. The only
exception is `#buildinpublic` on a genuine build-log post, and even that is optional.

**Retire "If I were learning X from scratch" to once a fortnight.** It is not worthless — it
does pull impressions — but it is 100% of your output and it attracts the wrong tier. Cap it.

**The new mix, per week:**

*Two build logs (Mon, Wed).* What you actually shipped, with the number attached. You have
material sitting unused: 22 autonomous cron jobs applying to your own job pipeline, a
`multiqr` npm package, the bait-account muting heuristic with 46 accounts caught, an embedding
matcher for creator-brand fit. Write these as "here is the thing that broke and what it cost
me." Post the failure, not the success — failures get replies, successes get likes.

*One teardown (Tue).* Pick a system and explain a tradeoff nobody talks about. Redis
thundering herd on expiry, `sync.Pool` and why it silently doesn't help under GC pressure,
connection-pool exhaustion as a cascading failure, `GOMEMLIMIT` versus container limits. These
are the topics your replies already show you know cold. A post you can defend in the replies is
worth ten you can't.

*One opinion (Thu).* A defensible take with a reason. "Most RAG latency problems are chunking
problems, not vector-DB problems." Opinion posts are what get quote-tweeted by tier 1.

*One Sunday long-form.* A 5-8 tweet thread or a single dense post going deep on something you
built that month. This is the post you pin and the one people read before deciding to follow.

**Revised 2026-09-10 — the account is X Free and nothing had ever been published.** 280
characters per tweet, no long posts. Every queued post was 450–975 characters and the publisher
reported success on every one of them while X refused every one; the timeline had nothing newer
than 28 August. Posts now live in the queue as an explicit `tweets` list (1–5 tweets, each under
270), the publisher builds a thread in the composer and only records a post after finding it on
the profile, and `x_post_publish.py --validate` refuses anything over the limit at write time.
Rules for a thread that gets read: tweet 1 stands alone as the whole claim or the thing that
broke, no link and no hashtag in it, the last tweet carries the open seam. A 5-tweet Sunday
long-form is the ceiling; a 2-tweet build log is fine.

**Format rules that carry over:** conversational prose, no bullet lists, no em-dashes, under
280 chars per tweet, contractions, personal specifics. Those preferences in the existing skill
are good — keep them.

**One rule that isn't in the skill yet: always end a post with a real question or an open
seam.** Not "what do you think?" — something like "still haven't found a clean way to bound
this, curious if anyone has." That is how you manufacture inbound replies, and inbound is your
weakest number by far.

---

## 5. Comments (replies) — your highest-leverage lever

This is where founders and VCs actually discover you. Rebuild it entirely.

**Source from the target list, not topic search.** Poll tier 1 and tier 2 timelines directly
for posts published in the last 30 minutes. Replying inside the first 30 minutes of a post from
a 50k-follower account is worth more than fifty replies to strangers, because you are visible
to everyone who arrives after you.

**Revised 2026-09-06 — make the job deterministic before tuning anything else in this section.**
The instruction above was implemented in `auto_reply_growth.py` and then never invoked, because
`social_x_reply_outbound` is an agent job whose prompt describes a goal ("scan notifications and
feed") instead of naming a command. The agent picks a script per run and has lately been picking
`x_feed_engage.py`, which does exactly the undirected feed scanning this plan was written to
eliminate. The fix is to convert the job to `no_agent: true` running `auto_reply_growth.py`
directly, the same shape as `x_target_engage` and `x_prospect`, which have never drifted.

The general lesson, worth applying to every job in this system: **a cron whose prompt states an
objective will drift; a cron that names a script cannot.** Reserve agent-mode jobs for work that
genuinely needs judgment at runtime, and note that drafting the reply text is already an
in-script model call — the job wrapper does not need to be an agent to get that.

**Zero replies is a legitimate outcome, but not from this source.** Today's evening runs all
returned `[SILENT]` and were right to: the feed had nothing worth replying to. Do not respond to
that by lowering the bar. Respond by changing what gets scanned. Once `from:` targeting is
actually running, a `[SILENT]` run means the target list was quiet in that window, which is
real information; today it only means the algorithm served ads.

**The topic-search fallback is off by default (2026-09-07).** This follows directly from making
the job deterministic, and it is the one place where fixing the config changed the strategy
rather than just enforcing it. `auto_reply_growth.py` was written to try the target list first
and fall back to topic search when no target had posted recently. That was safe while an agent
wrapped it, because the model looked at the topic results and usually refused them. As a script
on a 90-minute timer it has no such judgement: the first dry run after the switch drafted a
reply to a "CANCEL your weekend plans" thread, and at a 12/day cap it would have posted about a
dozen of those daily — a faster, more reliable version of the 4% hit rate this plan was written
to end.

So the fallback is now opt-in (`--allow-topic-fallback`). With no target posting in the window,
the job posts nothing and says so. The cost is real and should be named: at a 4-handle list this
means very few replies for a while. That is the correct trade. Reply volume was never the
constraint — 73 replies bought 4 inbound — and a quiet reply engine makes the list bottleneck
impossible to ignore, where a busy one papers over it with exactly the replies that damage the
account's standing with the people it is trying to reach.

Turn the fallback back on only if the list is healthy and the target searches are still coming
up empty, which would mean the window is too tight rather than the list too small.

**Revised 2026-09-10 — a reply is judged against the post before it is posted.** The first
target-list reply the deterministic job delivered went to @wagslane, one hour after a joke about
regulating online learning, and opened "Unbounded goroutines will take down your learning
platform faster than any lawsuit". Targeting, timing and voice were all correct. The reply had
nothing to do with the tweet. The same failure is in every commenting path: the X feed script
pitched multi-qr under a "looking to connect with managers" post, and LinkedIn got the same
canned sentence under four posts in six minutes.

The fix is structural, not another prompt rule. `social_voice.judge_relevance(post, reply)`
reads both texts and scores 1–5 how directly the reply engages with what was said; anything
under 4 is dropped, and an unscorable pair is dropped too. On the historic bad replies it
returns 1, 1 and 3. Every path that posts on Vishal's behalf runs it: `auto_reply_growth.py`,
`x_feed_engage.py`, and `linkedin_growth_comments.post_comment`, which now also refuses a
comment whose text or opening already went out in the last 60 days. The canned reply pools in
`auto_reply_growth.py` are gone — a fallback that posts boilerplate when the model is down is
the bot behaviour this plan exists to end. The quality bar above ("a specific fact, a number, a
failure mode, or a disagreement") is now enforced by code rather than hoped for.

**Revised 2026-09-16 — the target list alone cannot feed this job, so the post sources widened
and the topic fallback came back on.** The paragraph above says to turn the fallback on only if
the list is healthy and the target searches still come up empty, on the grounds that an empty
window means the window is too tight rather than the list too small. The data says otherwise: 75
consecutive runs between 09-10 and 09-15 found zero posts and sent zero replies. The list is not
healthy — it holds 5 of a planned 150 — and waiting for it to fill has cost six days of silence
with no mechanism in place to make it fill faster.

What did not change: the target list is still the only thing that privileges an author, and code
still never adds a handle to it. The prospecting pass proposes, a human promotes. That gate is
the thing this plan is built on and widening it automatically would be the actual mistake.

What changed is that the job now considers posts from three sources (target list, home timeline,
topic search) and gates non-target authors on facts it could never previously see, because it now
opens the post before replying to it: follower count, post age, replies already underneath, and
the author's bio. The old topic search failed at ~4% because it scored a truncated feed card
against a keyword list. A topic post now passes six gates, four of which read the real post.

The honest risk, named: tier hit rate will fall, and for a while most replies will go to people
who are not on the list. That is the trade being made deliberately. Judge it on inbound replies —
the north star in section 11 — not on tier hit rate, which measures aim at a 5-handle list rather
than whether anyone is reading. If two weeks of off-list replies produce no inbound, narrow the
sources back to `targets` rather than lowering the score floor. See X_TUNING_LOG.md, 2026-09-16.

**Volume: 8-12 replies/day, distributed.** 6 to tier 1-2, 3-4 to tier 3-4. That is up from your
current 5-8 but pointed at 4% → 90% relevant targets. Run every 90 minutes during waking hours
rather than 6-hourly, so you catch the 30-minute window.

**Quality bar, raised.** The current bar is "matches a tech regex." The new bar: a reply must
add a specific fact, a number, a failure mode, or a disagreement. If the model can only produce
agreement or a restatement, post nothing. Your existing replies are actually decent on this —
"Bounded worker pools instead of unbounded goroutines, GOMEMLIMIT tuning" is a real reply — the
problem is purely who receives them.

**Never reply twice to the same account in 48 hours.** Currently `kmeanskaran` and
`TanzilaSha9574` have three each. That reads as stalking.

**Escalation ladder — reply, then reply to the reply.** When a target responds to you, that is
the single most valuable event in this whole system and right now nothing handles it. A human
should answer it, not the model. Route every inbound to WhatsApp via the existing `@work_report`
target and answer it yourself within the hour.

---

## 6. Likes

Currently zero. Add it — it is cheap, low-risk, and it is the warm-up before every reply.

**30-40/day, exclusively on the target list.** Never on the algorithmic feed. Likes tell X's
graph who you belong to, and it is the cheapest way to appear in a target's notifications
without saying anything.

**Sequencing matters: like 2-3 of a target's posts across a few days before your first reply to
them.** A reply from a handle they have already seen twice lands as a peer. A reply from a cold
handle lands as spam. This one change probably explains most of the 4-inbound-replies problem.

**Never like your own replies, never like more than 5 posts from one account per week.**

**Revised 2026-09-06 — split the floor so likes can start now.** `x_target_engage` has run 12
times and exited `[SILENT] target list too small` every time, because a single `MIN_LIST_SIZE`
of 10 gates likes and follows together. Those two actions do not carry the same risk. Liking
3 tier-1 accounts, capped at 5 posts per account per week, is roughly 15 likes a week aimed at
handles that were hand-picked — there is no version of that which looks like spam or trips a
rate limit. Following is the action with a ceiling worth respecting.

So: **likes floor 3, follows floor 10.** Likes start immediately against whatever is in the
list, which means the warm-up in section 7 begins accumulating on the three seeds now rather
than whenever the list crosses ten. Given that the whole theory of the 4-inbound-replies
problem is *cold handles reply into a void*, the warm-up is the part that most wants a head
start.

Keep the daily budget proportional rather than fixed while the list is small — 5 likes per
handle per week is the binding constraint, not the 30-40/day target, and that number only
becomes reachable at ~40 handles.

---

## 7. Follows

Currently zero. This is the biggest untapped lever, because on X a follow is a notification and
a notification is a free introduction.

**15-20/day, ceiling 25.** Above that X starts flagging. Follow from the target list plus the
people who reply intelligently to tier-1 posts — that last group is a genuinely good
prospecting pool and it is free.

**Follow-backs are not the point, but track them.** Follow-back rate by tier is your single best
measure of whether the list is any good. If tier 2 follow-back is under 10%, the list is wrong,
not the tactics.

**Unfollow discipline:** after 30 days, unfollow non-reciprocal tier-4 accounts only. Never
unfollow tier 1-3 — you follow them because you want their posts, not their attention. Cap
unfollows at 10/day.

**Order of operations for a new target, over about ten days:** follow → like 2-3 posts over
several days → first substantive reply → keep replying weekly → they follow back or engage →
only then, a DM if there is a real reason.

---

## 8. DMs

The endgame, and worth doing carefully — this is where the actual jobs and consulting come from.

**Never cold. Only after a target has engaged with you twice.** The trigger should be
mechanical: target replies to you, or likes your reply, twice within 30 days. Then and only then
does a DM make sense.

**Human-written, always. Do not automate the send.** Have the system draft and route the draft
to WhatsApp for approval. An automated DM that lands wrong costs you a relationship
permanently, and DM spam is the fastest way to get an account restricted.

**Format, per the existing skill's DM guidance:** one concrete story connecting their stated
problem to something you actually built. No preamble, no pitch, no "I'd love to connect." Three
sentences. Ask nothing on the first DM.

**Volume: 3-5/week maximum.**

---

## 9. Weekly cadence

| Day | Post | Replies | Likes | Follows |
| --- | --- | --- | --- | --- |
| Mon | Build log, 20:30 | 10 | 35 | 18 |
| Tue | Teardown, 20:30 | 10 | 35 | 18 |
| Wed | Build log, 20:30 | 10 | 35 | 18 |
| Thu | Opinion, 20:30 | 10 | 35 | 18 |
| Fri | — | 6 | 25 | 12 |
| Sat | — | 4 | 20 | 10 |
| Sun | Long-form, 11:00 | 6 | 25 | 12 |

Weekly totals: 5 posts, ~56 replies, ~210 likes, ~106 follows. All within X's limits with
comfortable headroom.

---

## 10. Cron changes

Against the live scheduler as of 2026-09-05:

**Change**
- `social_x_post_publish` — `0 14 * * *` → `30 20 * * *`. Biggest single win, zero effort.
- `social_x_reply_outbound` — `every 360m` → `every 90m`, and repoint `auto_reply_growth.py`
  from `x.com/home` to target-list timelines. Raise `MAX_REPLIES` from 2 to 3.
- `social_all_reply_inbound` — currently **disabled**. Re-enable it. With only 4 inbound
  replies ever, every one is precious and right now nothing is catching them.

**Add**
- `x_target_engage` — every 120m. Likes and follows from the target list, with the
  like-before-reply sequencing.
- `x_target_refresh` — weekly, Sunday 06:00. Prune and top up the list, report follow-back rate
  by tier.
- `x_dm_draft` — daily 10:00. Draft DMs for qualified targets, route to WhatsApp for approval.
  Never auto-send.

**Leave alone**
- `social_bait_muter` — working well, 46 accounts caught.
- `cron_missed_replay`.

### Revised set, 2026-09-06 — applied 2026-09-07

Everything in this section is live as of 2026-09-07 09:30 IST. Verified by dry run where a dry
run exists, and by the engagement log where it does not.

The 09-05 changes above all landed in the scheduler — schedules, re-enables and new jobs are
verified live. These are the corrections that follow from two days of running them, in priority
order.

**Fix first — one line, unblocks the most**
- `social_x_reply_outbound` → `no_agent: true`, `script: auto_reply_growth.py`. Removes the
  script-choice drift described in section 5. Without this, no amount of target-list work
  reaches the reply engine.

**Fix next**
- `x_target_engage` — split `MIN_LIST_SIZE` into `MIN_LIST_LIKES = 3` and `MIN_LIST_FOLLOWS = 10`
  per section 6. Starts the warm-up on the three seeds today.
- `x_prospect` — `0 6 * * 0` → daily at 06:00 while the list is under 50 handles, reverting to
  weekly after. Add the seeds' following lists as a source, apply the 500-follower floor, and
  record follower count and recent-topic hints on each candidate.
- `social_x_post_publish` — add a retry. The 09-05 failure was a transient provider error at
  20:30 and nothing picked it up; `cron_missed_replay` covers missed runs, not failed ones. One
  retry at 21:00 on failure would have saved that post.

**Add**
- Post tracking. There is no `posts` table in `x_growth.db`, so `posts_published` in
  `x_metrics.db` is unverifiable and currently reads 0 for 7 days. `x_post_publish.py` should
  write `(tweet_id, slot, posted_at, content)` on success. Until it does, the post half of this
  plan is being measured on faith.
- `x_target_refresh` and `x_dm_draft` remain unbuilt, and correctly so — both are downstream of
  a list that exists.

**Still leave alone**
- `social_bait_muter`, `cron_missed_replay`, `social_all_reply_inbound` (153 runs, 3 failures,
  catching inbound as intended).

---

## 11. Measure these, not followers

Weekly, in the Sunday report:

1. **Inbound replies received** — the north star. 4 total today. Target 15/week by week 8.
2. **Tier 1-2 engagement rate** — % of your replies to tier 1-2 that get a like or reply back.
   Target 25%.
3. **Follow-back rate by tier** — tells you if the list is right. Target 30% t1, 15% t2, 10% t3.
4. **Profile visits per post** — the real intent signal. Available in X analytics.
5. **Qualified DM conversations** — founders/VCs/hiring managers in your DMs. Target 2/week by
   week 12.
6. Follower count. Last. It is an output, not a lever.

---

## 12. Rollout

**Week 1 — build the list.** `x_targets.json`, 150 accounts across four tiers. Move the post
time to 20:30. Strip hashtags. Re-enable `social_all_reply_inbound`. Nothing else changes.

**Weeks 2-3 — add likes and follows.** Ship `x_target_engage` in like-only mode for a week, then
enable follows. Repoint replies at the target list. Watch for any X rate-limit warnings.

**Weeks 4-6 — fix the content.** New post mix. Two build logs a week from work you have already
done. Cap the learning-path format at once a fortnight.

**Weeks 7-12 — DMs and pruning.** Enable `x_dm_draft` with human approval. First monthly list
prune using follow-back data. By week 12 you should be able to tell whether the list or the
tactics need work, because the tier metrics separate those two things cleanly.

**Realistic expectation:** meaningful inbound — founders replying, a recruiter DM worth
answering — starts somewhere in weeks 6-10, not weeks 1-2. The mechanism is repeated recognition
by a small group, and that takes about that long regardless of volume. Anything faster is
either luck or a viral post, and neither is a strategy.

### Re-baselined 2026-09-06

Week 1 did not happen. The list was not built, so nothing downstream of it started, and the
reply engine spent the week pointed at the feed. The clock starts when tier 1 exists — not
before, because every subsequent step reads from it. Treat 09-05 and 09-06 as instrumentation
days, which is genuinely what they produced: the metrics, the review job, the prospect pipeline
and four real bug fixes all exist now and did not on 09-04.

**Week 1, restarted — three things, roughly an hour total.**
1. Vishal seeds tier 1 by hand to 15 handles. Twenty minutes with X open. This is the gate.
2. Apply the "fix first" and "fix next" items in section 10.
3. Confirm one post actually publishes at 20:30 and lands in a tracking table.

Nothing else changes and no new mechanism gets built until those three are done. The temptation
after a flat week is to add another job; the correct move is to make the seven that exist do
what they already claim to.

**Week 2 onward** proceeds as originally written — likes then follows, then content, then DMs.
The original sequencing was not the problem.

**What the two days actually proved.** The one measurement that carries signal: on the single
occasion `auto_reply_growth.py` ran with `from:` targeting, it caught `@venkateshdotdev` 46
minutes after he posted, off a list of three handles. The mechanism works. It has just never
been switched on.

---

## 13. One honest caveat

Some of what runs today is closer to spam than engagement, and it is working against the goal.
Twenty generated replies a day to strangers found on an algorithmic feed is a pattern founders
and VCs recognise instantly — it is the exact thing they mute. The fix is not more volume with
better filters, it is fewer replies to people you have deliberately chosen, warmed up first,
and can genuinely say something useful to.

Automation is right for discovery, sequencing, drafting, and never missing a 30-minute window.
It is wrong for the moment a real person replies to you. Keep the human in that loop and this
works. Automate that away and it is 5,000 followers who can't help you.
