from collections import defaultdict
from decimal import Decimal
from typing import Any, cast
import math
from collections import defaultdict
from fractions import Fraction
import random

from openslides_backend.shared.typing import HistoryInformation

from ....services.datastore.commands import GetManyRequest
from ....shared.exceptions import ActionException, VoteServiceException
from ....shared.interfaces.write_request import WriteRequest
from ....shared.patterns import (
    collection_from_fqid,
    collectionfield_and_fqid_from_fqfield,
    fqid_from_collection_and_id,
)
from ...action import Action
from ..option.set_auto_fields import OptionSetAutoFields
from ..projector_countdown.mixins import CountdownCommand, CountdownControl
from ..vote.create import VoteCreate
from ..vote.user_token_helper import get_user_token
from .functions import check_poll_or_option_perms


class PollValidationMixin(Action):
    def validate_instance(self, instance: dict[str, Any]) -> None:
        super().validate_instance(instance)

        if poll_id := instance.get("id"):
            poll = self.datastore.get(
                fqid_from_collection_and_id("poll", poll_id),
                ["max_votes_amount", "min_votes_amount", "max_votes_per_option"],
            )
        max_votes_amount = cast(
            int,
            instance.get(
                "max_votes_amount", poll["max_votes_amount"] if poll_id else 1
            ),
        )
        min_votes_amount = cast(
            int,
            instance.get(
                "min_votes_amount", poll["min_votes_amount"] if poll_id else 1
            ),
        )
        max_votes_per_option = cast(
            int,
            instance.get(
                "max_votes_per_option", poll["max_votes_per_option"] if poll_id else 1
            ),
        )

        if max_votes_amount < max_votes_per_option:
            raise ActionException(
                "The maximum votes per option cannot be higher than the maximum amount of votes in total."
            )
        if max_votes_amount < min_votes_amount:
            raise ActionException(
                "The minimum amount of votes cannot be higher than the maximum amount of votes."
            )


class PollPermissionMixin(Action):
    def check_permissions(self, instance: dict[str, Any]) -> None:
        if "meeting_id" in instance:
            content_object_id = instance.get("content_object_id", "")
            meeting_id = instance["meeting_id"]
        else:
            poll = self.datastore.get(
                fqid_from_collection_and_id("poll", instance["id"]),
                ["content_object_id", "meeting_id"],
                lock_result=False,
            )
            content_object_id = poll.get("content_object_id", "")
            meeting_id = poll["meeting_id"]
        if not content_object_id:
            raise ActionException("No 'content_object_id' was given")
        check_poll_or_option_perms(
            content_object_id, self.datastore, self.user_id, meeting_id
        )


class StopControl(CountdownControl, Action):
    def build_write_request(self) -> WriteRequest | None:
        """
        Reduce locked fields
        """
        self.datastore.locked_fields = {
            k: v
            for k, v in self.datastore.locked_fields.items()
            if collectionfield_and_fqid_from_fqfield(k)[0]
            not in (
                "meeting_user/user_id",
                "meeting_user/vote_delegated_to_id",
                "poll/pollmethod",
                "poll/global_option_id",
                "poll/meeting_id",
                "poll/content_object_id",
                "meeting/users_enable_vote_weight",
                "meeting/poll_couple_countdown",
                "meeting/poll_countdown_id",
                "option/meeting_id",
            )
        }
        return super().build_write_request()

    def on_stop(self, instance: dict[str, Any]) -> None:
        poll = self.datastore.get(
            fqid_from_collection_and_id(self.model.collection, instance["id"]),
            [
                "state",
                "meeting_id",
                "pollmethod",
                "global_option_id",
                "entitled_group_ids",
                "content_object_id"
            ],
        )
        
        if poll["pollmethod"] == "STV":
            self.handle_stv(instance, poll)
        else:
            self.handle_general_poll(instance, poll)

    def handle_general_poll(self, instance: dict[str, Any], poll) -> None:
        # reset countdown given by meeting
        meeting = self.datastore.get(
            fqid_from_collection_and_id("meeting", poll["meeting_id"]),
            [
                "poll_couple_countdown",
                "poll_countdown_id",
                "users_enable_vote_weight",
                "users_enable_vote_delegations",
            ],
        )
        if meeting.get("poll_couple_countdown") and meeting.get("poll_countdown_id"):
            self.control_countdown(meeting["poll_countdown_id"], CountdownCommand.RESET)

        # stop poll in vote service and create vote objects
        results = self.vote_service.stop(instance["id"])
        action_data = []
        votesvalid = Decimal("0.000000")
        option_results: dict[int, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.000000"))
        )  # maps options to their respective YNA sums
        for ballot in results["votes"]:
            user_token = get_user_token()
            vote_weight = Decimal(ballot["weight"])
            votesvalid += vote_weight
            vote_template: dict[str, str | int] = {"user_token": user_token}
            if "vote_user_id" in ballot:
                vote_template["user_id"] = ballot["vote_user_id"]
            if "request_user_id" in ballot:
                vote_template["delegated_user_id"] = ballot["request_user_id"]

            if isinstance(ballot["value"], dict):
                for option_id_str, value in ballot["value"].items():
                    option_id = int(option_id_str)

                    vote_value = value
                    vote_weighted = vote_weight  # use new variable vote_weighted because pollmethod=Y/N does not imply anymore that only one loop is done (see max_votes_per_option)
                    if poll["pollmethod"] in ("Y", "N"):
                        if value == 0:
                            continue
                        vote_value = poll["pollmethod"]
                        vote_weighted *= value

                    option_results[option_id][vote_value] += vote_weighted
                    action_data.append(
                        {
                            "value": vote_value,
                            "option_id": option_id,
                            "weight": str(vote_weighted),
                            **vote_template,
                        }
                    )
            elif isinstance(ballot["value"], str):
                vote_value = ballot["value"]
                option_id = poll["global_option_id"]
                option_results[option_id][vote_value] += vote_weight
                action_data.append(
                    {
                        "value": vote_value,
                        "option_id": option_id,
                        "weight": str(vote_weight),
                        **vote_template,
                    }
                )
            else:
                raise VoteServiceException("Invalid response from vote service")
        
        self.execute_other_action(VoteCreate, action_data)
        # update results into option
        self.execute_other_action(
            OptionSetAutoFields,
            [
                {
                    "id": _id,
                    "yes": str(option["Y"]),
                    "no": str(option["N"]),
                    "abstain": str(option["A"]),
                }
                for _id, option in option_results.items()
            ],
        )
        # set voted ids
        voted_ids = results["user_ids"]
        instance["voted_ids"] = voted_ids

        # set votescast, votesvalid, votesinvalid
        instance["votesvalid"] = str(votesvalid)
        instance["votescast"] = str(Decimal("0.000000") + Decimal(len(voted_ids)))
        instance["votesinvalid"] = "0.000000"

        # set entitled users at stop.
        instance["entitled_users_at_stop"] = self.get_entitled_users(
            poll | instance, meeting
        )
    
    def handle_stv(self, instance: dict[str, Any], poll) -> None:
        meeting = self.datastore.get(
            fqid_from_collection_and_id("meeting", poll["meeting_id"]),
            [
                "poll_couple_countdown",
                "poll_countdown_id",
                "users_enable_vote_weight",
                "users_enable_vote_delegations",
            ],
        )
        if meeting.get("poll_couple_countdown") and meeting.get("poll_countdown_id"):
            self.control_countdown(meeting["poll_countdown_id"], CountdownCommand.RESET)
        assignment_id = int(poll["content_object_id"].split("/")[1])
        assignment = self.datastore.get(
            fqid_from_collection_and_id("assignment", assignment_id),
            [
                "open_posts",
            ],
        )
    
        # stop poll in vote service and create vote objects
        results = self.vote_service.stop(instance["id"])
        action_data = []
        votesvalid = Decimal("0.000000")

        for ballot in results["votes"]:
            user_token = get_user_token()
            vote_weight = Decimal(ballot["weight"])
            votesvalid += vote_weight
            vote_template: dict[str, str | int] = {"user_token": user_token}
            if "vote_user_id" in ballot:
                vote_template["user_id"] = ballot["vote_user_id"]
            if "request_user_id" in ballot:
                vote_template["delegated_user_id"] = ballot["request_user_id"]

            for option_id_str, value in ballot["value"].items():
                option_id = int(option_id_str)

                vote_value = str(value)
                vote_weighted = vote_weight

                action_data.append(
                    {
                        "value": vote_value,
                        "option_id": option_id,
                        "weight": str(vote_weighted),
                        **vote_template,
                    }
                )

        votes = list(map(lambda ballot: {user_id: int(rank) for user_id, rank in ballot["value"].items()}, results["votes"]))
        stv_results = self.run_stv(votes, assignment["open_posts"])

        option_results: dict[int, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.000000"))
        ) # maps options to their respective rankings

        for index, option in enumerate(stv_results):
            # default to 1 vote for all winners
            option_results[int(option)]["Y"] = Decimal("1.000000")

        self.execute_other_action(VoteCreate, action_data)
        # update results into option
        self.execute_other_action(
            OptionSetAutoFields,
            [
                {
                    "id": _id,
                    "yes": str(option["Y"]),
                    "no": str(option["N"]),
                    "abstain": str(option["A"]),
                }
                for _id, option in option_results.items()
            ],
        )
        # set voted ids
        voted_ids = results["user_ids"]
        instance["voted_ids"] = voted_ids

        # set votescast, votesvalid, votesinvalid
        instance["votesvalid"] = str(votesvalid)
        instance["votescast"] = str(Decimal("0.000000") + Decimal(len(voted_ids)))
        instance["votesinvalid"] = "0.000000"

        # set entitled users at stop.
        instance["entitled_users_at_stop"] = self.get_entitled_users(
            poll | instance, meeting
        )
        
    def get_entitled_users(
        self, poll: dict[str, Any], meeting: dict[str, Any]
    ) -> list[dict[str, Any]]:
        entitled_users = []
        all_voted_users = set(poll.get("voted_ids", []))

        # get all users from the groups.
        gmr = GetManyRequest(
            "group", poll.get("entitled_group_ids", []), ["meeting_user_ids"]
        )
        gm_result = self.datastore.get_many([gmr])
        groups = gm_result.get("group", {}).values()

        # fetch presence status
        meeting_user_ids = set()
        for group in groups:
            meeting_user_ids.update(group.get("meeting_user_ids", []))
        gmr = GetManyRequest(
            "meeting_user", list(meeting_user_ids), ["user_id", "vote_delegated_to_id"]
        )
        gm_result = self.datastore.get_many([gmr])
        meeting_users = gm_result.get("meeting_user", {}).values()

        mu_to_user_id = {}
        if meeting.get("users_enable_vote_delegations"):
            # fetch vote delegations
            delegated_to_mu_ids = list(
                {id_ for mu in meeting_users if (id_ := mu.get("vote_delegated_to_id"))}
            )
            if delegated_to_mu_ids:
                gmr = GetManyRequest("meeting_user", delegated_to_mu_ids, ["user_id"])
                mu_to_user_id = self.datastore.get_many([gmr]).get("meeting_user", {})

        gmr = GetManyRequest(
            "user",
            [mu["user_id"] for mu in meeting_users],
            ["is_present_in_meeting_ids"],
        )
        users = self.datastore.get_many([gmr]).get("user", {})

        for mu in meeting_users:
            entitled_users.append(
                {
                    "voted": mu["user_id"] in all_voted_users,
                    "present": poll["meeting_id"]
                    in users[mu["user_id"]].get("is_present_in_meeting_ids", []),
                    "user_id": mu["user_id"],
                    "vote_delegated_to_user_id": (
                        mu_to_user_id[vote_mu_id]["user_id"]
                        if (vote_mu_id := mu.get("vote_delegated_to_id"))
                        and meeting.get("users_enable_vote_delegations")
                        else None
                    ),
                }
            )

        return entitled_users

    def run_stv(self, ballots_by_rank, num_seats):
        """
        ballots_by_rank: list of dicts {candidate_id: rank}, where 1 is highest preference
        num_seats: number of winners to elect
        """

        # List of rounds to track vote distribution
        rounds = []

        # Convert ranked dicts into ordered preference lists
        processed_ballots = []
        for rank_dict in ballots_by_rank:
            # Sort candidates by rank value (lower is higher preference)
            ranked_candidates = sorted(rank_dict.items(), key=lambda x: x[1])
            preference_list = [cand for cand, _ in ranked_candidates]
            processed_ballots.append((Fraction(1), preference_list))

        total_votes = len(processed_ballots)
        quota = math.floor(total_votes / (num_seats + 1)) + 1

        elected = []
        eliminated = set()
        all_candidates = {cand for ballot in ballots_by_rank for cand in ballot}

        def get_active_candidates():
            return all_candidates - set(elected) - eliminated

        def count_votes():
            tally = defaultdict(Fraction)
            for weight, prefs in processed_ballots:
                for c in prefs:
                    if c in get_active_candidates():
                        tally[c] += weight
                        break
            return tally

        # Add initial vote counts as one round
        original_candidates = {}
        tally = count_votes()
        for candidate in sorted(list(all_candidates)):
                count = tally[candidate] if candidate in tally else 0
                original_candidates[candidate] = {
                    'startingVotes': count,
                    'isElected': False,
                    'isEliminated': False,
                    'votesAdded': 0,
                }
        rounds.append(original_candidates)

        while len(elected) < num_seats:
            tally = count_votes()

            candidates_for_round = {}
            # In each round, for each candidate, track:
                # name/ID (key)
                # starting % votes
                # % votes added this round
                # is elected this round?
                # is eliminated this round?

            # Populate defalut values for the round
            for candidate in sorted(list(all_candidates)):
                count = tally[candidate] if candidate in tally else 0
                candidates_for_round[candidate] = {
                    'startingVotes': count,
                    'isElected': False,
                    'isEliminated': False,
                    'votesAdded': 0,
                }

            candidates_over_threshold = []
            # Find all candidates who meet quota
            for candidate, count in tally.items():
                if count >= quota and candidate not in elected:
                    candidates_over_threshold.append((candidate, count))

            if len(candidates_over_threshold) > 0:
                # Select candidate with highest count to elect
                candidate, count = sorted(candidates_over_threshold, key=lambda x: -x[1])[0]

                # Get other candidates over the threshold so new votes are not distributed to them
                other_qualified_candidates = set(map(lambda x: x[0], candidates_over_threshold))

                elected.append(candidate)
                surplus = count - quota
                if surplus > 0:
                    transfer_value = surplus / count
                    new_ballots = []
                    for weight, prefs in processed_ballots:
                        if prefs and prefs[0] == candidate:
                            # Transfer proportional excess votes from this candidate to each voter's next choice candidate.
                            # Do not distribute to candidates who have been elected, eliminated, or crossed the threshold but are not yet elected
                            new_prefs = [c for c in prefs[1:] if c in get_active_candidates() and c not in other_qualified_candidates]
                            if new_prefs:
                                new_ballots.append((weight * transfer_value, new_prefs))
                                candidates_for_round[new_prefs[0]]['votesAdded'] += weight * transfer_value
                        else:
                            new_ballots.append((weight, prefs))
                    processed_ballots = new_ballots
                    candidates_for_round[candidate]['isElected'] = True

            else:
                # No one reached quota: eliminate lowest
                if not tally:
                    break

                active_candidates = get_active_candidates()
                # Get all candidates with lowest value, break ties randomly
                min_value = tally[min(active_candidates, key=lambda c: (tally[c], c))]
                lowest_candidates = [c for c in active_candidates if tally[c] == min_value]
                lowest = random.choice(lowest_candidates)
                
                eliminated.add(lowest)
                candidates_for_round[lowest]['isEliminated'] = True

                new_ballots = []
                for weight, prefs in processed_ballots:
                    # Transfer proportional excess votes from this candidate to each voter's next choice candidate.
                    new_prefs = [c for c in prefs if c != lowest]
                    if new_prefs:
                        candidates_for_round[new_prefs[0]]['votesAdded'] += weight
                        new_ballots.append((weight, new_prefs))
                processed_ballots = new_ballots

            # Add candidate data for round
            rounds.append(candidates_for_round)

            # Elect all remaining candidates if only as many remain as seats left
            if len(get_active_candidates()) + len(elected) == num_seats:
                for c in get_active_candidates():
                    if c not in elected:
                        elected.append(c)
                break

        return elected


class PollHistoryMixin(Action):
    poll_history_information: str

    def get_history_information(self) -> HistoryInformation | None:
        # no datastore access necessary if information is in payload
        polls = self.get_instances_with_fields(["content_object_id"])
        return {
            poll["content_object_id"]: [
                f"{self.get_history_title(poll)} {self.poll_history_information}"
            ]
            for poll in polls
        }

    def get_history_title(self, poll: dict[str, Any]) -> str:
        content_collection = collection_from_fqid(poll["content_object_id"])
        if content_collection == "assignment":
            return "Ballot"
        return "Voting"
