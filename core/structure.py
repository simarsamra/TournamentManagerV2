"""A tournament's shape: groups, knockout stages, bracket sides, and where
each team stands in it (AI_STRUCTURE_PLAN.md §1.1).

Plain functions of model objects, like core/analytics.py: no HTTP, nothing
rendered. The AI facts builders read these so the model is told what a
match was ("Semi-final", "Group B") and what state a team is in, instead of
being handed one league table for every format and left to guess.
"""
from collections import defaultdict

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
    wanted = everything if matches is None else list(matches)
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
