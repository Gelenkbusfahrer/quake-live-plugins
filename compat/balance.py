# minqlx - A Quake Live server administrator bot.
# Copyright (C) 2015 Mino <mino@minomino.org>

# This file is part of minqlx.

# minqlx is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# minqlx is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with minqlx. If not, see <http://www.gnu.org/licenses/>.

import minqlx
import requests
import itertools
import threading
import random
import time
import math

import asyncio
import aiohttp
from aiohttp import ClientTimeout
from aiohttp_retry import ExponentialRetry, RetryClient
from requests import codes

from minqlx.database import Redis

from abc import abstractmethod

STDDEV_BALANCING_ACTIVATED = False
BALANCE_MULTI_SWITCHES = False

RATING_KEY = "minqlx:players:{0}:ratings:{1}"  # 0 == steam_id, 1 == short gametype.

MAX_ATTEMPTS = 3
CACHE_EXPIRE = 60 * 10  # 10 minutes TTL.
DEFAULT_RATING = 1500
UNTRACKED_RATING = 9999
SUPPORTED_GAMETYPES = ("ad", "ca", "ctf", "dom", "ft", "tdm")
# Externally supported game types. Used by !getrating for game types the API works with.
EXT_SUPPORTED_GAMETYPES = ("ad", "ca", "ctf", "dom", "ft", "tdm", "duel", "ffa")


async def fetch_rating_response(request_url, headers=None):
    retry_options = ExponentialRetry(
        attempts=3,
        factor=0.1,
#        statuses={500, 502, 504},
        exceptions={aiohttp.ClientResponseError, aiohttp.ClientPayloadError},
    )
    async with RetryClient(
        raise_for_status=False,
        retry_options=retry_options,
        timeout=ClientTimeout(total=5, connect=3, sock_connect=3, sock_read=5),
    ) as retry_client, retry_client.get(request_url, headers=headers) as result:
        if result.status != codes.ok:
            return result.status, None
        return result.status, await result.json()


class balance(minqlx.Plugin):
    database = Redis

    def __init__(self):
        super().__init__()

        self.add_hook("round_countdown", self.handle_round_countdown)
        self.add_hook("round_start", self.handle_round_start)
        self.add_hook("round_end", self.handle_round_end)
        self.add_hook("vote_ended", self.handle_vote_ended)
        self.add_hook("player_disconnect", self.handle_player_disconnect)
        self.add_hook("map", self.handle_map_change)
        self.add_command(
            ("setrating", "setelo"), self.cmd_setrating, 3, usage="<id> <rating>"
        )
        self.add_command(
            ("getrating", "getelo", "elo"), self.cmd_getrating, usage="<id> [gametype]"
        )
        self.add_command(("remrating", "remelo"), self.cmd_remrating, 3, usage="<id>")
        self.add_command("balance", self.cmd_balance, 1)
        self.add_command(("teams", "teens"), self.cmd_teams)
        self.add_command("do", self.cmd_do, 1)
        self.add_command("dont", self.cmd_dont, 1)
        self.add_command(("agree", "a"), self.cmd_agree, client_cmd_perm=0)
        self.add_command(("veto", "v"), self.cmd_veto, client_cmd_perm=0)
        self.add_command(("ratings", "elos", "selo"), self.cmd_ratings)

        self.ratings_lock = threading.RLock()
        # Keys: steam_id - Items: {"ffa": {"elo": 123, "games": 321, "local": False}, ...}
        self.ratings = {}
        # Keys: steam_id -
        # Items:
        # {
        #     "deactivated": true/false,
        #     "ratings": {...},
        #     "allowRating": true/false,
        #     "privacy": "public/private/anonymous/untracked"
        # }
        self.player_info = {}
        # Keys: request_id - Items: (players, callback, channel)
        self.requests = {}
        self.request_counter = itertools.count()
        self.switched_players = []
        self.switch_suggestion = None
        self.in_countdown = False

        self.set_cvar_once("qlx_balanceUseLocal", "1")
        self.set_cvar_once("qlx_balanceUrl", "qlstats.net")
        self.set_cvar_once("qlx_balanceAuto", "1")
        self.set_cvar_once("qlx_balanceMinimumSuggestionDiff", "25")
        self.set_cvar_once("qlx_balanceMinimumStddevDiff", "50")
        self.set_cvar_once("qlx_balanceApi", "elo")
        # indicates whether switch suggestions need to be opted-in (!a) or vetoed (!v) by the suggested players
        self.set_cvar_once("qlx_balanceSwitchOptIn", "1")
        # turns on a center print message at round_countdown for the two players if they didn't react, yet.
        self.set_cvar_once("qlx_balanceNagSuggestees", "0")
        # number of rounds the suggested player pair might be nagged at round_countdown
        self.set_cvar_once("qlx_balanceNagRoundCount", "3")
        # log all switch suggestions before rating them
        self.set_cvar_once("qlx_balanceLogSuggestions", "0")
        # if set to true, this avoids suggesting the same players for switching in the same game twice, might lead to
        # fewer possible suggestions
        self.set_cvar_once("qlx_balanceUniquePlayerSwitches", "0")
        # currently possible values: diff, maxscore, eloscore, stddev
        # diff is the default behavior solely based upon difference comparison
        # maxscore prefers switches of players with the lower maxscore
        # eloscore prefers witches of player pair with the highest score/time divided by elo
        # stddev balances teams on the team average and stddev
        self.set_cvar_once("qlx_balanceRatingStrategy", "diff")
        # Filter percentage for the max score rating strategy, where 1.0 == 100%, default: 0.2 (20%)
        # Outliers for the max_score of the player pair in this range are also taken into account, and the one with the
        # lowest score_sum is used
        self.set_cvar_once("qlx_balanceMaxScoreRatingFilterPercentage", "0.2")

        self.use_local = self.get_cvar("qlx_balanceUseLocal", bool)
        self.api_url = "http://{}/{}/".format(
            self.get_cvar("qlx_balanceUrl"), self.get_cvar("qlx_balanceApi")
        )
        self.switch_opt_in = self.get_cvar("qlx_balanceSwitchOptIn", bool)
        self.nag_suggestees = self.get_cvar("qlx_balanceNagSuggestees", bool)
        self.nag_round_count = self.get_cvar("qlx_balanceNagRoundCount", int)
        self.nagging_rounds_left = 0
        self.unique_player_switches = self.get_cvar(
            "qlx_balanceUniquePlayerSwitches", bool
        )
        self.log_suggestions = self.get_cvar("qlx_balanceLogSuggestions", bool)

        self.minimum_suggestion_diff = self.get_cvar(
            "qlx_balanceMinimumSuggestionDiff", int
        )
        self.minimum_suggestion_stddev_diff = self.get_cvar(
            "qlx_balanceMinimumStddevDiff", int
        )

    def handle_round_countdown(self, *args, **kwargs):
        if (
            self.switch_opt_in
            and self.switch_suggestion is not None
            and self.switch_suggestion.all_agreed()
        ) or (
            not self.switch_opt_in
            and self.switch_suggestion is not None
            and not self.switch_suggestion.all_agreed()
        ):
            # If we don't delay the switch a bit, the round countdown sound and
            # text disappears for some weird reason.
            @minqlx.next_frame
            def f():
                self.execute_suggestion()

            f()

        self.in_countdown = True

    def handle_round_start(self, *args, **kwargs):
        self.in_countdown = False

    @minqlx.delay(2)
    def handle_round_end(self, data):
        if self.switch_suggestion is None:
            return

        if not self.nag_suggestees:
            return

        if self.nagging_rounds_left <= 0:
            return

        if self.game.roundlimit in [self.game.red_score, self.game.blue_score]:
            return

        self.nagging_rounds_left = self.nagging_rounds_left - 1

        try:
            self.switch_suggestion.validate_players()
        except minqlx.NonexistentPlayerError:
            self.switch_suggestion = None

        self.switch_suggestion.nag_players_that_didnt_agree()

    def handle_vote_ended(self, votes, vote, args, passed):
        if passed and vote == "shuffle" and self.get_cvar("qlx_balanceAuto", bool):
            gt = self.game.type_short
            if gt not in SUPPORTED_GAMETYPES:
                return

            @minqlx.delay(3.5)
            def f():
                players = self.teams()
                if len(players["red"] + players["blue"]) % 2 != 0:
                    self.msg(
                        "Teams were ^6NOT^7 balanced due to the total number of players being an odd number."
                    )
                    return

                players = {p.steam_id: gt for p in players["red"] + players["blue"]}
                self.add_request(players, self.callback_balance, minqlx.CHAT_CHANNEL)

            f()

    def handle_player_disconnect(self, player, reason):
        self.clean_player_data(player)

    @minqlx.thread
    def clean_player_data(self, player):
        for p in self.players().copy():
            if p.steam_id == player.steam_id and p.id != player.id:
                # there is a second client with same steam id
                return

        if player.steam_id in self.player_info:
            del self.player_info[player.steam_id]

        with self.ratings_lock:
            if player.steam_id in self.ratings:
                del self.ratings[player.steam_id]

    @minqlx.thread
    def fetch_ratings(self, players, request_id):
        if not players:
            return

        # We don't want to modify the actual dict, so we use a copy.
        players = players.copy()

        # Get local ratings if present in DB.
        if self.use_local:
            for steam_id in players.copy():
                gt = players[steam_id]
                key = RATING_KEY.format(steam_id, gt)
                if key in self.db:
                    with self.ratings_lock:
                        if steam_id in self.ratings:
                            self.ratings[steam_id][gt] = {
                                "games": -1,
                                "elo": int(self.db[key]),
                                "local": True,
                                "time": -1,
                            }
                        else:
                            self.ratings[steam_id] = {
                                gt: {
                                    "games": -1,
                                    "elo": int(self.db[key]),
                                    "local": True,
                                    "time": -1,
                                }
                            }
                    del players[steam_id]

        response_code = self.fetch_ratings_from_qlstats(self.api_url, players)

        if response_code == requests.codes.ok and self.game is not None:
            gametype = self.game.type_short
            defaulted_players = [
                player.steam_id
                for player in players
                if self.ratings[player.steam_id][gametype]["elo"] == 900
                or self.ratings[player.steam_id][gametype]["games"] == 0
            ]
            if defaulted_players and len(defaulted_players) > 0:
                response_code = self.fetch_ratings_from_qlstats(
                    self.other_api_url(), defaulted_players
                )

        self.handle_ratings_fetched(request_id, response_code)

    def other_api_url(self):
        balance_url_base = self.get_cvar("qlx_balanceUrl")
        balance_api = self.get_cvar("qlx_balanceApi")
        if balance_api == "elo":
            return "http://{}/{}/".format(balance_url_base, "elo_b")

        return "http://{}/{}/".format(balance_url_base, "elo")

    def fetch_ratings_from_qlstats(self, request_url, players):
        if not players or len(players) == 0:
            return requests.codes.ok

        attempts = 0
        last_status = 0
        untracked_sids = []

        url = request_url + "+".join([str(sid) for sid in players])

        while attempts < MAX_ATTEMPTS:
            attempts += 1

            response_status, js = asyncio.run(fetch_rating_response(url))
            last_status = response_status
            if js is None:
                self.logger.debug(f"{response_status = } {js = }")
                continue

            if "players" not in js:
                last_status = -1
                continue

            # Fill our ratings dict with the ratings we just got.
            for p in js["players"]:
                sid = int(p["steamid"])
                del p["steamid"]
                t = time.time()

                with self.ratings_lock:
                    if sid not in self.ratings:
                        self.ratings[sid] = {}

                    for gt in p:
                        p[gt]["time"] = t
                        p[gt]["local"] = False
                        self.ratings[sid][gt] = p[gt]
                        if (
                            self.ratings[sid][gt]["elo"] == 0
                            and self.ratings[sid][gt]["games"] == 0
                        ):
                            self.ratings[sid][gt]["elo"] = DEFAULT_RATING

                        if sid in players and gt == players[sid]:
                            # The API gave us the game type we wanted, so we remove it.
                            del players[sid]

                    # Fill the rest of the game types the API didn't return but supports.
                    for gt in SUPPORTED_GAMETYPES:
                        if gt not in self.ratings[sid]:
                            self.ratings[sid][gt] = {
                                "games": -1,
                                "elo": DEFAULT_RATING,
                                "local": False,
                                "time": time.time(),
                            }

            # If the API didn't return all the players, we set them to the default rating.
            for sid in players:
                with self.ratings_lock:
                    if sid not in self.ratings:
                        self.ratings[sid] = {}
                    self.ratings[sid][players[sid]] = {
                        "games": -1,
                        "elo": DEFAULT_RATING,
                        "local": False,
                        "time": time.time(),
                    }

            # Setting ratings for untracked players.
            if "untracked" in js:
                untracked_sids = list(map(lambda sid: int(sid), js["untracked"]))

            for gt in SUPPORTED_GAMETYPES:
                for sid in untracked_sids:
                    with self.ratings_lock:
                        if sid not in self.ratings:
                            self.ratings[sid] = {}
                        self.ratings[sid][gt] = {
                            "games": -1,
                            "elo": UNTRACKED_RATING,
                            "local": False,
                            "time": time.time(),
                        }

            # Saving player info
            if "playerinfo" in js:
                for player, data in js["playerinfo"].items():
                    sid = int(player)
                    if player in js["playerinfo"]:
                        self.player_info[sid] = js["playerinfo"][player]
                        self.player_info[sid]["time"] = time.time()

            break

        if attempts == MAX_ATTEMPTS:
            return last_status

        return requests.codes.ok

    @minqlx.next_frame
    def handle_ratings_fetched(self, request_id, status_code):
        players, callback, channel, args = self.requests[request_id]
        del self.requests[request_id]
        if status_code != requests.codes.ok:
            # TODO: Put a couple of known errors here for more detailed feedback.
            channel.reply("ERROR {}: Failed to fetch ratings.".format(status_code))
        else:
            callback(players, channel, *args)

    def add_request(self, players, callback, channel, *args):
        req = next(self.request_counter)
        self.requests[req] = players.copy(), callback, channel, args

        # Only start a new thread if we need to make an API request.
        if self.remove_cached(players):
            self.fetch_ratings(players, req)
        else:
            # All players were cached, so we tell it to go ahead and call the callbacks.
            self.handle_ratings_fetched(req, requests.codes.ok)

    def remove_cached(self, players):
        with self.ratings_lock:
            for sid in players.copy():
                gt = players[sid]
                if sid in self.ratings and gt in self.ratings[sid]:
                    t = self.ratings[sid][gt]["time"]
                    if t == -1 or time.time() < t + CACHE_EXPIRE:
                        del players[sid]

        return players

    def cmd_getrating(self, player, msg, channel):
        if len(msg) == 1:
            sid = player.steam_id
        else:
            try:
                sid = int(msg[1])
                target_player = None
                if 0 <= sid < 64:
                    target_player = self.player(sid)
                    sid = target_player.steam_id
            except ValueError:
                player.tell("Invalid ID. Use either a client ID or a SteamID64.")
                return minqlx.RET_STOP_ALL
            except minqlx.NonexistentPlayerError:
                player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
                return minqlx.RET_STOP_ALL

        if len(msg) > 2:
            if msg[2].lower() in EXT_SUPPORTED_GAMETYPES:
                gt = msg[2].lower()
            else:
                player.tell(
                    "Invalid gametype. Supported gametypes: {}".format(
                        ", ".join(EXT_SUPPORTED_GAMETYPES)
                    )
                )
                return minqlx.RET_STOP_ALL
        else:
            gt = self.game.type_short
            if gt not in EXT_SUPPORTED_GAMETYPES:
                player.tell("This game mode is not supported by the balance plugin.")
                return minqlx.RET_STOP_ALL

        self.add_request({sid: gt}, self.callback_getrating, channel, gt)

    def callback_getrating(self, players, channel, gametype):
        sid = next(iter(players))
        player = self.player(sid)
        name = player.name if player else sid

        channel.reply(
            "{} has a rating of ^6{}^7 in {}.".format(
                name, self.ratings[sid][gametype]["elo"], gametype.upper()
            )
        )

    def cmd_setrating(self, player, msg, channel):
        if len(msg) < 3:
            return minqlx.RET_USAGE

        try:
            sid = int(msg[1])
            target_player = None
            if 0 <= sid < 64:
                target_player = self.player(sid)
                sid = target_player.steam_id
        except ValueError:
            player.tell("Invalid ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL
        except minqlx.NonexistentPlayerError:
            player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL

        try:
            rating = int(msg[2])
        except ValueError:
            player.tell("Invalid rating.")
            return minqlx.RET_STOP_ALL

        name = target_player.name if target_player else sid

        gt = self.game.type_short
        self.db[RATING_KEY.format(sid, gt)] = rating

        # If we have the player cached, set the rating.
        with self.ratings_lock:
            if sid in self.ratings and gt in self.ratings[sid]:
                self.ratings[sid][gt]["elo"] = rating
                self.ratings[sid][gt]["local"] = True
                self.ratings[sid][gt]["time"] = -1

        channel.reply(
            "{}'s {} rating has been set to ^6{}^7.".format(name, gt.upper(), rating)
        )

    def cmd_remrating(self, player, msg, channel):
        if len(msg) < 2:
            return minqlx.RET_USAGE

        try:
            sid = int(msg[1])
            target_player = None
            if 0 <= sid < 64:
                target_player = self.player(sid)
                sid = target_player.steam_id
        except ValueError:
            player.tell("Invalid ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL
        except minqlx.NonexistentPlayerError:
            player.tell("Invalid client ID. Use either a client ID or a SteamID64.")
            return minqlx.RET_STOP_ALL

        name = target_player.name if target_player else sid

        gt = self.game.type_short
        del self.db[RATING_KEY.format(sid, gt)]

        # If we have the player cached, remove the game type.
        with self.ratings_lock:
            if sid in self.ratings and gt in self.ratings[sid]:
                del self.ratings[sid][gt]

        channel.reply(
            "{}'s locally set {} rating has been deleted.".format(name, gt.upper())
        )

    def cmd_balance(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        teams = self.teams()
        if len(teams["red"] + teams["blue"]) % 2 != 0:
            player.tell("The total number of players should be an even number.")
            return minqlx.RET_STOP_ALL

        players = {p.steam_id: gt for p in teams["red"] + teams["blue"]}
        self.add_request(players, self.callback_balance, minqlx.CHAT_CHANNEL)

    def callback_balance(self, players, channel):
        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = teams["red"] + teams["blue"]
        gt = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = {p.steam_id: gt for p in current}
                self.add_request(d, self.callback_balance, channel)
                return

        # Start out by evening out the number of players on each team.
        diff = len(teams["red"]) - len(teams["blue"])
        if abs(diff) > 1:
            if diff > 0:
                for i in range(diff - 1):
                    p = teams["red"].pop()
                    p.put("blue")
                    teams["blue"].append(p)
            elif diff < 0:
                for i in range(abs(diff) - 1):
                    p = teams["blue"].pop()
                    p.put("red")
                    teams["red"].append(p)

        teams = self.teams()

        # Start shuffling by looping through our suggestion function until
        # there are no more switches that can be done to improve teams.
        switch = self.suggest_switch(teams, gt)
        if switch:
            while switch:
                p1 = switch.red_player
                p2 = switch.blue_player
                switch.execute()

                if isinstance(p1, list):
                    for player in p1:
                        teams["blue"].append(player)
                        teams["red"].remove(player)
                else:
                    teams["blue"].append(p1)
                    teams["red"].remove(p1)

                if isinstance(p2, list):
                    for player in p2:
                        teams["red"].append(player)
                        teams["blue"].remove(player)
                else:
                    teams["red"].append(p2)
                    teams["blue"].remove(p2)

                switch = self.suggest_switch(teams, gt)

            self.report_teams(
                [player.steam_id for player in teams["red"]],
                [player.steam_id for player in teams["blue"]],
                channel,
            )
        else:
            channel.reply("Teams are good! Nothing to balance.")
        return True

    def report_teams(self, red_team, blue_team, channel):
        gt = self.game.type_short

        avg_red = self.team_average(red_team, gt)
        avg_blue = self.team_average(blue_team, gt)
        avg_diff = avg_red - avg_blue

        stddev_red = self.team_stddev(red_team, gt, mu=avg_red)
        stddev_blue = self.team_stddev(blue_team, gt, mu=avg_blue)

        if int(avg_diff) > 0:
            channel.reply(
                "^1{:.0f} (deviation: {:.0f}) ^7vs ^4{:.0f} (deviation: {:.0f})^7 - DIFFERENCE: ^1{:.0f}".format(
                    avg_red, stddev_red, avg_blue, stddev_blue, abs(avg_diff)
                )
            )
            return
        if int(avg_diff) < 0:
            channel.reply(
                "^1{:.0f} (deviation: {:.0f}) ^7vs ^4{:.0f} (deviation: {:.0f})^7 - DIFFERENCE: ^4{:.0f}".format(
                    avg_red, stddev_red, avg_blue, stddev_blue, abs(avg_diff)
                )
            )
            return
        channel.reply(
            "^1{:.0f} (deviation: {:.0f}) ^7vs ^4{:.0f} (deviation: {:.0f})^7 - Holy shit!".format(
                avg_red, stddev_red, avg_blue, stddev_blue
            )
        )

    def cmd_teams(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        teams = self.teams()
        if len(teams["red"]) != len(teams["blue"]):
            player.tell("Both teams should have the same number of players.")
            return minqlx.RET_STOP_ALL

        teams = {p.steam_id: gt for p in teams["red"] + teams["blue"]}
        self.add_request(teams, self.callback_teams, channel)

    def callback_teams(self, players, channel):
        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = teams["red"] + teams["blue"]
        gametype = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = {p.steam_id: gametype for p in current}
                self.add_request(d, self.callback_teams, channel)
                return

        self.report_teams(
            [player.steam_id for player in teams["red"]],
            [player.steam_id for player in teams["blue"]],
            channel,
        )

        self.collect_suggestions(teams, gametype, channel)

    @minqlx.thread
    def collect_suggestions(self, teams, gametype, channel):
        possible_switches = self.filtered_suggestions(teams, gametype)

        if self.unique_player_switches and len(self.switched_players) > 0:
            possible_switches = list(
                filter(
                    lambda suggestion: suggestion.red_player.steam_id
                    not in self.switched_players
                    and suggestion.blue_player.steam_id not in self.switched_players,
                    possible_switches,
                )
            )
        self.handle_suggestions_collected(possible_switches, channel)

    def handle_suggestions_collected(self, possible_switches, channel):
        rating_strategy = self.rating_strategy(
            self.get_cvar("qlx_balanceRatingStrategy", str)
        )
        switch_suggestion_queue = SuggestionQueue(possible_switches, rating_strategy)

        if self.log_suggestions:
            self.logger.info("Suggestion queue: {}".format(switch_suggestion_queue))

        if switch_suggestion_queue and len(switch_suggestion_queue) > 0:
            switch = switch_suggestion_queue.best_suggestion()
            channel.reply(switch.nag_console_print())

            if not self.switch_suggestion or switch != self.switch_suggestion:
                self.switch_suggestion = switch
                if self.nag_suggestees:
                    switch.nag_players_that_didnt_agree()
                    self.nagging_rounds_left = self.nag_round_count
        else:
            i = random.randint(0, 99)
            if not i:
                channel.reply("Teens look ^6good!")
            else:
                channel.reply("Teams look good!")
            self.switch_suggestion = None

        return True

    def rating_strategy(self, strategy):
        if strategy == "eloscore":
            return EloScoreBasedRatingStrategy(self.ratings, self.game.type_short)
        if strategy == "maxscore":
            filter_percentage = self.get_cvar(
                "qlx_balanceMaxScoreRatingFilterPercentage", float
            )
            return MaxScoreSuggestionRatingStrategy(filter_percentage)
        if strategy == "biased":
            return BiasedSuggestionRatingStrategy(self.game)
        if strategy == "stddev":
            return StddevSuggestionRatingStrategy()
        return DiffSuggestionRatingStrategy()

    def cmd_do(self, player, msg, channel):
        """Forces a suggested switch to be done."""
        if not self.switch_suggestion:
            return

        if not self.switch_opt_in:
            return

        if self.log_suggestions:
            self.logger.info(
                "Forced suggestion switch: {}".format(self.switch_suggestion)
            )

        self.execute_suggestion()

    def cmd_dont(self, player, msg, channel):
        """Vetoes a suggested switch to be done."""
        if not self.switch_suggestion:
            return

        if self.switch_opt_in:
            return

        if self.log_suggestions:
            self.logger.info(
                "Vetoed suggestion switch: {}".format(self.switch_suggestion)
            )

        self.msg("An admin prevented the switch! The switch will be terminated.")
        self.switch_suggestion = None
        self.nagging_rounds_left = 0

    def cmd_agree(self, player, msg, channel):
        """After the bot suggests a switch, players in question can use this to agree to the switch."""
        if (
            not self.switch_suggestion
            or not self.switch_opt_in
            or self.switch_suggestion.all_agreed()
        ):
            return

        self.switch_suggestion.agree(player)

        if not self.switch_suggestion.all_agreed():
            return

        if self.log_suggestions:
            self.logger.info(
                "Both players agreed to suggestion: {}".format(self.switch_suggestion)
            )
        # If the game's in progress and we're not in the round countdown, wait for next round.
        if self.game.state == "in_progress" and not self.in_countdown:
            self.msg("The switch will be executed at the start of next round.")
            return

        # Otherwise, switch right away.
        self.execute_suggestion()

    def cmd_veto(self, player, msg, channel):
        """After the bot suggests a switch, players in question can use this to veto to the switch."""
        if not self.switch_suggestion or self.switch_opt_in:
            return

        self.switch_suggestion.agree(player)

        if not self.switch_suggestion.all_agreed():
            return

        if self.log_suggestions:
            self.logger.info(
                "Both players disagreed to suggestion: {}".format(
                    self.switch_suggestion
                )
            )

        self.msg("Both players vetoed! The switch will be terminated.")
        self.switch_suggestion = None
        self.nagging_rounds_left = 0

    def cmd_ratings(self, player, msg, channel):
        gt = self.game.type_short
        if gt not in EXT_SUPPORTED_GAMETYPES:
            player.tell("This game mode is not supported by the balance plugin.")
            return minqlx.RET_STOP_ALL

        players = {p.steam_id: gt for p in self.players()}
        self.add_request(players, self.callback_ratings, channel)

    def callback_ratings(self, players, channel):
        # We check if people joined while we were requesting ratings and get them if someone did.
        teams = self.teams()
        current = self.players()
        gt = self.game.type_short

        for p in current:
            if p.steam_id not in players:
                d = {p.steam_id: gt for p in current}
                self.add_request(d, self.callback_ratings, channel)
                return

        if teams["free"]:
            free_sorted = sorted(
                teams["free"],
                key=lambda x: self.ratings[x.steam_id][gt]["elo"],
                reverse=True,
            )
            free = ", ".join(
                [
                    "{}: ^6{}^7".format(
                        p.clean_name, self.ratings[p.steam_id][gt]["elo"]
                    )
                    for p in free_sorted
                ]
            )
            channel.reply(free)
        if teams["red"]:
            red_sorted = sorted(
                teams["red"],
                key=lambda x: self.ratings[x.steam_id][gt]["elo"],
                reverse=True,
            )
            red = ", ".join(
                [
                    "{}: ^1{}^7".format(
                        p.clean_name, self.ratings[p.steam_id][gt]["elo"]
                    )
                    for p in red_sorted
                ]
            )
            channel.reply(red)
        if teams["blue"]:
            blue_sorted = sorted(
                teams["blue"],
                key=lambda x: self.ratings[x.steam_id][gt]["elo"],
                reverse=True,
            )
            blue = ", ".join(
                [
                    "{}: ^4{}^7".format(
                        p.clean_name, self.ratings[p.steam_id][gt]["elo"]
                    )
                    for p in blue_sorted
                ]
            )
            channel.reply(blue)
        if teams["spectator"]:
            spec_sorted = sorted(
                teams["spectator"],
                key=lambda x: self.ratings[x.steam_id][gt]["elo"],
                reverse=True,
            )
            spec = ", ".join(
                [
                    "{}: {}".format(p.clean_name, self.ratings[p.steam_id][gt]["elo"])
                    for p in spec_sorted
                ]
            )
            channel.reply(spec)

    def suggest_switch(self, teams, gametype):
        """Suggest a switch based on average team ratings."""
        possible_switches = self.filtered_suggestions(teams, gametype)

        rating_strategy = self.rating_strategy(
            self.get_cvar("qlx_balanceRatingStrategy", str)
        )
        switch_suggestion_queue = SuggestionQueue(possible_switches, rating_strategy)

        if switch_suggestion_queue and len(switch_suggestion_queue) > 0:
            return switch_suggestion_queue.best_suggestion()

        return None

    def filtered_suggestions(self, teams, gametype):
        avg_red = self.team_average(
            [player.steam_id for player in teams["red"]], gametype
        )
        avg_blue = self.team_average(
            [player.steam_id for player in teams["blue"]], gametype
        )
        avg_diff = abs(avg_red - avg_blue)

        possible_switches = self.possible_switches(teams, gametype)

        rounds = self.game.blue_score + self.game.red_score

        if rounds < 8:
            self.minimum_suggestion_stddev_diff = 50
            self.minimum_suggestion_diff = 50
        elif rounds < 14:
            self.minimum_suggestion_stddev_diff = 100
            self.minimum_suggestion_diff = 100
        else:
            self.minimum_suggestion_stddev_diff = 150
            self.minimum_suggestion_diff = 150

        if STDDEV_BALANCING_ACTIVATED and avg_diff <= self.minimum_suggestion_diff:
            stddev_red = self.team_stddev(
                [player.steam_id for player in teams["red"]], gametype, mu=avg_red
            )
            stddev_blue = self.team_stddev(
                [player.steam_id for player in teams["blue"]], gametype, mu=avg_blue
            )
            stddev_diff = abs(stddev_red - stddev_blue)
            return list(
                filter(
                    lambda suggestion: stddev_diff - abs(suggestion.stddev_diff)
                    >= self.minimum_suggestion_stddev_diff
                    and abs(suggestion.stddev_diff)
                    <= self.minimum_suggestion_stddev_diff
                    and abs(suggestion.avg_diff) <= self.minimum_suggestion_diff,
                    possible_switches,
                )
            )

        return list(
            filter(
                lambda suggestion: avg_diff > abs(suggestion.avg_diff)
                and avg_diff - abs(suggestion.avg_diff) >= self.minimum_suggestion_diff,
                possible_switches,
            )
        )

    def possible_switches(self, teams, gametype):
        switches = []
        for red_p in teams["red"]:
            for blue_p in teams["blue"]:
                r = [
                    player.steam_id
                    for player in teams["red"]
                    if player.steam_id != red_p.steam_id
                ] + [blue_p.steam_id]
                b = [
                    player.steam_id
                    for player in teams["blue"]
                    if player.steam_id != blue_p.steam_id
                ] + [red_p.steam_id]
                avg_red = self.team_average(r, gametype)
                avg_blue = self.team_average(b, gametype)
                diff = avg_red - avg_blue

                if diff <= self.minimum_suggestion_diff:
                    if STDDEV_BALANCING_ACTIVATED:
                        stddev_red = self.team_stddev(r, gametype, mu=avg_red)
                        stddev_blue = self.team_stddev(b, gametype, mu=avg_blue)
                        stddev_diff = stddev_red - stddev_blue
                        suggestion = Suggestion(red_p, blue_p, diff, stddev_diff)
                    else:
                        suggestion = Suggestion(red_p, blue_p, diff)

                    switches.append(suggestion)

        if BALANCE_MULTI_SWITCHES:
            return switches + self.possible_multi_switches(teams, gametype)

        return switches

    def possible_multi_switches(self, teams, gametype):
        switches = []
        remaining_red = teams["red"].copy()
        for red_p1 in teams["red"]:
            remaining_red = [
                player for player in remaining_red if player.steam_id != red_p1.steam_id
            ]
            for red_p2 in remaining_red:
                remaining_blue = teams["blue"].copy()
                for blue_p1 in teams["blue"]:
                    remaining_blue = [
                        player
                        for player in remaining_blue
                        if player.steam_id != blue_p1.steam_id
                    ]
                    for blue_p2 in remaining_blue:
                        r = [
                            player.steam_id
                            for player in teams["red"]
                            if player.steam_id not in [red_p1.steam_id, red_p2.steam_id]
                        ] + [blue_p1.steam_id, blue_p2.steam_id]
                        b = [
                            player.steam_id
                            for player in teams["blue"]
                            if player.steam_id
                            not in [blue_p1.steam_id, blue_p2.steam_id]
                        ] + [red_p1.steam_id, red_p2.steam_id]

                        avg_red = self.team_average(r, gametype)
                        avg_blue = self.team_average(b, gametype)
                        diff = avg_red - avg_blue
                        if abs(diff) < self.minimum_suggestion_diff:
                            stddev_red = self.team_stddev(r, gametype, mu=avg_red)
                            stddev_blue = self.team_stddev(b, gametype, mu=avg_blue)
                            stddev_diff = stddev_red - stddev_blue

                            suggestion = CompositeSuggestion(
                                diff,
                                stddev_diff,
                                Suggestion(red_p1, blue_p1, diff, stddev_diff),
                                Suggestion(red_p2, blue_p2, diff, stddev_diff),
                            )
                            switches.append(suggestion)

        return switches

    def team_average(self, steam_ids, gametype):
        """Calculates the average rating of a team."""
        if not steam_ids or len(steam_ids) == 0:
            return 0
        return sum(
            [self.ratings[steam_id][gametype]["elo"] for steam_id in steam_ids]
        ) / len(steam_ids)

    def team_stddev(self, steam_ids, gametype, mu=None):
        """Calculates the std deviation rating of a team"""
        if not steam_ids or len(steam_ids) == 0:
            return 0
        team_elos = [
            pow(self.ratings[steam_id][gametype]["elo"] - mu, 2)
            for steam_id in steam_ids
        ]
        return math.sqrt(sum(team_elos) / len(steam_ids))

    def execute_suggestion(self):
        if self.switch_suggestion is None:
            return

        try:
            self.switch_suggestion.execute()
        except minqlx.NonexistentPlayerError:
            self.switch_suggestion = None
            return
        except PlayerMovedToSpecError:
            self.switch_suggestion = None
            return

        self.switched_players += self.switch_suggestion.affected_steam_ids()
        self.switch_suggestion = None

    def handle_map_change(self, mapname, factory):
        self.switched_players = []


class SuggestionRatingStrategy:

    @abstractmethod
    def best_suggestion(self, suggestions):
        pass


class DiffSuggestionRatingStrategy(SuggestionRatingStrategy):

    def best_suggestion(self, suggestions):
        return min(suggestions, key=lambda suggestion: abs(suggestion.avg_diff))


class BiasedSuggestionRatingStrategy(SuggestionRatingStrategy):
    def __init__(self, game):
        self.game = game

    def best_suggestion(self, suggestions):
        score_diff = self.game.red_score - self.game.blue_score
        sorted_suggestions = sorted(
            suggestions, key=lambda suggestion: suggestion.diff, reverse=score_diff > 0
        )
        return sorted_suggestions[0]


class MaxScoreSuggestionRatingStrategy(SuggestionRatingStrategy):

    def __init__(self, filter_percentage):
        self.max_score_filter_percentage = filter_percentage

    def best_suggestion(self, suggestions):
        min_max_score = min([suggestion.max_score for suggestion in suggestions])
        fuzzy_min_score = max(
            int(min_max_score * (1 - self.max_score_filter_percentage)), 0
        )
        fuzzy_max_score = max(
            int(min_max_score * (1 + self.max_score_filter_percentage)),
            min_max_score + 1,
        )
        filtered_list = [
            suggestion
            for suggestion in suggestions
            if suggestion.max_score in range(fuzzy_min_score, fuzzy_max_score)
        ]

        if len(filtered_list) == 1:
            return filtered_list[0]

        min_score_sum = min([suggestion.score_sum for suggestion in filtered_list])
        best_suggestion = [
            suggestion
            for suggestion in filtered_list
            if suggestion.score_sum == min_score_sum
        ]
        return best_suggestion[0]


class EloScoreBasedRatingStrategy(SuggestionRatingStrategy):

    def __init__(self, ratings, gametype):
        self.ratings = ratings
        self.gametype = gametype

    def best_suggestion(self, suggestions):
        rated_suggestions = {}
        for suggestion in suggestions:
            red_player_rating = self.rate_player(suggestion.red_player)
            blue_player_rating = self.rate_player(suggestion.blue_player)

            rated_suggestions[suggestion] = max(red_player_rating, blue_player_rating)

        return max(rated_suggestions, key=rated_suggestions.get)

    def rate_player(self, player):
        score_per_time = player.score / player.stats.time
        elo = self.ratings[player.steam_id][self.gametype]
        return score_per_time / elo


class StddevSuggestionRatingStrategy(SuggestionRatingStrategy):

    def best_suggestion(self, suggestions):
        multi_suggestions = [
            suggestion
            for suggestion in suggestions
            if isinstance(suggestion, CompositeSuggestion)
        ]
        single_suggestions = [
            suggestion
            for suggestion in suggestions
            if suggestion not in multi_suggestions
        ]

        if len(single_suggestions) > 0:
            return min(suggestions, key=lambda suggestion: abs(suggestion.stddev_diff))
        return min(suggestions, key=lambda suggestion: abs(suggestion.stddev_diff))


class SuggestionQueue:

    def __init__(self, items=None, strategy=DiffSuggestionRatingStrategy()):
        self.suggestions = items if items is not None else []
        self.strategy = strategy

    def __str__(self):
        return "[{}]".format(
            ", ".join([str(suggestion) for suggestion in self.suggestions])
        )

    def __len__(self):
        return len(self.suggestions)

    def best_suggestion(self):
        if len(self.suggestions) == 0:
            return None

        if len(self.suggestions) == 1:
            return self.suggestions[0]

        return self.strategy.best_suggestion(self.suggestions)


class Suggestion:

    def __init__(self, red_player, blue_player, avg_diff, stddev_diff=0):
        self.red_player = red_player
        self.blue_player = blue_player
        self.avg_diff = avg_diff
        self.stddev_diff = stddev_diff
        self._agreed = {}
        self.switch_opt_in = minqlx.Plugin.get_cvar("qlx_balanceSwitchOptIn", bool)

    def __eq__(self, other):
        if not isinstance(other, Suggestion):
            return False

        return (
            self.red_player == other.red_player
            and self.blue_player == other.blue_player
            and self.avg_diff == other.avg_diff
            and self.stddev_diff == other.stddev_diff
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __str__(self):
        red_player = "({}, score: {}, dmg: {}, time: {})".format(
            self.red_player.clean_name,
            self.red_player.score,
            self.red_player.stats.damage_dealt,
            self.red_player.stats.time,
        )
        blue_player = "({}, score: {}, dmg: {}, time: {})".format(
            self.blue_player.clean_name,
            self.blue_player.score,
            self.blue_player.stats.damage_dealt,
            self.blue_player.stats.time,
        )

        return "Switch {} with {}, resulting diff: {}".format(
            red_player,
            blue_player,
            self.avg_diff,
        )

    def nag_center_print(self):
        if self.switch_opt_in:
            return "^6!a^7 to switch"
        return "^6!v^7 to veto switching"

    def nag_console_print(self):
        if self.switch_opt_in:
            return "SUGGESTION: switch ^6{}^7 with ^6{}^7. Mentioned players can type ^6 !a^7 to agree.".format(
                self.red_player.clean_name, self.blue_player.clean_name
            )

        return (
            "NOTICE: Server will switch ^6{}^7 with ^6{}^7 at start of next round. "
            "Both mentioned players need to type ^6!v^7 to veto the switch.".format(
                self.red_player.clean_name, self.blue_player.clean_name
            )
        )

    def nag_players_that_didnt_agree(self):
        if not self.agreed(self.red_player):
            self.red_player.center_print(self.nag_center_print())
            self.red_player.tell(self.nag_console_print())

        if not self.agreed(self.blue_player):
            self.blue_player.center_print(self.nag_center_print())
            self.blue_player.tell(self.nag_console_print())

    def agree(self, player):
        self._agreed[player.steam_id] = True

    def agreed(self, player):
        return self._agreed.get(player.steam_id, False)

    def all_agreed(self):
        return self.agreed(self.red_player) and self.agreed(self.blue_player)

    def affected_steam_ids(self):
        return [self.red_player.steam_id, self.blue_player.steam_id]

    def validate_players(self):
        self.red_player.update()
        self.blue_player.update()

    def execute(self):
        self.red_player.update()
        self.blue_player.update()

        if self.red_player.team == "spectator":
            raise PlayerMovedToSpecError(self.red_player)

        if self.blue_player.team == "spectator":
            raise PlayerMovedToSpecError(self.blue_player)

        minqlx.Plugin.switch(self.red_player, self.blue_player)

    @property
    def max_score(self):
        return max(self.red_player.score, self.blue_player.score)

    @property
    def score_sum(self):
        return self.red_player.score + self.blue_player.score


class CompositeSuggestion(Suggestion):

    def __init__(self, avg_diff, stddev_diff, *suggestions):
        super().__init__(None, None, avg_diff)
        
        self.avg_diff = avg_diff
        self.stddev_diff = stddev_diff
        self.suggestions = list(suggestions)
        self.switch_opt_in = minqlx.Plugin.get_cvar("qlx_balanceSwitchOptIn", bool)

    def __eq__(self, other):
        if not isinstance(other, CompositeSuggestion):
            return False

        return all(suggestion in other.suggestions for suggestion in self.suggestions)

    def __str__(self):
        return "[{}]".format(
            ", ".join([suggestion.__str__() for suggestion in self.suggestions])
        )

    def nag_console_print(self):
        red_players = [
            suggestion.red_player.clean_name for suggestion in self.suggestions
        ]
        blue_players = [
            suggestion.blue_player.clean_name for suggestion in self.suggestions
        ]

        if self.switch_opt_in:
            return "SUGGESTION: switch ^6{}^7 with ^6{}^7. Mentioned players can type !a to agree.".format(
                ", ".join(red_players), ", ".join(blue_players)
            )

        return (
            "NOTICE: Server will switch ^6{}^7 with ^6{}^7 at start of next round. "
            "All mentioned players need to type ^6!v^7 to veto the switch.".format(
                ", ".join(red_players), ", ".join(blue_players)
            )
        )

    def nag_players_that_didnt_agree(self):
        for suggestion in self.suggestions:
            suggestion.nag_players_that_didnt_agree()

    def agree(self, player):
        for suggestion in self.suggestions:
            if player.steam_id not in suggestion.affected_steam_ids():
                continue
            suggestion.agree(player)
            return

    def agreed(self, player):
        for suggestion in [
            suggestion
            for suggestion in self.suggestions
            if player.steam_id in suggestion.affected_steam_ids()
        ]:
            return suggestion.agreed(player)

        return False

    def all_agreed(self):
        return all(suggestion.all_agreed() for suggestion in self.suggestions)

    def affected_steam_ids(self):
        affected_ids = [
            suggestion.affected_steam_ids() for suggestion in self.suggestions
        ]
        return sum(affected_ids, [])

    def validate_players(self):
        for suggestion in self.suggestions:
            suggestion.validate_players()

    def execute(self):
        for suggestion in self.suggestions:
            suggestion.execute()

    @property
    def max_score(self):
        max_scores = [suggestion.max_score for suggestion in self.suggestions]
        return max(max_scores)

    @property
    def score_sum(self):
        score_sums = [suggestion.score_sum for suggestion in self.suggestions]
        return sum(score_sums)

    @property
    def red_player(self):
        return [suggestion.red_player for suggestion in self.suggestions]

    @property
    def blue_player(self):
        return [suggestion.blue_player for suggestion in self.suggestions]


class PlayerMovedToSpecError(Exception):

    def __init__(self, player):
        self.player = player
