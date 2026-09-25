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

from .facts import serialise

MAX_SNAPSHOT_CHARS = 14000
FINISHED = ("confirmed", "forfeited")
UPCOMING = ("upcoming", "in_progress", "pending_confirmation", "disputed")
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
    active_ids = {team.pk for team in active}

    def label(team):
        if team is None:
            return "TBD"
        return label_map.get(team.pk) or _team_display_label(tournament, team)

    matches = list(
        tournament.matches.filter(Q(status__in=FINISHED) | Q(status__in=UPCOMING))
        .select_related("team1", "team2", "winner", "court")
        .order_by("match_number")
    )
    finished = [m for m in matches if m.status in FINISHED]
    upcoming = [m for m in matches if m.status in UPCOMING]

    # Per-team extras from the finished matches, oldest first.
    extra = {pk: {"for": 0, "against": 0, "results": []} for pk in active_ids}
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
    left = {pk: 0 for pk in active_ids}
    for m in upcoming:
        for team in (m.team1, m.team2):
            if team is not None and team.pk in left:
                left[team.pk] += 1

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
            "matches_left": len(upcoming),
        },
    }

    if tournament.format in analytics.STANDINGS_FORMATS:
        rows = [row for row in standings if row["team"].pk in active_ids]
        leader_points = rows[0]["points"] if rows else 0
        table = []
        for i, row in enumerate(rows):
            entry = {
                "rank": row["rank"], "team": row["display_label"],
                "played": row["played"], "wins": row["wins"], "draws": row["draws"],
                "losses": row["losses"], "points": row["points"], "game_diff": row["game_diff"],
                "points_behind_leader": leader_points - row["points"],
            }
            if i + 1 < len(rows):
                entry["points_ahead_of_next"] = row["points"] - rows[i + 1]["points"]
            entry.update(team_extras(row["team"].pk))
            # "Can they still catch ...?" without the model doing sums.
            entry["max_possible_points"] = row["points"] + entry["matches_left"] * tournament.points_per_win
            table.append(entry)
        facts["table"] = table
    else:
        stats, _ = analytics.team_performance(tournament, standings, active)
        facts["teams"] = [
            {
                "team": s["display_label"], "played": s["played"], "wins": s["wins"],
                "draws": s["draws"], "losses": s["losses"], "win_rate_pct": s["win_rate"],
                **team_extras(s["team"].pk),
            }
            for s in stats
        ]

    results = []
    for m in finished:
        row = {"match": m.match_number, "round": m.round_number,
               "date": _when(m.scheduled_time)[:10], "team1": label(m.team1), "team2": label(m.team2)}
        if m.group:
            row["group"] = m.group
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
        {"match": m.match_number, "round": m.round_number, "when": _when(m.scheduled_time),
         "court": m.court.name if m.court else "", "team1": label(m.team1), "team2": label(m.team2),
         "status": m.get_status_display()}
        for m in upcoming
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
