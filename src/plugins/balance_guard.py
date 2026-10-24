"""Observe CA balancing and ask the real Bob to comment. This plugin cannot move players.

No balance handlers are replaced or called. Ratings are read from balance's
existing cache; all observations and feedback stay in memory until unload.
"""

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
import math
from time import monotonic

import minqlx


VERSION = "0.2.0-observer"


@dataclass(frozen=True)
class Rules:
    enabled: bool = True
    lead: int = 2
    streak: int = 3
    team_rounds: int = 2
    player_rounds: int = 2
    improvement: float = 125.0
    join_tolerance: float = 125.0
    join_rating_gap: float = 100.0
    performance_ratio: float = 1.25
    core_fraction: float = 0.75
    cooldown: float = 20.0
    max_messages: int = 8


CONFIG = {
    "enabled": ("Enabled", "1", 0, 1),
    "lead": ("Lead", "2", 1, 10),
    "streak": ("Streak", "3", 2, 6),
    "team_rounds": ("TeamRounds", "2", 2, 6),
    "player_rounds": ("PlayerRounds", "2", 2, 6),
    "improvement": ("MinImprovement", "125", 0, 1000),
    "join_tolerance": ("JoinTolerance", "125", 0, 1000),
    "join_rating_gap": ("JoinRatingGap", "100", 0, 2000),
    "performance_ratio": ("PerformanceRatio", "1.25", 1.01, 5),
    "core_fraction": ("CoreFraction", "0.75", 0.5, 1),
    "cooldown": ("Cooldown", "20", 0, 300),
    "max_messages": ("MaxMessages", "8", 1, 50),
}


@dataclass(frozen=True)
class PlayerState:
    identifier: int
    team: str
    rating: object = None
    score: object = None
    damage: object = None


@dataclass(frozen=True)
class Snapshot:
    players: tuple
    red_score: int
    blue_score: int
    roundlimit: int

    def members(self, team):
        return frozenset(p.identifier for p in self.players if p.team == team)

    @property
    def roster(self):
        return self.members("red"), self.members("blue")


@dataclass(frozen=True)
class RoundData:
    roster: tuple
    winner: str
    performance: dict


@dataclass(frozen=True)
class Decision:
    reason: str
    team: str = ""
    before: object = None
    after: object = None
    threshold: object = None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def phase_threshold(snapshot):
    rounds = snapshot.red_score + snapshot.blue_score
    return 50 if rounds < 8 else 100 if rounds < 14 else 150


def rating_gap(snapshot):
    averages = []
    for team in ("red", "blue"):
        ratings = [p.rating for p in snapshot.players if p.team == team]
        if not ratings or any(value is None for value in ratings):
            return None
        average = number(sum(value / len(ratings) for value in ratings))
        if average is None:
            return None
        averages.append(average)
    return number(averages[0] - averages[1])


def swapped(snapshot, first, second):
    players = {p.identifier: p for p in snapshot.players}
    if first not in players or second not in players or first == second:
        return None
    a, b = players[first], players[second]
    if {a.team, b.team} != {"red", "blue"}:
        return None
    return replace(
        snapshot,
        players=tuple(
            replace(p, team=b.team if p.identifier == first else a.team) if p.identifier in (first, second) else p
            for p in snapshot.players
        ),
    )


def comparable_rosters(first, second, fraction):
    for previous, current in zip(first, second):
        if not previous or not current:
            return False
        overlap = len(previous & current)
        if overlap / len(previous) < fraction or overlap / len(current) < fraction:
            return False
    return True


def dominant_team(snapshot, history, rules):
    recent = []
    for record in reversed(history):
        if not comparable_rosters(record.roster, snapshot.roster, rules.core_fraction):
            break
        recent.append(record.winner)
    if len(recent) < rules.team_rounds:
        return None
    lead = snapshot.red_score - snapshot.blue_score
    leading = "red" if lead >= rules.lead else "blue" if lead <= -rules.lead else None
    streaking = None
    if len(recent) >= rules.streak and len(set(recent[: rules.streak])) == 1:
        streaking = recent[0]
    if leading and streaking and leading != streaking:
        return None  # Old score lead and recent momentum disagree.
    return leading or streaking


def performance_direction(snapshot, first, second, history, rules, leader):
    # Compare both players over exactly the same recent complete rounds.
    recent = list(history)[-rules.player_rounds :]
    if len(recent) < rules.player_rounds:
        return 0
    if any(first not in r.performance or second not in r.performance for r in recent):
        return 0
    players = {p.identifier: p for p in snapshot.players}
    a, b = players[first], players[second]
    if a.rating is None or b.rating is None or a.rating == b.rating:
        return 0
    scores = {identifier: sum(r.performance[identifier][0] for r in recent) for identifier in (first, second)}
    damage = {identifier: sum(r.performance[identifier][1] for r in recent) for identifier in (first, second)}
    high, low = sorted((a, b), key=lambda p: scores[p.identifier], reverse=True)
    if (
        high.rating >= low.rating
        or scores[high.identifier] <= 0
        or scores[low.identifier] < 0
        or scores[high.identifier] < rules.performance_ratio * scores[low.identifier]
        or damage[high.identifier] <= 0
        or damage[high.identifier] < rules.performance_ratio * damage[low.identifier]
    ):
        return 0
    return -1 if high.team == leader else 1


def evaluate_switch(snapshot, first, second, history, rules, stale=False):
    if stale:
        return Decision("stale")
    alternative = swapped(snapshot, first, second)
    if alternative is None:
        return None
    if snapshot.roundlimit > 1 and snapshot.red_score == snapshot.blue_score == snapshot.roundlimit - 1:
        return Decision("matchpoint")
    leader = dominant_team(snapshot, history, rules)
    before, after = rating_gap(snapshot), rating_gap(alternative)
    if leader is None:
        return None
    direction = performance_direction(snapshot, first, second, history, rules, leader)
    if direction > 0:
        return Decision("performance", leader)
    if direction < 0:
        return None  # Strong match evidence says this particular swap helps the losing team.
    threshold = max(phase_threshold(snapshot), rules.improvement)
    sign = 1 if leader == "red" else -1
    if (
        before is not None
        and after is not None
        and sign * (after - before) > 0
        and abs(before) - abs(after) < threshold
    ):
        return Decision("rating", leader, before, after, threshold)
    return None


def evaluate_join(actual, baseline, first, second, history, rules):
    # Judge the original team cores; new players have no match-performance sample yet.
    leader = dominant_team(baseline, history, rules)
    alternative = swapped(actual, first, second)
    if leader is None or alternative is None:
        return None
    players = {p.identifier: p for p in actual.players}
    a, b = players[first], players[second]
    if a.rating is None or b.rating is None:
        return None
    high, low = sorted((a, b), key=lambda p: p.rating, reverse=True)
    if high.team != leader or high.rating - low.rating < rules.join_rating_gap:
        return None
    before, after = rating_gap(actual), rating_gap(alternative)
    if before is None or after is None:
        return None
    tolerance = max(phase_threshold(actual), rules.join_tolerance)
    if abs(after) - abs(before) > tolerance:
        return None
    return Decision("join", leader, before, after, tolerance)


def comment_facts(decision, snapshot):
    """Only anonymous rule findings cross the Bob bridge, never player records."""
    return {
        "reason": decision.reason,
        "dominant_team": decision.team,
        "score": {"red": snapshot.red_score, "blue": snapshot.blue_score},
        "roundlimit": snapshot.roundlimit,
        "rating_gap_red_minus_blue_before": decision.before,
        "rating_gap_red_minus_blue_alternative": decision.after,
        "rule_threshold": decision.threshold,
        "recommended_action": "review_join_allocation" if decision.reason == "join" else "reconsider_switch",
        "action_performed": False,
    }


class balance_guard(minqlx.Plugin):
    """Read-only observer. There deliberately is no intervention mode."""

    def __init__(self):
        super().__init__()
        for suffix, default, _minimum, _maximum in CONFIG.values():
            self.set_cvar_once("qlx_balanceGuard" + suffix, default)
        self._alive = True
        self._event_number = 0
        self._events = deque(maxlen=20)
        self._last_message = float("-inf")
        self._compatibility = "noch nicht geprueft"
        self._read_rules()
        self._reset()
        self.add_hook("map", self.handle_reset)
        self.add_hook("game_countdown", self.handle_reset)
        self.add_hook("game_end", self.handle_reset)
        self.add_hook("round_start", self.handle_round_start, priority=minqlx.PRI_HIGHEST)
        self.add_hook("round_end", self.handle_round_end, priority=minqlx.PRI_HIGHEST)
        self.add_hook("round_countdown", self.handle_countdown, priority=minqlx.PRI_HIGHEST)
        self.add_hook("team_switch_attempt", self.handle_join_attempt, priority=minqlx.PRI_HIGHEST)
        self.add_hook("team_switch", self.handle_team_switch)
        self.add_hook("player_disconnect", self.handle_disconnect)
        self.add_hook("unload", self.handle_unload)
        self.add_command("guardstatus", self.cmd_status, 3)
        self.add_command("guardfeedback", self.cmd_feedback, 3, usage="<Kommentar-ID> gut|falsch")
        self.add_command("guardreload", self.cmd_reload, 3)
        self._schedule_poll()

    def _read_rules(self):
        defaults = Rules()
        values = {}
        for field, (suffix, _default, minimum, maximum) in CONFIG.items():
            raw = number(self.get_cvar("qlx_balanceGuard" + suffix))
            if (
                raw is None
                or not minimum <= raw <= maximum
                or isinstance(getattr(defaults, field), (bool, int))
                and not raw.is_integer()
            ):
                values[field] = getattr(defaults, field)
            elif field == "enabled":
                values[field] = bool(raw)
            elif isinstance(getattr(defaults, field), int):
                values[field] = int(raw)
            else:
                values[field] = raw
        self.rules = Rules(**values)

    def _reset(self):
        self._epoch = getattr(self, "_epoch", 0) + 1
        self._history = deque(maxlen=6)
        self._round_start = None
        self._dirty = set()
        self._first_join = None
        self._join_inflight = None
        self._proposal = None
        self._proposal_roster = None
        self._roster_generation = 0
        self._proposal_generation = 0
        self._proposal_reported = False
        self._messages = 0
        self._requests = 0
        self._suppressed = 0
        self._pending_comment = None
        self._comment_deadline = 0
        self._bob_status = "noch keine Anfrage"
        self._balance_instance = None

    def _running(self):
        return (
            self._alive
            and self.rules.enabled
            and self.plugins.get("balance_guard") is self
            and self.game
            and self.game.state == "in_progress"
            and self.game.type_short == "ca"
        )

    def _dependency(self):
        plugin = self.plugins.get("balance")
        if plugin is None:
            self._compatibility = "balance fehlt"
            return None
        if (
            not isinstance(getattr(plugin, "ratings", None), Mapping)
            or not hasattr(plugin, "switch_suggestion")
            or not callable(getattr(getattr(plugin, "ratings_lock", None), "acquire", None))
            or not callable(getattr(plugin.ratings_lock, "release", None))
        ):
            self._compatibility = "balance-Schnittstelle unbekannt"
            return None
        if self._balance_instance is not None and self._balance_instance is not plugin:
            self._reset()  # A dependency reload invalidates old suggestions and join tickets.
        self._balance_instance = plugin
        self._compatibility = "balance lesbar"
        return plugin

    def _snapshot(self, with_stats=False):
        if not self._running():
            return None
        balance = self._dependency()
        if balance is None:
            return None
        lock = balance.ratings_lock
        try:
            acquired = lock.acquire(False)
        except (AttributeError, TypeError):
            self._compatibility = "Rating-Sperre unbekannt; keine Bewertung"
            return None
        if not acquired:
            return None
        try:
            teams = self.teams()
            players = []
            for team in ("red", "blue"):
                for player in teams[team]:
                    identifier = player.steam_id
                    entry = balance.ratings.get(identifier, {}).get("ca", {})
                    rating = number(entry.get("elo"))
                    if rating is not None and rating <= 0:
                        rating = None
                    score = damage = None
                    if with_stats:
                        stats = player.stats
                        score, damage = number(stats.score), number(stats.damage_dealt)
                    players.append(PlayerState(identifier, team, rating, score, damage))
            game = self.game
            return Snapshot(tuple(players), int(game.red_score), int(game.blue_score), int(game.roundlimit))
        except (AttributeError, KeyError, TypeError, ValueError, minqlx.NonexistentPlayerError):
            self._compatibility = "Momentaufnahme unvollstaendig"
            return None
        finally:
            lock.release()

    def _schedule_poll(self):
        minqlx.delay(1)(self._poll)()

    def _poll(self):
        if not self._alive or self.plugins.get("balance_guard") is not self:
            return
        try:
            self._observe_suggestion()
        finally:
            self._schedule_poll()

    def _observe_suggestion(self):
        snapshot = self._snapshot()
        if snapshot is None:
            return
        suggestion = self._balance_instance.switch_suggestion
        if suggestion is None:
            self._proposal = None
            return
        try:
            first = suggestion.red_player.steam_id
            second = suggestion.blue_player.steam_id
        except (AttributeError, minqlx.NonexistentPlayerError):
            self._compatibility = "Vorschlagsformat unbekannt; keine Bewertung"
            return
        if suggestion is not self._proposal:
            if swapped(snapshot, first, second) is None:
                return
            self._proposal = suggestion
            self._proposal_roster = snapshot.roster
            self._proposal_generation = self._roster_generation
            self._proposal_reported = False
        if self._proposal_reported:
            return
        decision = evaluate_switch(
            snapshot,
            first,
            second,
            self._history,
            self.rules,
            stale=(snapshot.roster != self._proposal_roster or self._proposal_generation != self._roster_generation),
        )
        if decision is not None:
            self._proposal_reported = True
            alternative = swapped(snapshot, first, second)
            self._announce(decision, snapshot, alternative.roster if alternative is not None else None)

    def _announce(self, decision, snapshot, alternative_roster=None):
        now = monotonic()
        if not self._running():
            return
        if self._pending_comment is not None and now > self._comment_deadline:
            self._pending_comment = None
            self._bob_status = "Antwort abgelaufen"
        if (
            self._pending_comment is not None
            or self._requests >= self.rules.max_messages
            or now - self._last_message < self.rules.cooldown
        ):
            self._suppressed += 1
            return
        bot = self.plugins.get("openai_bot")
        bridge = getattr(bot, "comment_balance_guard", None)
        if not callable(bridge):
            self._suppressed += 1
            self._bob_status = "Bob fehlt oder Schnittstelle unbekannt"
            return
        token = object()
        epoch = self._epoch
        self._pending_comment = token
        self._comment_deadline = now + 12

        def is_current():
            if (
                self._pending_comment is not token
                or self._epoch != epoch
                or monotonic() > self._comment_deadline
                or not self._running()
            ):
                return False
            current = self._snapshot()
            return (
                current is not None
                and self._epoch == epoch
                and current.red_score == snapshot.red_score
                and current.blue_score == snapshot.blue_score
                and current.roster in (snapshot.roster, alternative_roster)
            )

        def finished(sent):
            if self._pending_comment is not token:
                return
            self._pending_comment = None
            if not sent:
                self._suppressed += 1
                self._bob_status = "keine Ausgabe (beschaeftigt, Fehler oder veraltet)"
                return
            self._event_number += 1
            self._events.append({"number": self._event_number, "reason": decision.reason, "feedback": None})
            self._messages += 1
            self._last_message = monotonic()
            self._bob_status = "Kommentar ausgegeben"

        try:
            accepted = bridge(comment_facts(decision, snapshot), is_current, finished)
        except Exception:
            accepted = False  # Never expose another plugin's exception text.
        if accepted:
            self._requests += 1
            self._last_message = now
            self._bob_status = "Antwort angefragt"
        else:
            finished(False)

    def handle_reset(self, *args):
        self._reset()

    def handle_unload(self, plugin):
        if plugin == "balance_guard":
            self._alive = False
            self._reset()

    def handle_round_start(self, *args):
        self._first_join = self._join_inflight = None
        self._dirty = set()
        self._round_start = self._snapshot(with_stats=True)

    def handle_team_switch(self, player, old, new):
        if old != new and (old in ("red", "blue") or new in ("red", "blue")):
            self._roster_generation += 1
        if self._round_start is not None and old != new:
            self._dirty.add(player.steam_id)

    def handle_disconnect(self, player, *args):
        if self._proposal_roster and any(player.steam_id in team for team in self._proposal_roster):
            self._roster_generation += 1
        if self._round_start is not None:
            self._dirty.add(player.steam_id)
        if self._first_join and self._first_join[0] == player.steam_id:
            self._first_join = None

    def handle_round_end(self, data):
        epoch = self._epoch
        start, self._round_start = self._round_start, None
        if not isinstance(data, Mapping):
            return
        end = self._snapshot(with_stats=True)
        winner = str(data.get("TEAM_WON", "")).lower()
        if start is None or end is None or epoch != self._epoch or winner not in ("red", "blue"):
            return
        current = {p.identifier: p for p in end.players}
        performance = {}
        for previous in start.players:
            player = current.get(previous.identifier)
            if player is None or player.team != previous.team or previous.identifier in self._dirty:
                continue
            if any(v is None for v in (previous.score, previous.damage, player.score, player.damage)):
                continue
            delta = player.score - previous.score, player.damage - previous.damage
            if delta[1] >= 0:
                performance[player.identifier] = delta
        self._history.append(RoundData(start.roster, winner, performance))

    def handle_countdown(self, *args):
        self._observe_suggestion()

    def handle_join_attempt(self, player, old, new):
        # Returning None always leaves every original join handler in control.
        if old not in ("spectator", "free") or new not in ("red", "blue", "any"):
            return
        if self._join_inflight is not None:
            return
        auto = self.plugins.get("auto_rebalance")
        if auto is None or not hasattr(auto, "last_new_player_id"):
            return
        before = self._snapshot()
        if before is None:
            return
        if len(before.members("red")) == len(before.members("blue")):
            self._first_join = (player.steam_id, before, auto)
            return
        pending, self._first_join = self._first_join, None
        if pending is None:
            return
        first, baseline, expected_auto = pending
        if first == player.steam_id or auto is not expected_auto or auto.last_new_player_id != first:
            return
        ticket = (self._epoch, first, player.steam_id, baseline, auto)
        self._join_inflight = ticket
        minqlx.delay(0.2)(self._finish_join)(ticket)

    def _finish_join(self, ticket):
        if ticket is not self._join_inflight:
            return
        self._join_inflight = None
        epoch, first, second, baseline, auto = ticket
        if epoch != self._epoch or self.plugins.get("auto_rebalance") is not auto:
            return
        actual = self._snapshot()
        if actual is None or epoch != self._epoch:
            return
        original = set().union(*baseline.roster)
        if set().union(*actual.roster) != original | {first, second} or len(actual.members("red")) != len(
            actual.members("blue")
        ):
            return
        for previous, current in zip(baseline.roster, actual.roster):
            if not previous <= current:
                return  # Someone else moved during the join; the cause is ambiguous.
        decision = evaluate_join(actual, baseline, first, second, self._history, self.rules)
        if decision is not None:
            self._announce(decision, actual)

    def cmd_status(self, player, msg, channel):
        good = sum(e["feedback"] == "gut" for e in self._events)
        bad = sum(e["feedback"] == "falsch" for e in self._events)
        latest = f"{self._events[-1]['number']} ({self._events[-1]['reason']})" if self._events else "keine"
        channel.reply(
            f"Balance-Guard {VERSION}: nur Beobachtung; aktiv={self.rules.enabled}; {self._compatibility}; "
            f"Runden={len(self._history)}; Kommentare={self._messages}; "
            f"Anfragen={self._requests}/{self.rules.max_messages}; "
            f"Bob={self._bob_status}; letzte Kommentar-ID={latest}; "
            f"unterdrueckt={self._suppressed}; Feedback gut/falsch={good}/{bad}."
        )

    def cmd_feedback(self, player, msg, channel):
        if (
            len(msg) != 3
            or not msg[1].isascii()
            or not msg[1].isdigit()
            or len(msg[1]) > 8
            or msg[2].lower() not in ("gut", "falsch")
        ):
            return minqlx.RET_USAGE
        event = next((e for e in self._events if e["number"] == int(msg[1])), None)
        if event is None:
            channel.reply("Diese Kommentar-ID ist nicht mehr im Speicher.")
            return
        event["feedback"] = msg[2].lower()
        channel.reply(f"Guard-Kommentar #{event['number']}: als {event['feedback']} markiert.")

    def cmd_reload(self, player, msg, channel):
        self._read_rules()
        self._reset()
        channel.reply("Balance-Guard: eigene Regeln neu gelesen, Beobachtungsdaten zurueckgesetzt. Nur Beobachtung.")
