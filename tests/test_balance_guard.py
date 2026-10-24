"""Synthetic rule and lifecycle tests. No native plugin, Redis, API or server access."""

import ast
from dataclasses import replace
from pathlib import Path
import random
import sys
from types import ModuleType, SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src/plugins/balance_guard.py"


class FakeLock:
    def __init__(self):
        self.busy = False
        self.acquisitions = []
        self.released = 0

    def acquire(self, blocking):
        self.acquisitions.append(blocking)
        return not self.busy

    def release(self):
        self.released += 1


class FakePlayer:
    def __init__(self, identifier, team, score=0, damage=0):
        self.steam_id = identifier
        self.team = team
        self.stats = SimpleNamespace(score=score, damage_dealt=damage)

    def put(self, *args):
        raise AssertionError("The observer must never move a player")


class FakePlugin:
    engine = None

    def __init__(self):
        self.engine = FakePlugin.engine

    @property
    def plugins(self):
        return self.engine.registry.copy()

    @property
    def game(self):
        return self.engine.game

    def teams(self):
        return {team: [p for p in self.engine.players if p.team == team] for team in ("red", "blue", "spectator")}

    def set_cvar_once(self, key, value):
        self.engine.cvars.setdefault(key, value)

    def get_cvar(self, key):
        return self.engine.cvars.get(key)

    def add_hook(self, name, handler, **kwargs):
        self.engine.hooks.append((name, handler, kwargs))

    def add_command(self, name, handler, permission, **kwargs):
        self.engine.commands[name] = (handler, permission)

    def msg(self, text):
        self.engine.messages.append(text)


def forbidden(*args, **kwargs):
    raise AssertionError("An observer attempted an action or external integration")


def load_isolated():
    module = ModuleType("balance_guard_synthetic_tests")
    module.minqlx = SimpleNamespace(
        Plugin=FakePlugin,
        PRI_HIGHEST=0,
        RET_USAGE="usage",
        NonexistentPlayerError=type("NonexistentPlayerError", (Exception,), {}),
        console_command=forbidden,
        callvote=forbidden,
    )

    def delay(seconds):
        def decorate(function):
            def schedule(*args, **kwargs):
                FakePlugin.engine.scheduled.append((seconds, function, args, kwargs))

            return schedule

        return decorate

    module.minqlx.delay = delay
    tree = ast.parse(SOURCE.read_bytes(), filename=str(SOURCE))
    tree.body = [
        n for n in tree.body if not (isinstance(n, ast.Import) and any(alias.name == "minqlx" for alias in n.names))
    ]
    sys.modules[module.__name__] = module
    exec(compile(tree, str(SOURCE), "exec"), module.__dict__)
    return module


logic = load_isolated()


def snapshot(red_rating=1150, blue_rating=1000, red_score=0, blue_score=5, size=4):
    players = tuple(logic.PlayerState(100 + i, "red", red_rating) for i in range(1, size + 1))
    players += tuple(logic.PlayerState(200 + i, "blue", blue_rating) for i in range(1, size + 1))
    return logic.Snapshot(players, red_score, blue_score, 8)


def history(state, count=2, winner="blue", performance=None):
    return [logic.RoundData(state.roster, winner, dict(performance or {})) for _ in range(count)]


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = logic.Rules()
        self.state = snapshot()
        self.history = history(self.state)

    def decide(self, state=None, records=None, **kwargs):
        return logic.evaluate_switch(
            state or self.state, 101, 201, self.history if records is None else records, self.rules, **kwargs
        )

    def test_phase_boundaries_match_existing_local_balance(self):
        for rounds, threshold in [(0, 50), (7, 50), (8, 100), (13, 100), (14, 150)]:
            with self.subTest(rounds=rounds):
                self.assertEqual(logic.phase_threshold(replace(self.state, red_score=rounds, blue_score=0)), threshold)

    def test_small_rating_improvement_buffing_dominant_team_is_flagged(self):
        result = self.decide()
        self.assertEqual(result.reason, "rating")
        self.assertEqual(result.team, "blue")
        self.assertEqual(abs(result.before) - abs(result.after), 75)
        self.assertEqual(result.threshold, 125)

    def test_existing_late_threshold_is_never_lowered(self):
        state = replace(snapshot(1130, 1000, size=2), red_score=6, blue_score=8, roundlimit=12)
        result = self.decide(state, history(state))
        self.assertEqual(result.threshold, 150)

    def test_exact_threshold_is_allowed(self):
        state = snapshot(1125, 1000, size=2)
        self.assertIsNone(self.decide(state, history(state)))

    def test_swap_helping_losing_team_is_not_flagged(self):
        state = snapshot(1000, 1150)
        self.assertIsNone(self.decide(state, history(state)))

    def test_missing_ratings_do_not_get_invented_defaults(self):
        state = replace(self.state, players=(replace(self.state.players[0], rating=None),) + self.state.players[1:])
        self.assertIsNone(self.decide(state))

    def test_two_complete_rounds_required_for_dominance(self):
        self.assertIsNone(self.decide(records=self.history[:1]))

    def test_conflicting_score_lead_and_recent_streak_abstain(self):
        self.assertIsNone(self.decide(records=history(self.state, 3, "red")))

    def test_winning_streak_can_supply_evidence_without_two_round_lead(self):
        state = replace(self.state, red_score=4, blue_score=5)
        self.assertIsNotNone(self.decide(state, history(state, 3)))

    def test_old_roster_evidence_is_not_used_for_a_different_team(self):
        other = replace(
            self.state, players=tuple(replace(p, identifier=p.identifier + 1000) for p in self.state.players)
        )
        self.assertIsNone(self.decide(records=history(other)))

    def test_equal_matchpoint_is_flagged_without_rating_or_performance_history(self):
        state = replace(self.state, red_score=7, blue_score=7)
        self.assertEqual(self.decide(state, []).reason, "matchpoint")

    def test_matchpoint_adapts_to_roundlimit_and_does_not_apply_to_unequal_score(self):
        state = replace(self.state, red_score=9, blue_score=9, roundlimit=10)
        self.assertEqual(self.decide(state, []).reason, "matchpoint")
        self.assertIsNone(self.decide(replace(state, blue_score=8), []))

    def test_stale_proposal_is_flagged_even_if_players_left(self):
        self.assertEqual(self.decide(replace(self.state, players=()), stale=True).reason, "stale")

    def test_performance_inversion_can_flag_an_otherwise_plausible_swap(self):
        state = snapshot(1100, 1200)
        records = history(state, performance={101: (10, 1000), 201: (2, 200)})
        self.assertEqual(self.decide(state, records).reason, "performance")

    def test_reliable_performance_showing_a_helpful_swap_overrides_rating_warning(self):
        records = history(self.state, performance={101: (2, 200), 201: (10, 1000)})
        self.assertIsNone(self.decide(records=records))

    def test_performance_requires_common_recent_full_rounds_and_damage_confirmation(self):
        state = snapshot(1100, 1200)
        for records in [
            history(state, 1, performance={101: (10, 1000), 201: (2, 200)}),
            history(state, performance={101: (10, 1000)}),
            history(state, performance={101: (10, 100), 201: (2, 200)}),
            history(state, performance={101: (2.1, 210), 201: (2, 200)}),
        ]:
            with self.subTest(records=records):
                self.assertIsNone(self.decide(state, records))

    def test_join_pair_can_be_recommended_to_support_the_losing_core(self):
        base = snapshot(1100, 1000)
        actual = replace(
            base, players=base.players + (logic.PlayerState(301, "blue", 1300), logic.PlayerState(302, "red", 1000))
        )
        result = logic.evaluate_join(actual, base, 301, 302, history(base), self.rules)
        self.assertEqual(result.reason, "join")
        self.assertEqual(abs(result.after) - abs(result.before), 120)

    def test_join_abstains_on_missing_ratings_large_cost_and_tiny_rating_gap(self):
        base = snapshot(1100, 1000)
        for high, low in [(None, 1000), (2000, 500), (1050, 1000)]:
            with self.subTest(high=high, low=low):
                actual = replace(
                    base,
                    players=base.players + (logic.PlayerState(301, "blue", high), logic.PlayerState(302, "red", low)),
                )
                self.assertIsNone(logic.evaluate_join(actual, base, 301, 302, history(base), self.rules))

    def test_bob_receives_only_anonymous_facts_and_no_claim_of_intervention(self):
        for decision in [
            logic.Decision("stale"),
            logic.Decision("matchpoint"),
            logic.Decision("rating", "blue", 100, 25, 125),
            logic.Decision("performance", "blue"),
            logic.Decision("join", "blue"),
        ]:
            facts = logic.comment_facts(decision, self.state)
            self.assertIs(facts["action_performed"], False)
            self.assertEqual(facts["reason"], decision.reason)
            self.assertEqual(facts["score"], {"red": 0, "blue": 5})
            self.assertFalse(set(facts) & {"players", "names", "identifiers", "history"})

    def test_rules_are_color_symmetric_across_synthetic_matches(self):
        rng = random.Random(20261024)
        opposite = {"red": "blue", "blue": "red", "": ""}
        for case in range(250):
            state = replace(
                self.state,
                red_score=rng.randrange(8),
                blue_score=rng.randrange(8),
                players=tuple(replace(p, rating=4 * rng.randrange(200, 501)) for p in self.state.players),
            )
            performance = {p.identifier: (rng.randrange(20), rng.randrange(100, 1000)) for p in state.players}
            winner = rng.choice(("red", "blue"))
            records = history(state, 3, winner, performance)
            inverted = replace(
                state,
                red_score=state.blue_score,
                blue_score=state.red_score,
                players=tuple(replace(p, team=opposite[p.team]) for p in state.players),
            )
            inverted_records = [
                logic.RoundData((r.roster[1], r.roster[0]), opposite[r.winner], r.performance) for r in records
            ]
            result = self.decide(state, records)
            other = self.decide(inverted, inverted_records)
            with self.subTest(case=case):
                self.assertEqual(result is None, other is None)
                if result is not None:
                    self.assertEqual(result.reason, other.reason)
                    self.assertEqual(opposite[result.team], other.team)


class Engine:
    def __init__(self, cvars=None):
        self.cvars = dict(cvars or {})
        self.registry = {}
        self.game = SimpleNamespace(state="in_progress", type_short="ca", red_score=0, blue_score=5, roundlimit=8)
        self.players = [FakePlayer(p.identifier, p.team) for p in snapshot().players]
        self.messages, self.scheduled, self.hooks = [], [], []
        self.commands = {}
        self.balance = SimpleNamespace(
            ratings={p.identifier: {"ca": {"elo": p.rating}} for p in snapshot().players},
            ratings_lock=FakeLock(),
            switch_suggestion=None,
            cmd_dont=forbidden,
            execute_suggestion=forbidden,
        )
        self.auto = SimpleNamespace(last_new_player_id=None)
        self.bob_requests = []
        self.bob_busy = False
        self.registry.update(
            balance=self.balance,
            auto_rebalance=self.auto,
            openai_bot=SimpleNamespace(bot_name="Bob", comment_balance_guard=self.ask_bob),
        )
        self.now = 1000
        FakePlugin.engine = self
        logic.monotonic = lambda: self.now
        self.guard = logic.balance_guard()
        self.registry["balance_guard"] = self.guard

    def ask_bob(self, facts, is_current, finished):
        if self.bob_busy:
            return False
        self.bob_requests.append((facts, is_current, finished))
        return True

    def deliver_bob(self, success=True):
        facts, is_current, finished = self.bob_requests.pop(0)
        sent = success and is_current()
        if sent:
            self.messages.append("Bob: Oh nee, dieser Switch geht gar nicht.")
        finished(sent)
        return facts

    def player(self, identifier):
        return next(p for p in self.players if p.steam_id == identifier)

    def seed_history(self):
        state = self.guard._snapshot()
        self.guard._history.extend(history(state))

    def suggest(self):
        self.balance.switch_suggestion = SimpleNamespace(red_player=self.player(101), blue_player=self.player(201))

    def run_delay(self, seconds):
        index = next(i for i, entry in enumerate(self.scheduled) if entry[0] == seconds)
        _delay, function, args, kwargs = self.scheduled.pop(index)
        function(*args, **kwargs)

    def join_pair(self):
        first, second = FakePlayer(301, "spectator"), FakePlayer(302, "spectator")
        self.players += [first, second]
        self.balance.ratings.update({301: {"ca": {"elo": 1300}}, 302: {"ca": {"elo": 1000}}})
        self.guard.handle_join_attempt(first, "spectator", "blue")
        first.team = "blue"
        self.auto.last_new_player_id = 301
        self.guard.handle_join_attempt(second, "spectator", "red")
        second.team = "red"
        return first, second


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.guard = self.engine.guard

    def test_proposal_requests_real_bob_without_mutating_suggestion_or_players(self):
        self.engine.seed_history()
        self.engine.suggest()
        suggestion = self.engine.balance.switch_suggestion
        before = [(p.steam_id, p.team) for p in self.engine.players]
        self.guard._observe_suggestion()
        self.assertEqual(self.engine.messages, [])
        self.assertEqual(self.engine.bob_requests[0][0]["reason"], "rating")
        self.engine.deliver_bob()
        self.assertEqual(len(self.engine.messages), 1)
        self.assertNotIn("TEST", self.engine.messages[0])
        self.assertIs(self.engine.balance.switch_suggestion, suggestion)
        self.assertEqual(before, [(p.steam_id, p.team) for p in self.engine.players])
        self.assertTrue(all(value is False for value in self.engine.balance.ratings_lock.acquisitions))

    def test_poll_and_countdown_do_not_duplicate_a_comment(self):
        self.engine.seed_history()
        self.engine.suggest()
        for _ in range(5):
            self.guard._observe_suggestion()
            self.guard.handle_countdown()
        self.assertEqual(len(self.engine.bob_requests), 1)
        self.engine.deliver_bob()
        self.assertEqual(len(self.engine.messages), 1)

    def test_roster_change_after_first_observation_is_reported_as_stale(self):
        self.engine.suggest()
        self.guard._observe_suggestion()
        self.engine.player(104).team = "spectator"
        self.guard.handle_countdown()
        self.assertEqual(self.engine.deliver_bob()["reason"], "stale")

    def test_player_leaving_and_returning_between_polls_still_invalidates_proposal(self):
        self.engine.suggest()
        self.guard._observe_suggestion()
        player = self.engine.player(104)
        self.guard.handle_team_switch(player, "red", "spectator")
        self.guard.handle_team_switch(player, "spectator", "red")
        self.guard._observe_suggestion()
        self.assertEqual(self.engine.deliver_bob()["reason"], "stale")

    def test_two_real_synthetic_rounds_feed_a_later_recommendation(self):
        self.engine.game.blue_score = 0
        self.guard.handle_reset()
        for round_number in (1, 2):
            self.guard.handle_round_start(round_number)
            for player in self.engine.players:
                player.stats.score += 5
                player.stats.damage_dealt += 500
            self.engine.game.blue_score = round_number
            self.guard.handle_round_end({"TEAM_WON": "BLUE"})
        self.engine.suggest()
        self.guard.handle_countdown()
        facts = self.engine.deliver_bob()
        self.assertEqual(len(self.engine.messages), 1)
        self.assertEqual(facts["score"], {"red": 0, "blue": 2})
        self.assertEqual(facts["rule_threshold"], 125)

    def test_unknown_round_payload_and_lock_signature_do_not_raise(self):
        self.guard.handle_round_start(6)
        self.guard.handle_round_end(None)
        self.assertEqual(len(self.guard._history), 0)
        self.engine.balance.ratings_lock = SimpleNamespace(acquire=lambda: True, release=forbidden)
        self.assertIsNone(self.guard._snapshot())

    def test_missing_or_changed_adapter_and_busy_lock_abstain(self):
        self.engine.balance.ratings_lock.busy = True
        self.assertIsNone(self.guard._snapshot())
        self.engine.registry["balance"] = SimpleNamespace(ratings={})
        self.guard._observe_suggestion()
        self.assertIn("unbekannt", self.guard._compatibility)
        del self.engine.registry["balance"]
        self.guard._observe_suggestion()
        self.assertEqual(self.engine.messages, [])

    def test_unknown_composite_suggestion_is_skipped(self):
        self.engine.balance.switch_suggestion = SimpleNamespace(suggestions=[])
        self.guard._observe_suggestion()
        self.assertIn("unbekannt", self.guard._compatibility)
        self.assertEqual(self.engine.messages, [])

    def test_dependency_reload_discards_old_rounds(self):
        self.engine.seed_history()
        self.engine.registry["balance"] = SimpleNamespace(
            ratings=self.engine.balance.ratings.copy(), ratings_lock=FakeLock(), switch_suggestion=None
        )
        self.guard._snapshot()
        self.assertEqual(len(self.guard._history), 0)

    def test_complete_round_deltas_exclude_partial_round_players(self):
        self.guard.handle_round_start(6)
        for player in self.engine.players:
            player.stats.score += 10
            player.stats.damage_dealt += 1000
        changed = self.engine.player(101)
        self.guard.handle_team_switch(changed, "red", "spectator")
        self.guard.handle_team_switch(changed, "spectator", "red")
        self.guard.handle_round_end({"TEAM_WON": "BLUE"})
        record = self.guard._history[-1]
        self.assertNotIn(101, record.performance)
        self.assertEqual(record.performance[201], (10, 1000))

    def test_join_observation_leaves_actual_allocation_untouched(self):
        for value in self.engine.balance.ratings.values():
            if value["ca"]["elo"] == 1150:
                value["ca"]["elo"] = 1100
        self.engine.seed_history()
        first, second = self.engine.join_pair()
        self.engine.run_delay(0.2)
        self.assertEqual((first.team, second.team), ("blue", "red"))
        self.assertEqual(self.engine.deliver_bob()["reason"], "join")
        self.assertEqual(len(self.engine.messages), 1)

    def test_join_pair_stays_observable_when_arrivals_are_more_than_fifteen_seconds_apart(self):
        self.engine.seed_history()
        first, second = FakePlayer(301, "spectator"), FakePlayer(302, "spectator")
        self.engine.players += [first, second]
        self.guard.handle_join_attempt(first, "spectator", "blue")
        first.team = "blue"
        self.engine.auto.last_new_player_id = 301
        self.engine.now += 60
        self.guard.handle_join_attempt(second, "spectator", "red")
        self.assertIsNotNone(self.guard._join_inflight)

    def test_dependency_reload_at_round_end_cannot_mix_old_and_new_observations(self):
        self.guard.handle_round_start(6)
        self.engine.registry["balance"] = SimpleNamespace(
            ratings=self.engine.balance.ratings.copy(), ratings_lock=FakeLock(), switch_suggestion=None
        )
        self.guard.handle_round_end({"TEAM_WON": "BLUE"})
        self.assertEqual(len(self.guard._history), 0)

    def test_delayed_join_result_is_discarded_after_round_start_or_reset(self):
        for reset in [lambda: self.guard.handle_round_start(6), lambda: self.guard.handle_reset()]:
            self.engine = Engine()
            self.guard = self.engine.guard
            self.engine.seed_history()
            self.engine.join_pair()
            reset()
            self.engine.run_delay(0.2)
            self.assertEqual(self.engine.messages, [])

    def test_ambiguous_join_with_another_player_moving_is_not_judged(self):
        self.engine.seed_history()
        self.engine.join_pair()
        self.engine.player(101).team = "blue"
        self.engine.player(201).team = "red"
        self.engine.run_delay(0.2)
        self.assertEqual(self.engine.messages, [])

    def test_invalid_settings_fall_back_without_raising_and_disabled_means_silent(self):
        engine = Engine(
            {
                "qlx_balanceGuardEnabled": "0",
                "qlx_balanceGuardCooldown": "nan",
                "qlx_balanceGuardLead": "2.5",
                "qlx_balanceGuardPerformanceRatio": "0",
            }
        )
        self.assertEqual(engine.guard.rules.cooldown, 20)
        self.assertEqual(engine.guard.rules.lead, 2)
        self.assertEqual(engine.guard.rules.performance_ratio, 1.25)
        engine.suggest()
        engine.guard._observe_suggestion()
        self.assertEqual(engine.messages, [])

    def test_cooldown_and_match_budget_bound_the_output(self):
        self.guard.rules = replace(self.guard.rules, max_messages=2)
        state = self.guard._snapshot()
        decision = logic.Decision("matchpoint")
        self.guard._announce(decision, state)
        self.engine.deliver_bob()
        self.guard._announce(decision, state)
        self.engine.now += 20
        self.guard._announce(decision, state)
        self.engine.deliver_bob()
        self.engine.now += 20
        self.guard._announce(decision, state)
        self.assertEqual(len(self.engine.messages), 2)
        self.assertEqual(self.guard._suppressed, 2)

    def test_unloaded_instance_does_not_reschedule_or_emit(self):
        self.guard.handle_unload("balance_guard")
        del self.engine.registry["balance_guard"]
        self.engine.run_delay(1)
        self.assertEqual(self.engine.scheduled, [])
        self.guard.handle_countdown()
        self.assertEqual(self.engine.messages, [])

    def test_admin_commands_are_restricted_and_feedback_contains_no_player_data(self):
        self.assertTrue(all(permission == 3 for _handler, permission in self.engine.commands.values()))
        channel = SimpleNamespace(reply=self.engine.messages.append)
        self.guard._announce(logic.Decision("matchpoint"), self.guard._snapshot())
        self.engine.deliver_bob()
        self.guard.cmd_feedback(None, ["guardfeedback", "1", "gut"], channel)
        self.assertEqual(self.guard._events[0], {"number": 1, "reason": "matchpoint", "feedback": "gut"})
        self.assertEqual(self.guard.cmd_feedback(None, ["guardfeedback", "x", "gut"], channel), "usage")

    def test_missing_busy_or_failing_bob_has_no_canned_fallback(self):
        state = self.guard._snapshot()
        bot = self.engine.registry.pop("openai_bot")
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.engine.registry["openai_bot"] = bot
        self.engine.bob_busy = True
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.engine.bob_busy = False
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.engine.deliver_bob(success=False)
        self.assertEqual(self.engine.messages, [])
        self.assertEqual(self.guard._events, logic.deque())
        self.assertEqual(self.guard._requests, 1)

    def test_pending_request_does_not_queue_duplicates_and_failures_consume_budget(self):
        self.guard.rules = replace(self.guard.rules, max_messages=1)
        state = self.guard._snapshot()
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.engine.now += 30
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.assertEqual(len(self.engine.bob_requests), 1)
        self.engine.deliver_bob(success=False)
        self.guard._announce(logic.Decision("matchpoint"), state)
        self.assertEqual(self.engine.bob_requests, [])

    def test_pending_response_is_discarded_after_reset_unload_score_roster_or_timeout(self):
        changes = [
            lambda e: e.guard.handle_reset(),
            lambda e: e.guard.handle_unload("balance_guard"),
            lambda e: setattr(e.game, "red_score", 1),
            lambda e: setattr(e.player(104), "team", "spectator"),
            lambda e: setattr(e, "now", e.now + 13),
        ]
        for change in changes:
            engine = Engine()
            engine.guard._announce(logic.Decision("matchpoint"), engine.guard._snapshot())
            change(engine)
            engine.deliver_bob()
            self.assertEqual(engine.messages, [])

    def test_comment_can_describe_just_executed_original_switch_without_claiming_a_veto(self):
        self.engine.seed_history()
        self.engine.suggest()
        self.guard._observe_suggestion()
        self.engine.player(101).team = "blue"
        self.engine.player(201).team = "red"
        self.engine.deliver_bob()
        self.assertEqual(len(self.engine.messages), 1)

    def test_status_exposes_feedback_id_only_after_actual_delivery(self):
        replies = []
        channel = SimpleNamespace(reply=replies.append)
        self.guard._announce(logic.Decision("matchpoint"), self.guard._snapshot())
        self.guard.cmd_status(None, [], channel)
        self.assertIn("Kommentar-ID=keine", replies[-1])
        self.engine.deliver_bob()
        self.guard.cmd_status(None, [], channel)
        self.assertIn("Kommentar-ID=1 (matchpoint)", replies[-1])

    def test_feedback_rejects_non_ascii_numeric_glyphs(self):
        channel = SimpleNamespace(reply=self.engine.messages.append)
        self.assertEqual(self.guard.cmd_feedback(None, ["guardfeedback", "²", "gut"], channel), "usage")

    def test_warmup_other_gametypes_and_missing_game_are_ignored(self):
        self.engine.game.state = "warmup"
        self.assertIsNone(self.guard._snapshot())
        self.engine.game.state = "in_progress"
        self.engine.game.type_short = "ctf"
        self.assertIsNone(self.guard._snapshot())
        self.engine.game = None
        self.assertIsNone(self.guard._snapshot())

    def test_no_action_database_network_or_handler_replacement_code_exists(self):
        tree = ast.parse(SOURCE.read_bytes())
        banned = {
            "put",
            "switch",
            "execute_suggestion",
            "cmd_dont",
            "console_command",
            "callvote",
            "change_map",
            "kick",
            "add_request",
            "_send_message",
            "_gather_completion",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, banned)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
                self.assertFalse(set(modules) & {"redis", "requests", "openai", "socket", "subprocess"})


if __name__ == "__main__":
    unittest.main()
