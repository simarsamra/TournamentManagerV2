"""What the model is allowed to see (AI_ANALYTICS_PLAN.md AI-4, §1.3).

The model never touches the database. It gets a small JSON document of
numbers the analytics code already computed (core/analytics.py), for one
tournament the asker may view:

- names are display labels (A-3), never internal shadow-team names;
- no audit log, notes, availability, usernames, emails or player names;
- only the teams the question names, plus the top of the table;
- at most MAX_FACTS_CHARS once serialised (about 1,500 tokens).
"""
import json
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.utils import timezone

from core import analytics
from core.standings import calculate_standings

MAX_FACTS_CHARS = 6000
TOP_ROWS = 8
INTENTS = (
    "head_to_head", "form", "next_match", "what_if",
    "standings", "team_performance", "unknown",
)
WINDOWS = (3, 5, 8, 10, 15)


@dataclass
class Route:
    """A validated question route: which card, which teams. AI-5 builds these
    from the model's JSON; only active teams of the tournament may appear."""

    intent: str = "unknown"
    team_a: object = None      # Team
    team_b: object = None      # Team
    window: int = 5
    match: object = None       # Match, for what_if
    winner: str = ""           # "team1" | "team2" | "draw", for what_if

    def __post_init__(self):
        if self.intent not in INTENTS:
            raise ValueError(f"Unknown intent {self.intent!r}")
        if self.window not in WINDOWS:
            raise ValueError(f"Window must be one of {WINDOWS}")


def team_keys(active_teams):
    """{"T1": team, "T2": team, ...} in the page's order (by name).

    The model picks teams by these opaque keys, never by typing a name, so
    two teams with similar or identical labels can't be confused.
    """
    return {f"T{i}": team for i, team in enumerate(active_teams, start=1)}


def _standings_rows(rows):
    return [
        {
            "rank": row["rank"], "team": row["display_label"],
            "played": row["played"], "wins": row["wins"], "draws": row["draws"],
            "losses": row["losses"], "points": row["points"], "game_diff": row["game_diff"],
        }
        for row in rows
    ]


def _performance_rows(stats):
    return [
        {
            "team": s["display_label"], "played": s["played"], "wins": s["wins"],
            "draws": s["draws"], "losses": s["losses"], "win_rate_pct": s["win_rate"],
        }
        for s in stats
    ]


def build_facts(tournament, user, route):
    """Return the facts document for `route`, as the model will see it.

    Raises PermissionDenied if `user` can't view the tournament's analytics,
    and ValueError if the route names a team that isn't active in it.
    """
    allowed, _ = analytics.can_view_analytics(user, tournament)
    if not allowed:
        raise PermissionDenied("No access to this tournament's analytics.")

    standings = calculate_standings(tournament)
    label_map = analytics.label_standings(tournament, standings)
    active = analytics.active_teams(tournament, label_map)
    for team in (route.team_a, route.team_b):
        if team is not None and team.pk not in {t.pk for t in active}:
            raise ValueError(f"Team {team.pk} is not active in this tournament.")

    active_labels = {team.pk: team.display_label for team in active}

    def label(team):
        return active_labels[team.pk]

    facts = {
        "tournament": {
            "name": tournament.name,
            "format": tournament.get_format_display(),
            "status": tournament.get_status_display(),
            "points_for": {
                "win": tournament.points_per_win,
                "draw": tournament.points_per_draw,
                "loss": tournament.points_per_loss,
            },
        },
    }
    if tournament.format in analytics.STANDINGS_FORMATS:
        facts["standings_top"] = _standings_rows(standings[:TOP_ROWS])
    else:
        stats, _ = analytics.team_performance(tournament, standings, active)
        facts["results_top"] = _performance_rows(stats[:TOP_ROWS])

    if route.intent == "head_to_head" and route.team_a and route.team_b:
        card = analytics.head_to_head(tournament, route.team_a, route.team_b)
        if card is not None:
            facts["head_to_head"] = {
                "team_a": label(route.team_a), "team_b": label(route.team_b),
                "meetings": card["total_matches"],
                "team_a_wins": card["team1_wins"], "team_b_wins": card["team2_wins"],
                "draws": card["draws"],
                "team_a_avg_score": card["team1_avg_score"],
                "team_b_avg_score": card["team2_avg_score"],
            }

    elif route.intent == "form" and route.team_a:
        rows = analytics.rolling_form(tournament, route.team_a, route.window)
        facts["form"] = {
            "team": label(route.team_a),
            "window": route.window,
            "matches": [{"opponent": r["opponent"], "result": r["result"]} for r in rows],
            "win_rate_pct": rows[-1]["win_rate"] if rows else None,
        }

    elif route.intent == "next_match" and route.team_a:
        prep = analytics.next_opponent_prep(tournament, route.team_a)
        if prep is None:
            facts["next_match"] = {"team": label(route.team_a), "scheduled": False}
        else:
            when = prep["match"].scheduled_time
            facts["next_match"] = {
                "team": label(route.team_a),
                "scheduled": True,
                "opponent": prep["opponent_label"] or "TBD",
                "when": timezone.localtime(when).strftime("%Y-%m-%d %H:%M") if when else "not yet scheduled",
                "opponent_last_5": prep["opponent_record"],
                "head_to_head": prep["h2h"],
            }

    elif route.intent == "what_if" and route.match is not None and route.winner:
        offered, _ = analytics.simulator_matches(tournament)
        if any(m.pk == route.match.pk for m in offered):
            simulated, applied = analytics.simulate(
                tournament, standings, offered, {route.match.pk: route.winner}
            )
            if applied:
                match = next(m for m in offered if m.pk == route.match.pk)
                outcome = {
                    "team1": f"{match.team1_label} beat {match.team2_label}",
                    "team2": f"{match.team2_label} beat {match.team1_label}",
                    "draw": f"{match.team1_label} and {match.team2_label} draw",
                }[route.winner]
                facts["what_if"] = {
                    "assumed_result": outcome,
                    "projected_standings_top": [
                        {**row, "points_change": sim["point_change"]}
                        for row, sim in zip(_standings_rows(simulated[:TOP_ROWS]), simulated[:TOP_ROWS])
                    ],
                }

    return _fit(facts)


def serialise(facts):
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":"))


def _fit(facts):
    """Trim the table until the document fits MAX_FACTS_CHARS. The rows the
    question is about (head-to-head, form, next match) are kept longest."""
    for key in ("standings_top", "results_top"):
        while len(serialise(facts)) > MAX_FACTS_CHARS and len(facts.get(key, [])) > 3:
            facts[key] = facts[key][:-1]
    what_if = facts.get("what_if")
    while what_if and len(serialise(facts)) > MAX_FACTS_CHARS and len(what_if["projected_standings_top"]) > 3:
        what_if["projected_standings_top"] = what_if["projected_standings_top"][:-1]
    if len(serialise(facts)) > MAX_FACTS_CHARS:
        raise ValueError("Facts don't fit the size limit even after trimming.")
    return facts
