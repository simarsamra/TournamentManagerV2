"""The whole tournament in one facts document, for conversational answers.

A small tournament's table, results and fixtures fit in a few thousand
tokens, so instead of routing a question to one card the model can see the
full picture and answer anything about it, follow-ups included. The same
rules as facts.py apply (§1.3): display labels only, no notes, users,
availability or player names, and every number comes from this code.

Numbers people usually ask for (points gaps, games left, streaks, scores
for and against) are computed here, because the model is told not to do
arithmetic and the number check hides nothing it can trace back to these.

build_snapshot returns None when the tournament is too big even after
trimming; the pipeline then falls back to routing (AI-5).
"""
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone

from core import analytics
from core.standings import calculate_standings
from core.views.helpers import _team_display_label

from core.structure import KIND_GROUPS, KIND_LEAGUE, build_structure

from . import structure_facts
from .facts import serialise

MAX_SNAPSHOT_CHARS = 14000
FINISHED = ("confirmed", "forfeited")
TO_PLAY = ("upcoming", "in_progress")
AWAITING = ("pending_confirmation", "disputed")
UPCOMING = TO_PLAY + AWAITING
MIN_RESULTS_KEPT = 10


def _when(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "not scheduled"


def _streak(results):
    """"W3" for three wins in a row, from results newest first."""
    if not results:
        return ""
    count = 0
    for result in results:
        if result != results[0]:
            break
        count += 1
    return f"{results[0]}{count}"


def _played_day(match):
    """The day a finished match was played (the news board's rule), or None."""
    times = [t for t in (match.scheduled_time, match.score_submitted_at) if t]
    return timezone.localtime(min(times)).strftime("%Y-%m-%d") if times else None


def build_snapshot(tournament, user):
    """Return the tournament's facts document, or None if it can't fit.

    Raises PermissionDenied if `user` can't view the tournament's analytics.
    """
    allowed, _ = analytics.can_view_analytics(user, tournament)
    if not allowed:
        raise PermissionDenied("No access to this tournament's analytics.")

    standings = calculate_standings(tournament)
    label_map = analytics.label_standings(tournament, standings)
    active = analytics.active_teams(tournament, label_map)

    def label(team):
        if team is None:
            return "to be decided"
        return label_map.get(team.pk) or _team_display_label(tournament, team)

    structure = build_structure(tournament, label)
    team_ids = set(structure.teams)

    matches = list(
        tournament.matches.filter(Q(status__in=FINISHED) | Q(status__in=UPCOMING))
        .select_related("team1", "team2", "winner", "court")
        .order_by("match_number")
    )
    finished = [m for m in matches if m.status in FINISHED]
    # Played, score not yet confirmed: neither a result nor still to play (S-1).
    awaiting = [m for m in matches if m.status in AWAITING]
    upcoming = [m for m in matches if m.status in TO_PLAY]
    known = [m for m in upcoming if m.team1_id or m.team2_id]
    undecided = [m for m in upcoming if not (m.team1_id or m.team2_id)]

    # Per-team extras from the finished matches, oldest first.
    extra = {pk: {"for": 0, "against": 0, "results": []} for pk in team_ids}
    for m in finished:
        for team, own, other in ((m.team1, m.score_team1, m.score_team2),
                                 (m.team2, m.score_team2, m.score_team1)):
            if team is None or team.pk not in extra:
                continue
            row = extra[team.pk]
            if own is not None and other is not None:
                row["for"] += own
                row["against"] += other
            row["results"].append("W" if m.winner_id == team.pk else "L" if m.winner_id else "D")
    left = {pk: 0 for pk in team_ids}
    group_left = {pk: 0 for pk in team_ids}
    for m in known:
        for pk in (m.team1_id, m.team2_id):
            if pk in left:
                left[pk] += 1
                if m.group:
                    group_left[pk] += 1

    def team_extras(pk):
        row = extra[pk]
        newest_first = row["results"][::-1]
        last5 = newest_first[:5]
        return {
            "score_for": row["for"], "score_against": row["against"],
            "streak": _streak(newest_first),
            "last_5": "".join(reversed(last5)),
            "last_5_wins": last5.count("W"),
            "matches_left": left[pk],
        }

    facts = {
        "tournament": {
            "name": tournament.name,
            "format": tournament.get_format_display(),
            "status": tournament.get_status_display(),
            "today": timezone.localtime().strftime("%Y-%m-%d"),
            "points_for": {
                "win": tournament.points_per_win,
                "draw": tournament.points_per_draw,
                "loss": tournament.points_per_loss,
            },
            "matches_played": len(finished),
            "matches_left": sum(1 for m in known if m.team1_id and m.team2_id),
            "phase": structure_facts.PHASE_WORDS[structure.phase],
            **structure_facts.tournament_facts(tournament, structure),
        },
    }
    if structure.placings:
        facts["tournament"]["placings"] = dict(structure.placings)

    def table_rows(rows, through_places=None, group=False):
        shown = [r for r in rows if r["team"].pk in team_ids]
        contenders = [r for r in shown if not r.get("withdrawn")]
        leader_points = contenders[0]["points"] if contenders else 0
        last_through = (contenders[through_places - 1]["points"]
                        if through_places and len(contenders) >= through_places else None)
        table = []
        for i, row in enumerate(shown):
            pk = row["team"].pk
            entry = structure_facts.table_row(structure, row)
            behind = "points_behind_group_leader" if group else "points_behind_leader"
            entry[behind] = leader_points - row["points"]
            if last_through is not None:
                entry["points_behind_last_place_through"] = max(0, last_through - row["points"])
            if i + 1 < len(shown):
                entry["points_ahead_of_next"] = row["points"] - shown[i + 1]["points"]
            entry.update(team_extras(pk))
            if group:
                entry["group_matches_left"] = group_left[pk]
                # "Can they still catch ...?" within the group, without sums.
                entry["max_possible_group_points"] = row["points"] + group_left[pk] * tournament.points_per_win
            else:
                entry["max_possible_points"] = row["points"] + entry["matches_left"] * tournament.points_per_win
            table.append(entry)
        return table

    if structure.kind == KIND_LEAGUE:
        facts["table"] = table_rows(structure.table)
    elif structure.kind == KIND_GROUPS:
        advance = structure.advance_per_group or 0
        facts["groups"] = [
            {"group": letter, "advance": advance, "table": table_rows(rows, advance, group=True)}
            for letter, rows in structure.groups.items()
        ]
        if structure.phase in ("knockout", "finished"):
            facts["bracket"] = structure_facts.bracket_facts(structure)
    else:
        stats, _ = analytics.team_performance(tournament, standings, active)
        facts["teams"] = [
            {
                "team": s["display_label"], "status": structure_facts.status_of(structure, s["team"].pk),
                "played": s["played"], "wins": s["wins"], "draws": s["draws"], "losses": s["losses"],
                **team_extras(s["team"].pk),
            }
            for s in stats
        ]
        facts["bracket"] = structure_facts.bracket_facts(structure)
    withdrawn = sorted(st.label for st in structure.teams.values() if st.withdrawn)
    if withdrawn:
        facts["withdrawn"] = withdrawn

    def stage(match):
        return structure.stages.get(match.pk, "")

    results = []
    for m in finished:
        row = {"match": m.match_number, "stage": stage(m), "date": _played_day(m),
               "team1": label(m.team1), "team2": label(m.team2)}
        if m.status == "forfeited":
            row["forfeit_won_by"] = label(m.winner) if m.winner_id else "nobody"
        else:
            row.update(score1=m.score_team1, score2=m.score_team2,
                       winner=label(m.winner) if m.winner_id else "draw")
            if m.score_team1 is not None and m.score_team2 is not None:
                row["margin"] = abs(m.score_team1 - m.score_team2)
        results.append(row)
    facts["results"] = results

    facts["fixtures"] = [
        {"match": m.match_number, "stage": stage(m), "when": _when(m.scheduled_time),
         "court": m.court.name if m.court else "", "team1": label(m.team1), "team2": label(m.team2),
         "status": m.get_status_display()}
        for m in known
    ]
    if undecided:
        counts = {}
        for m in undecided:
            counts[stage(m)] = counts.get(stage(m), 0) + 1
        facts["later_matches_to_be_decided"] = [{"stage": k, "matches": v} for k, v in counts.items()]
    if awaiting:
        facts["awaiting_confirmation"] = [
            {"match": m.match_number, "stage": stage(m), "team1": label(m.team1), "team2": label(m.team2),
             "status": m.get_status_display()}
            for m in awaiting
        ]

    pairs = {}
    for m in finished:
        if m.team1 is None or m.team2 is None:
            continue
        a, b = sorted((m.team1, m.team2), key=lambda t: label(t).lower())
        rec = pairs.setdefault((a.pk, b.pk), {"teams": [label(a), label(b)], "wins": [0, 0], "draws": 0})
        if m.winner_id == a.pk:
            rec["wins"][0] += 1
        elif m.winner_id == b.pk:
            rec["wins"][1] += 1
        else:
            rec["draws"] += 1
    facts["head_to_head"] = [
        {"teams": r["teams"], "team1_wins": r["wins"][0], "team2_wins": r["wins"][1], "draws": r["draws"]}
        for r in pairs.values()
    ]

    return _fit(facts)


def _fit(facts):
    """Drop the oldest results, then the latest fixtures, until the document
    fits MAX_SNAPSHOT_CHARS; None if it still doesn't."""
    dropped = 0
    while len(serialise(facts)) > MAX_SNAPSHOT_CHARS and len(facts["results"]) > MIN_RESULTS_KEPT:
        facts["results"].pop(0)
        dropped += 1
    if dropped:
        facts["older_results_not_listed"] = dropped
    while len(serialise(facts)) > MAX_SNAPSHOT_CHARS and len(facts["fixtures"]) > MIN_RESULTS_KEPT:
        facts["fixtures"].pop()
        facts["later_fixtures_not_listed"] = facts.get("later_fixtures_not_listed", 0) + 1
    if len(serialise(facts)) > MAX_SNAPSHOT_CHARS:
        return None
    return facts
