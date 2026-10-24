# Quake Live plugins: Balance Guard and Bob

An experimental **observer for Clan Arena balancing**, with short comments from
Bob, an OpenAI-powered spectator bot. It highlights questionable proposed swaps
and allocations of new players. It does **not** cancel proposals, execute admin
commands, move players, or offer an intervention mode.

This collection contains the current observer implementation and the matching
Bob bridge. It is intended for server operators who can review their existing
balancing setup. Loading has been checked on ShinQLX 0.7.0; a complete natural
match test of the rules and AI comments is still pending. It is not a generally
validated replacement for a server's existing balancing system.

## Files

| Path | Purpose |
| --- | --- |
| `src/plugins/balance_guard.py` | Observer, version `0.2.0-observer` |
| `src/plugins/openai_bot.py` | Bob variant with bounded history reads and the guard-comment bridge |
| `src/plugins/openai_bot_role_balance_guard.txt` | Additional event-specific prompt; uses Bob's existing personality |
| `examples/prompts/` | New, neutral starter prompts, not production personality files |
| `compat/` | Optional reference versions of the expected balancer and join balancer |
| `tests/` | Synthetic offline tests for the observer, bridge and history logic |
| `docs/INTEGRATION.md` | Required balance interface, data handling and limits |
| `NOTICE.md`, `licenses/` | Attribution and component-specific licenses |

## Prerequisites

- An existing QLDS with a working MinQLX-compatible Python interface. The checked
  deployment uses **ShinQLX 0.7.0 and Python 3.13**; other framework/Python versions
  have not been verified. Local synthetic tests use Python 3.12.
- Redis and the framework's Redis integration for Bob and the existing balancer.
- A loaded plugin named **`balance`** exposing the interface documented in
  [INTEGRATION.md](docs/INTEGRATION.md). An arbitrary plugin with that name is not
  sufficient. Unsupported interfaces are skipped rather than guessed.
- A loaded plugin named **`openai_bot`** exposing `comment_balance_guard` as in
  the included variant, its Python dependencies (`openai`, `redis`, `tiktoken`,
  `emoji`), and the operator's own API configuration.
- Optional: **`auto_rebalance`** with `last_new_player_id` for observing join pairs.
  Proposal observations do not require this optional plugin.

The SDK must provide `AsyncOpenAI` and accept the completion parameters used by
the included code, including `max_completion_tokens`. No dependency lockfile or
fresh-install compatibility matrix is provided. Resolve packages in the QLDS
Python environment rather than installing them into an unrelated system Python.

## Installation

1. Back up the affected files and configuration with their permissions. Compare
   any existing Bob/balance variants before replacing anything. Keep a rollback
   copy: these files may differ from other versions with the same names.
2. Check your existing balancer against [the required interface](docs/INTEGRATION.md).
   `compat/balance.py` and `compat/auto_rebalance.py` are supplied as reference
   variants for an explicitly reviewed setup, not automatic replacements.
   The reference `balance.py` additionally imports `requests`, `aiohttp` and
   `aiohttp_retry`, and may contact rating services according to its configuration.
3. Copy the three files in `src/plugins/` into your configured `qlx_pluginsPath`.
   Keep `openai_bot_role_balance_guard.txt` beside `openai_bot.py`.
4. Keep your existing `openai_bot_system_context.txt` and
   `openai_bot_role_chat.txt`. If starting from scratch, copy the two neutral
   files from `examples/prompts/` to the same plugin directory and adapt them.
   Do not overwrite an existing personality unintentionally. The guard requires
   a nonempty chat-role file and a nonempty guard-role file; otherwise it stays
   silent. Other optional event-role files are not provided here.
5. Configure Bob's **`qlx_openai_apikey`** and **`qlx_openai_model`** through your
   existing protected server configuration. Use an API model compatible with
   the parameters in this Bob variant. Do not paste a key into source code, a
   public configuration example, a chat command, or a Git commit. API requests
   use the operator's account and can incur charges.
6. Load your compatible `balance`, then optional `auto_rebalance`, then Bob and
   the guard. With an existing administrator plugin loader supporting `!load`:

   ```text
   !load openai_bot
   !load balance_guard
   !guardstatus
   ```

   If Bob is already loaded, use your existing targeted reload mechanism after
   updating it. The prefix and permissions depend on your plugin manager.
   A QLDS restart is not required for a supported live plugin load.
7. For future starts, append `balance_guard` **after its dependencies** in your
   existing `qlx_plugins` value. Preserve all existing entries and ordering
   requirements; do not replace your whole list with an example from this README.
8. Observe a normal CA match. Load before the match when possible; loading
   mid-match cannot reconstruct earlier rounds. Dominance/performance rules
   need at least two suitable newly observed complete rounds by default.

Immediate rollback of the observer is `!unload balance_guard` through your
existing loader. Remove its autoload entry as well if you want it to stay
disabled on subsequent starts. An unloaded guard discards pending comments;
there are no player moves or database changes by the guard to undo. Restoring a
previous Bob variant is a separate operation using your own backup.

## What it observes

- A swap would strengthen an already dominant team while improving the absolute
  average-rating gap too little.
- Two players' performance over the same complete rounds contradicts their
  rating order, and the proposed swap would help the dominant team.
- Both teams are at match point and another proposed swap deserves a second look.
- A proposal's roster changed after the guard first observed it.
- The dominant team received the higher-rated of two newcomers although swapping
  just those newcomers would be within the configured rating-gap tolerance.

Dominance requires comparable team cores and either a score lead or a winning
streak; conflicting signals cause the guard to abstain. Performance compares
both score and damage deltas over complete rounds, excluding partial participation.
These are starting heuristics, not proof that a proposed swap is wrong.

Bob receives an anonymous rule finding and uses the existing system/chat persona
plus the extra guard prompt. Comments are generated, not selected from fixed
phrases. He may question the decision, but must not claim to have cancelled it.
The original balancer can execute a swap while an AI response is in flight.

## Administration

The observer's commands require permission level **3**:

| Command | Purpose |
| --- | --- |
| `!guardstatus` | Version, compatibility status, observation counts, Bob status and latest internal comment ID |
| `!guardfeedback 1 gut` | Mark emitted comment 1 as useful |
| `!guardfeedback 1 falsch` | Mark emitted comment 1 as unsuitable |
| `!guardreload` | Reread the guard's cvars and reset its observations; does not reload other plugins |

Command names and the `gut`/`falsch` feedback tokens are literal. Current admin
status messages are in German. Feedback covers the last 20 delivered comments,
stays in memory, and does not automatically train or tune anything.

## Configuration

All entries have the prefix **`qlx_balanceGuard`**. Apply changes through your
existing server configuration and use `!guardreload` to reread current values.

| Suffix | Default | Meaning |
| --- | ---: | --- |
| `Enabled` | `1` | Enable observation |
| `Lead` | `2` | Score lead used as a dominance signal |
| `Streak` | `3` | Consecutive round wins used as a dominance signal |
| `TeamRounds` | `2` | Minimum comparable complete rounds |
| `PlayerRounds` | `2` | Common complete rounds for performance comparison |
| `MinImprovement` | `125` | Minimum useful reduction in the absolute team-rating gap for a questionable proposal |
| `JoinTolerance` | `125` | Allowed extra absolute rating gap for the alternative newcomer allocation |
| `JoinRatingGap` | `100` | Minimum rating difference between the two newcomers |
| `PerformanceRatio` | `1.25` | Required advantage in both score and damage |
| `CoreFraction` | `0.75` | Required overlap of past/current team rosters |
| `Cooldown` | `20` | Minimum seconds between accepted requests/comments |
| `MaxMessages` | `8` | Maximum accepted AI requests per match/observation phase |

The guard's own phase floor is 50/100/150 after 0-7/8-13/14+ completed rounds;
it uses the maximum of that floor and `MinImprovement` or `JoinTolerance` for
the respective rule. These values do not change the balancer's own thresholds.
Invalid cvars fall back to defaults. Rounds/streak are bounded by the six-round
history. Cooldown, request budget and missing/busy dependencies can suppress
comments; an observation is not a promise of a chat message.

## Offline tests

From a checkout with Python 3.12:

```sh
python -B -m unittest discover -s tests -q
```

**74 tests pass**: 47 observer tests, 11 Bob-bridge tests and 16 history tests.
They extract selected Python definitions with synthetic framework, Redis and
SDK objects. They do not import the production plugins or require an API key,
native game engine, Redis server or network connection. They do not test the
optional reference balancers as complete runtime plugins or prove live match
correctness. See [INTEGRATION.md](docs/INTEGRATION.md) for remaining limits.

## License and attribution

The new observer, prompt, tests and documentation use BSD-3-Clause. Bob and
`auto_rebalance` retain their upstream notices and BSD-3-Clause terms.
**`compat/balance.py` is separately GPL-3.0-or-later** as declared in that file;
the BSD license at the root does not replace its license. Full texts and
provenance are in [NOTICE.md](NOTICE.md) and [licenses/](licenses/).
