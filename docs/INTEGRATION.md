# Integration contract and limits

## Required balance adapter

The observer looks up the loaded plugin under the exact name `balance` and reads:

- `ratings`: a mapping keyed by the integer player identifier, with CA entries
  shaped like `ratings[identifier]["ca"]["elo"]`.
- `ratings_lock`: an object exposing `acquire(False)` and `release()`; a busy
  cache is skipped without waiting.
- `switch_suggestion`: `None` or a simple suggestion exposing
  `red_player.steam_id` and `blue_player.steam_id`.

The field called `elo` is the balancer's cached rating value. The guard does not
independently verify which rating system produced it. It does not ask Redis or a
rating service for missing values. Composite/multi-player proposals are skipped;
the interface is not interchangeable with every plugin called `balance` or
`balancetwo`.

The optional join adapter looks up `auto_rebalance.last_new_player_id`. It
observes two newcomers, then checks their actual allocation after a 0.2-second
delay. Other roster changes or a dependency reload invalidate the observation.
If that interface is absent, proposal observation can still work, while join-pair
observation is unavailable.

The supplied `compat/` files are unchanged reference variants matching these
fields. Review their own configuration, dependencies, external rating-service
use, commands and existing deployment before adopting them. They are not loaded
or installed by this repository and are not covered by the 74 observer/Bob tests.

## Bob bridge

The loaded `openai_bot` must provide:

```python
comment_balance_guard(facts, is_current, finished) -> bool
```

The guard calls it on the game frame. `facts` contains only the reason category,
team color, scores, round limit, optional aggregate rating gaps/threshold and
`action_performed=False`. `is_current()` is checked before delivery;
`finished(sent)` reports delivery back on the game frame. A `True` return means
the request was accepted, not that a message has already been delivered.

The included Bob variant adds this bridge to the upstream spectator bot and
retains its existing chat behavior. It permits one pending guard worker, skips
a busy chat lock, gives the guard API call an eight-second timeout and schedules
delivery on the next game frame. The guard rejects an answer older than twelve
seconds or one for an obsolete match/roster. It accepts the exact observed swap
as an allowed later roster, because the original balancer may already have acted.

Guard requests do not read/write Bob's chat history. They reuse his system and
chat-role files, so keep those files free of anything you do not want sent to the
API provider. The new event-specific prompt is not a replacement personality.
Missing guard/chat roles, unavailable Bob, busy workers, API errors and stale
answers produce no fixed fallback comment. A guard request can occupy Bob's
shared chat lock until completion or timeout; normal Bob replies may wait.

## Data and external effects

- The guard keeps player identifiers, observations and feedback in memory. It
  writes no files, records no raw player data and does not change Redis.
- Its anonymous event payload is sent to the API through Bob, together with the
  operator's system/chat prompts. AI output is public game chat and can be
  captured by other installed chat/log/relay plugins.
- Bob's **ordinary chat path is different**: it records chat history in Redis
  and may send player names, chat and match context to the provider. Extended
  logging can record that context. The guard's narrower data path does not
  make the entire existing chatbot anonymous.
- Guard delivery disables Bob's own Discord relay for that message only. It
  can still read Bob's existing Redis message-replacement table; other server
  plugins can relay/log the message independently.
- The reference balancer can query rating services, and Bob uses the operator's
  configured API account. This repository supplies no credentials or live data.

## Known limits

Only CA is observed. Proposals are sampled once per second and during round
countdown; very short-lived proposals can be missed. "Stale" means changed
since the first observation, not since an unknown original proposal time.
Incomplete rounds, missing ratings, large team changes, conflicting dominance
signals and unsupported proposal types cause abstention. Loading mid-match
starts a new history. Feedback is temporary and does not tune the rules.

The rule values are initial heuristics. Synthetic tests verify those rules and
integration behavior with fake interfaces, not their sporting judgment. File
loading on ShinQLX was checked; a full natural match and actual AI guard-response
test remain pending. No general MinQLX compatibility, SDK/model compatibility
matrix, Linux installation reproduction or performance benchmark is claimed.
The observer has no action mode; prompt instructions are not used as an access
control mechanism for administrative commands.
