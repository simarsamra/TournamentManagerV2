"""The tournament's shape, as the model sees it (AI_STRUCTURE_PLAN.md §1.2).

Every facts builder describes tables, groups and brackets through these
functions, so the model always gets the same words: a league table, one
table per group with how many go through, or a bracket summary, never one
ranked table that mixes teams from different groups. Statuses, stages and
tie reasons all come from core/structure.py; the model only quotes them.
"""
from core.structure import KIND_BRACKET, KIND_GROUPS, KIND_LEAGUE

# Added to every prompt that shows the model a table or a bracket (§1.3).
STRUCTURE_RULE = (
    "Only compare teams in the same group. Use each team's status as given (\"through\", \"out\", "
    "\"in contention\", \"one life left\"); never work it out yourself. In a bracket there is no table: "
    "talk about who is still in and the next round."
)

# T-2, T-3: say scores and competitors in the tournament's own words.
WORDING_RULE = (
    "Scores are counted in tournament.score_unit, and game_diff is the difference in them: say \"goal "
    "difference\" only when score_unit is goals. The competitors are tournament.participant (a team, or a "
    "single player): don't call a player a team."
)
PROMPT_RULES = STRUCTURE_RULE + "\n" + WORDING_RULE

SCORE_UNITS = {
    "badminton": "games", "tennis": "sets", "table_tennis": "games", "volleyball": "sets",
    "soccer": "goals", "basketball": "points", "cricket": "runs", "other": "points",
}

PHASE_WORDS = {"league": "league", "group_stage": "group stage", "knockout": "knockout", "finished": "finished"}
STILL_IN = ("alive", "unbeaten", "one_life_left")


def status_of(structure, team_id):
    """The status text for a team, or "" when there's nothing to say (a
    league that's still running)."""
    state = structure.teams.get(team_id)
    if state is None or state.status == "in_league":
        return ""
    return state.text


def table_row(structure, row):
    out = {
        "rank": row["rank"], "team": row["display_label"],
        "played": row["played"], "wins": row["wins"], "draws": row["draws"],
        "losses": row["losses"], "points": row["points"], "game_diff": row["game_diff"],
    }
    status = status_of(structure, row["team"].pk)
    if status:
        out["status"] = status
    if row.get("separated_by"):
        out["separated_by"] = row["separated_by"]
    if row.get("withdrawn"):
        out["withdrawn"] = True
    return out


def group_of(structure, team_id):
    state = structure.teams.get(team_id)
    return state.group if state else ""


def groups_facts(structure, rows=None, only=None):
    return [
        {"group": letter, "advance": structure.advance_per_group,
         "table": [table_row(structure, row) for row in table[:rows]]}
        for letter, table in structure.groups.items()
        if only is None or letter in only
    ]


def bracket_facts(structure):
    """Who is still in, the next round, who went out where."""
    teams = sorted(structure.teams.values(), key=lambda s: s.label.lower())
    still_in = [{"team": s.label, "status": s.text} for s in teams if s.status in STILL_IN]
    facts = {"still_in": still_in}
    next_stages = {s.detail for s in teams if s.status == "alive" and s.detail}
    if len(next_stages) == 1:
        facts["next_round"] = next_stages.pop()
    playing_for_third = [s.label for s in teams if s.status == "playing_for_third"]
    if playing_for_third:
        facts["playing_for_third"] = playing_for_third
    knocked_out = [{"team": s.label, "out_in": s.detail} for s in teams if s.status == "out"]
    if knocked_out:
        facts["knocked_out"] = knocked_out
    consolation = [{"team": s.label, "status": s.text} for s in teams
                   if s.status in ("in_consolation", "consolation_winner")]
    if consolation:
        facts["consolation_bracket"] = consolation
    return facts


def standings_facts(structure, *, rows=None, only_group=None):
    """The fragment that replaces a single ranked table:

    league  -> {"standings_top": [row, ...]}
    groups  -> {"phase", "groups": [{"group", "advance", "table"}], "bracket" once the knockouts start}
    bracket -> {"phase", "bracket": {...}}

    Plus "placings" and "withdrawn" when there are any.
    """
    facts = {}
    if structure.kind == KIND_LEAGUE:
        facts["standings_top"] = [table_row(structure, row) for row in structure.table[:rows]]
    elif structure.kind == KIND_GROUPS:
        facts["phase"] = PHASE_WORDS[structure.phase]
        facts["groups"] = groups_facts(structure, rows, only={only_group} if only_group else None)
        if structure.phase in ("knockout", "finished"):
            facts["bracket"] = bracket_facts(structure)
    elif structure.kind == KIND_BRACKET:
        facts["phase"] = PHASE_WORDS[structure.phase]
        facts["bracket"] = bracket_facts(structure)
    if structure.placings:
        facts["placings"] = dict(structure.placings)
    withdrawn = sorted(s.label for s in structure.teams.values() if s.withdrawn)
    if withdrawn:
        facts["withdrawn"] = withdrawn
    return facts


def wording_facts(tournament):
    """How to talk about this tournament's scores and competitors."""
    return {
        "score_unit": SCORE_UNITS.get(tournament.sport_type, "points"),
        "participant": tournament.participant_label.lower(),
    }


def tournament_facts(tournament, structure):
    """Extra keys for a facts document's "tournament" block."""
    return {"kind": structure.kind, "tiebreakers": structure.tiebreakers, **wording_facts(tournament)}


def team_facts(structure, team_id):
    """What a team's own story needs: its status, and its group's table."""
    state = structure.teams.get(team_id)
    if state is None:
        return {}
    facts = {}
    status = status_of(structure, team_id)
    if status:
        facts["your_status"] = status
    if structure.kind == KIND_GROUPS and state.group:
        facts["your_group"] = state.group
        facts["your_group_advance"] = structure.advance_per_group
    return facts


def trim(facts, limit, size, keep_groups=()):
    """Shrink group tables and the bracket's knocked-out list until
    size(facts) <= limit. Never below advance + 1 rows (who goes through
    must stay visible), never the groups in `keep_groups`."""
    for group in sorted(facts.get("groups", []), key=lambda g: -len(g["table"])):
        if group["group"] in keep_groups:
            continue
        floor = (group.get("advance") or 0) + 1
        while size(facts) > limit and len(group["table"]) > max(floor, 1):
            group["table"].pop()
    bracket = facts.get("bracket") or {}
    while size(facts) > limit and bracket.get("knocked_out"):
        bracket["knocked_out"].pop(0)
    return facts
