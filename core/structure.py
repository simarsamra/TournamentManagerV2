"""A tournament's shape: groups, knockout stages, bracket sides, and where
each team stands in it (AI_STRUCTURE_PLAN.md §1.1).

Plain functions of model objects, like core/analytics.py: no HTTP, nothing
rendered. The AI facts builders read these so the model is told what a
match was ("Semi-final", "Group B") and what state a team is in, instead of
being handed one league table for every format and left to guess.
"""
from collections import defaultdict
from dataclasses import dataclass, field

from .standings import _head_to_head_matches, _head_to_head_points, calculate_standings

KIND_LEAGUE, KIND_GROUPS, KIND_BRACKET = "league", "groups", "bracket"
LEAGUE_FORMATS = ("round_robin", "double_round_robin")
BRACKET_FORMATS = ("knockout", "double_elimination", "consolation")


def structure_kind(tournament):
    if tournament.format in LEAGUE_FORMATS:
        return KIND_LEAGUE
    if tournament.format == "hybrid":
        return KIND_GROUPS
    return KIND_BRACKET


def _round_name(match_count):
    """The bracket templates' rule (standings_content.html): named by how
    many matches the round has, byes included."""
    return {1: "Final", 2: "Semi-final", 4: "Quarter-final"}.get(match_count, f"Round of {2 * match_count}")


def _lower_first(text):
    return text[:1].lower() + text[1:]


def stage_labels(tournament, matches=None):
    """{match pk: stage label} for `matches` (default: all the tournament's).

    Labels depend on the whole bracket (how many matches a round has, which
    losers round is last), so the tournament's matches are loaded once here
    whatever `matches` is. Hybrid knockout rounds continue the group-round
    numbering, so a label never depends on round_number's value alone.
    """
    everything = list(tournament.matches.only(
        "pk", "group", "bracket_type", "round_number", "status",
    ))
    return _stage_labels(tournament, everything, everything if matches is None else list(matches))


def _stage_labels(tournament, everything, wanted):
    fmt = tournament.format

    round_sizes = defaultdict(int)       # (bracket_type, round_number) -> match count
    losers_rounds = set()
    for match in everything:
        if fmt == "hybrid" and match.group:
            continue
        round_sizes[(match.bracket_type, match.round_number)] += 1
        if match.bracket_type == "losers":
            losers_rounds.add(match.round_number)
    losers_order = {number: i for i, number in enumerate(sorted(losers_rounds), start=1)}

    def label(match):
        if fmt in LEAGUE_FORMATS:
            return f"Round {match.round_number}"
        if fmt == "hybrid" and match.group:
            return f"Group {match.group}"
        kind = match.bracket_type
        if kind == "third_place":
            return "Third-place match"
        if kind == "grand_final":
            return "Grand final decider" if match.round_number > 1 else "Grand final"
        if kind == "losers":
            index = losers_order[match.round_number]
            return "Losers bracket final" if index == len(losers_order) else f"Losers bracket round {index}"
        name = _round_name(round_sizes[(kind, match.round_number)])
        if kind == "consolation":
            return f"Consolation {_lower_first(name)}"
        if fmt == "double_elimination":
            return f"Winners bracket {_lower_first(name)}"
        return name

    return {match.pk: label(match) for match in wanted}


# -- Where each team stands (ST-4) ---------------------------------------------

TEAM_STATUSES = (
    "in_league", "placed",
    "in_contention", "through", "out_in_groups",
    "alive", "out", "playing_for_third",
    "unbeaten", "one_life_left",
    "in_consolation", "consolation_winner",
    "champion", "runner_up", "third",
    "withdrawn",
)
DONE = ("confirmed", "forfeited")
UNFINISHED = ("upcoming", "in_progress", "pending_confirmation", "disputed")
# A group match in one of these no longer changes anything.
GROUP_MATCH_OVER = DONE + ("cancelled", "bye")
TIEBREAKER_WORDS = {"game_diff": "game difference", "games_won": "games won", "head_to_head": "head-to-head"}


@dataclass
class TeamState:
    team_id: int
    label: str
    group: str = ""
    status: str = ""
    detail: str = ""          # a stage ("Semi-final") or a placing ("5th")
    withdrawn: bool = False

    @property
    def text(self):
        return status_text(self.status, self.detail)


@dataclass
class Structure:
    kind: str
    phase: str                                   # league | group_stage | knockout | finished
    advance_per_group: int | None = None
    groups: dict = field(default_factory=dict)   # letter -> calculate_standings rows
    table: list = field(default_factory=list)    # league: calculate_standings rows
    teams: dict = field(default_factory=dict)    # team pk -> TeamState
    stages: dict = field(default_factory=dict)   # match pk -> stage label
    placings: dict = field(default_factory=dict)
    tiebreakers: list = field(default_factory=list)


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def status_text(status, detail=""):
    """The words a status is shown (and quoted) as."""
    stage = _lower_first(detail) if detail else ""
    if status == "alive":
        return f"through to the {stage}" if stage else "still in"
    if status == "out":
        if detail == "Third-place match":
            return "lost the third-place match"
        return f"out in the {stage}" if stage else "out"
    if status == "placed":
        return f"finished {detail}"
    if status == "in_consolation" and stage:
        return f"in the consolation bracket, next the {stage}"
    return {
        "in_league": "in the league",
        "in_contention": "still in the race to go through",
        "through": "through to the knockouts",
        "out_in_groups": "out in the group stage",
        "playing_for_third": "playing for third place",
        "unbeaten": "unbeaten, in the winners bracket",
        "one_life_left": "one loss, in the losers bracket: one more and they're out",
        "in_consolation": "in the consolation bracket",
        "consolation_winner": "won the consolation bracket",
        "champion": "champion",
        "runner_up": "runner-up",
        "third": "third",
        "withdrawn": "withdrew",
    }.get(status, "")


def _teams_of(match):
    return (match.team1_id, match.team2_id)


def _loser_id(match):
    if not match.winner_id or not (match.team1_id and match.team2_id):
        return None
    return match.team2_id if match.winner_id == match.team1_id else match.team1_id


def _order(match):
    return (match.round_number, match.match_number)


def build_structure(tournament, label):
    """Work out the tournament's shape once. `label(team)` gives the display
    label (the facts builders pass their own)."""
    kind = structure_kind(tournament)
    matches = list(tournament.matches.select_related("team1", "team2", "winner"))
    participations = [
        p for p in tournament.team_participations.select_related("team")
        if p.status in ("active", "withdrawn")
    ]
    structure = Structure(
        kind=kind,
        phase=_phase(tournament, kind, matches),
        stages=_stage_labels(tournament, matches, matches),
        tiebreakers=[TIEBREAKER_WORDS.get(t, t) for t in tournament.get_tiebreaker_order()],
    )
    for p in participations:
        structure.teams[p.team_id] = TeamState(
            team_id=p.team_id, label=label(p.team),
            group=p.group if kind == KIND_GROUPS else "",
            withdrawn=p.status == "withdrawn",
        )

    def labelled(rows):
        for row in rows:
            row["display_label"] = label(row["team"])
        return rows

    if kind == KIND_LEAGUE:
        structure.table = separated_by(tournament, labelled(calculate_standings(tournament)))
        _league_states(tournament, structure)
    elif kind == KIND_GROUPS:
        structure.advance_per_group = tournament.teams_per_group_advance
        letters = sorted({p.group for p in participations if p.group})
        structure.groups = {
            g: separated_by(tournament, labelled(calculate_standings(tournament, group=g)), group=g)
            for g in letters
        }
        _group_states(tournament, structure, matches)
        knockout = [m for m in matches if not m.group]
        seeded = {pk for m in knockout for pk in _teams_of(m) if pk}
        if seeded:
            for pk, state in structure.teams.items():
                if pk not in seeded and not state.withdrawn:
                    state.status = "out_in_groups"
            _knockout_states(tournament, structure, knockout, seeded)
    elif tournament.format == "double_elimination":
        _double_elimination_states(tournament, structure, matches)
    else:
        _knockout_states(tournament, structure, matches, set(structure.teams))
        if tournament.format == "consolation":
            _consolation_states(tournament, structure, matches)

    for state in structure.teams.values():
        if state.withdrawn:
            state.status, state.detail = "withdrawn", ""
    for rows in [structure.table, *structure.groups.values()]:
        for row in rows:
            state = structure.teams.get(row["team"].pk)
            row["status"] = state.status if state else ""
    structure.placings = _placings(structure, matches)
    return structure


def _phase(tournament, kind, matches):
    if tournament.status == "completed":
        return "finished"
    if kind == KIND_LEAGUE:
        return "league"
    if kind == KIND_GROUPS:
        started = any(not m.group and m.bracket_type == "winners" and (m.team1_id or m.team2_id) for m in matches)
        return "knockout" if started else "group_stage"
    return "knockout"


def _league_states(tournament, structure):
    finished = tournament.status == "completed"
    position = 0
    for row in structure.table:
        state = structure.teams.get(row["team"].pk)
        if state is None or state.withdrawn:
            continue
        position += 1
        if not finished:
            state.status = "in_league"
        elif position <= 3:
            state.status = ("champion", "runner_up", "third")[position - 1]
        else:
            state.status, state.detail = "placed", ordinal(position)


def _group_states(tournament, structure, matches):
    """ST-4 rule 4: through / out / in contention, from the group alone.

    "Through" and "out" are certain, whatever the remaining results: ties
    count against the team, because tiebreakers aren't predicted.
    """
    most = max(tournament.points_per_win, tournament.points_per_draw, tournament.points_per_loss, 0)
    for letter, rows in structure.groups.items():
        group_matches = [m for m in matches if m.group == letter]
        rivals = [row for row in rows if not row["withdrawn"]]
        places = min(structure.advance_per_group or 0, len(rivals))
        if all(m.status in GROUP_MATCH_OVER for m in group_matches):
            for i, row in enumerate(rivals):
                structure.teams[row["team"].pk].status = "through" if i < places else "out_in_groups"
            continue
        left = {row["team"].pk: 0 for row in rivals}
        for m in group_matches:
            if m.status in UNFINISHED:
                for pk in _teams_of(m):
                    if pk in left:
                        left[pk] += 1
        best = {row["team"].pk: row["points"] + left[row["team"].pk] * most for row in rivals}
        for row in rivals:
            pk, points = row["team"].pk, row["points"]
            others = [r for r in rivals if r["team"].pk != pk]
            can_reach = sum(1 for r in others if best[r["team"].pk] >= points)
            out_of_reach = sum(1 for r in others if r["points"] > best[pk])
            if can_reach < places:
                status = "through"
            elif out_of_reach >= places:
                status = "out_in_groups"
            else:
                status = "in_contention"
            structure.teams[pk].status = status


def _knockout_states(tournament, structure, matches, candidates):
    """ST-4 rule 6: the main draw of a knockout (or a hybrid's knockout)."""
    main = sorted((m for m in matches if m.bracket_type == "winners"), key=_order)
    third = next((m for m in matches if m.bracket_type == "third_place"), None)
    final = next((m for m in reversed(main) if m.next_match_id is None), None)
    for pk in candidates:
        state = structure.teams.get(pk)
        if state is None or state.withdrawn:
            continue
        lost = next((m for m in main if m.status in DONE and _loser_id(m) == pk), None)
        if lost is None:
            if (final is not None and final.status in DONE and final.winner_id == pk) or (
                tournament.status == "completed" and tournament.champion_id == pk
            ):
                state.status, state.detail = "champion", ""
                continue
            upcoming = next((m for m in main if pk in _teams_of(m) and m.status not in DONE + ("bye", "cancelled")), None)
            state.status = "alive"
            state.detail = structure.stages.get(upcoming.pk, "") if upcoming else ""
            continue
        if lost is final:
            state.status, state.detail = "runner_up", ""
        elif third is not None and pk in _teams_of(third):
            if third.status in DONE:
                state.status, state.detail = ("third", "") if third.winner_id == pk else ("out", "Third-place match")
            else:
                state.status, state.detail = "playing_for_third", ""
        else:
            state.status, state.detail = "out", structure.stages.get(lost.pk, "")


def _consolation_states(tournament, structure, matches):
    """ST-4 rule 8: first-round losers carry on in the consolation bracket."""
    main = [m for m in matches if m.bracket_type == "winners"]
    first_round = min((m.round_number for m in main), default=None)
    consolation = sorted((m for m in matches if m.bracket_type == "consolation"), key=_order)
    cons_final = next((m for m in reversed(consolation) if m.next_match_id is None), None)
    for m in main:
        if m.round_number != first_round or m.status not in DONE:
            continue
        pk = _loser_id(m)
        state = structure.teams.get(pk)
        if state is None or state.withdrawn:
            continue
        if cons_final is not None and cons_final.status in DONE and cons_final.winner_id == pk:
            state.status, state.detail = "consolation_winner", ""
            continue
        lost = next((c for c in consolation if c.status in DONE and _loser_id(c) == pk), None)
        if lost is not None:
            state.status, state.detail = "out", structure.stages.get(lost.pk, "")
            continue
        upcoming = next((c for c in consolation if pk in _teams_of(c) and c.status not in DONE), None)
        if consolation and upcoming is None:
            continue    # a round-1 loser the consolation bracket left out
        state.status = "in_consolation"
        state.detail = structure.stages.get(upcoming.pk, "") if upcoming else ""


BRACKET_ORDER = {"winners": 0, "losers": 1, "grand_final": 2}


def _double_elimination_states(tournament, structure, matches):
    """ST-4 rule 7: out after two losses (or a grand final lost without a reset)."""
    played = sorted(
        (m for m in matches if m.bracket_type in BRACKET_ORDER and m.status in DONE and m.team1_id and m.team2_id),
        key=lambda m: (BRACKET_ORDER[m.bracket_type], m.round_number, m.match_number),
    )
    live = sorted((m for m in matches if m.bracket_type in BRACKET_ORDER and m.status not in DONE + ("bye", "cancelled")),
                  key=lambda m: (BRACKET_ORDER[m.bracket_type], m.round_number, m.match_number))
    decided_finals = [m for m in played if m.bracket_type == "grand_final"]
    champion = tournament.champion_id if tournament.status == "completed" else None
    for pk, state in structure.teams.items():
        if state.withdrawn:
            continue
        losses = [m for m in played if _loser_id(m) == pk]
        upcoming = next((m for m in live if pk in _teams_of(m)), None)
        detail = structure.stages.get(upcoming.pk, "") if upcoming else ""
        lost_final_for_good = any(m.bracket_type == "grand_final" for m in losses) and not tournament.enable_bracket_reset
        if champion == pk:
            state.status, state.detail = "champion", ""
        elif champion and decided_finals and _loser_id(decided_finals[-1]) == pk:
            state.status, state.detail = "runner_up", ""
        elif len(losses) >= 2 or lost_final_for_good:
            state.status, state.detail = "out", structure.stages.get(losses[-1].pk, "")
        elif losses:
            state.status, state.detail = "one_life_left", detail
        else:
            state.status, state.detail = "unbeaten", detail
    if champion:
        losers = [m for m in played if m.bracket_type == "losers"]
        if losers:
            last = max(losers, key=_order)
            third = structure.teams.get(_loser_id(last))
            if third is not None and third.status == "out":
                third.status, third.detail = "third", ""


def _placings(structure, matches):
    placings = {}
    for name in ("champion", "runner_up", "third"):
        state = next((s for s in structure.teams.values() if s.status == name), None)
        if state is not None:
            placings[name] = state.label
    if structure.kind != KIND_LEAGUE and not any(m.bracket_type == "third_place" for m in matches):
        # No third-place match: both beaten semi-finalists share third.
        semis = sorted(s.label for s in structure.teams.values() if s.status == "out" and s.detail == "Semi-final")
        if semis:
            placings["semi_finalists"] = semis
    return placings


# -- Why tied teams are in that order (ST-5) ------------------------------------

def separated_by(tournament, rows, group=None):
    """Set row["separated_by"] on each row level on points with the row
    above: the tiebreaker that put it lower, in words. Mirrors
    standings.rank_standings: the scalar tiebreakers in the configured
    order, then head-to-head among the teams still tied, then the fixed
    last-resort order ("registration order"). Returns `rows`."""
    order = tournament.get_tiebreaker_order()
    scalars = [tb for tb in order if tb != "head_to_head"]
    matches = None

    def scalar_key(row):
        return tuple(row[tb] for tb in scalars if tb in row)

    for i in range(1, len(rows)):
        above, row = rows[i - 1], rows[i]
        if row["points"] != above["points"]:
            continue
        reason = next((TIEBREAKER_WORDS[tb] for tb in scalars if tb in row and row[tb] != above[tb]), None)
        if reason is None and "head_to_head" in order:
            tied = [r["team"].pk for r in rows
                    if r["points"] == row["points"] and scalar_key(r) == scalar_key(row)]
            if matches is None:
                matches = _head_to_head_matches(tournament, group=group)
            h2h = _head_to_head_points(tournament, tied, group=group, matches=matches)
            if h2h.get(above["team"].pk) != h2h.get(row["team"].pk):
                reason = TIEBREAKER_WORDS["head_to_head"]
        row["separated_by"] = reason or "registration order"
    return rows
