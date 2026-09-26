"""Realistic tournaments for tests (AI_STRUCTURE_PLAN.md ST-0).

Built with the real fixture generator, and every result goes through the
production score-lock path (`_lock_match_score`), so brackets advance,
third-place matches fill, consolation brackets appear, hybrid knockouts are
seeded and tournaments complete exactly as they do on the site. Nothing here
re-implements any of those steps.

A plain module rather than a test module so several test files can share it
(core/ai/testing.py is the precedent). Nothing outside tests imports it.
"""
from django.utils import timezone

from .models import Team, TeamTournamentParticipation, Tournament
from .scheduling import generate_fixtures

NAMES = [
    "Red Rovers", "Golden Boots", "Blue Jays", "Green Giants",
    "Silver Hawks", "Purple Pumas", "Orange Owls", "Black Bears",
]
# Red Rovers strongest, Black Bears weakest.
STRENGTH = {name: len(NAMES) - i for i, name in enumerate(NAMES)}


def _make(organizer, fmt, names, name=None, **fields):
    tournament = Tournament.objects.create(
        name=name or f"Test {fmt}", format=fmt, status="active", created_by=organizer,
        sport_type="soccer", **fields,
    )
    for team_name in names:
        team, _ = Team.objects.get_or_create(name=team_name)
        TeamTournamentParticipation.objects.create(team=team, tournament=tournament, status="active")
    generate_fixtures(tournament)
    return tournament


def make_league(organizer, names=NAMES, double=False, **fields):
    return _make(organizer, "double_round_robin" if double else "round_robin", names, **fields)


def make_hybrid(organizer, names=NAMES, groups=2, advance=2, third_place=False, **fields):
    return _make(organizer, "hybrid", names, num_groups=groups, teams_per_group_advance=advance,
                 enable_third_place_match=third_place, **fields)


def make_knockout(organizer, names=NAMES, third_place=False, **fields):
    return _make(organizer, "knockout", names, enable_third_place_match=third_place, **fields)


def make_double_elimination(organizer, names=NAMES, reset=True, **fields):
    return _make(organizer, "double_elimination", names, enable_bracket_reset=reset, **fields)


def make_consolation(organizer, names=NAMES, **fields):
    return _make(organizer, "consolation", names, **fields)


def play(match, s1, s2):
    """Record a result through the production lock path."""
    from .views.helpers import _lock_match_score

    match.refresh_from_db()
    match.tournament.refresh_from_db()
    if match.team1_id is None or match.team2_id is None:
        raise AssertionError(f"Match {match.match_number} doesn't have both teams yet")
    match.score_team1, match.score_team2 = s1, s2
    match.score_submitted_at = timezone.now()
    match.save()
    if _lock_match_score(match) is False:
        raise AssertionError(f"Match {match.match_number} wasn't accepted ({s1}-{s2})")
    match.refresh_from_db()
    return match


def forfeit(match, winner):
    match.refresh_from_db()
    match.status = "forfeited"
    match.winner = winner
    match.save()
    return match


def _stronger_first(match, upset=()):
    one, two = match.team1.name, match.team2.name
    first_wins = STRENGTH.get(one, 0) >= STRENGTH.get(two, 0)
    if one in upset or two in upset:
        first_wins = one in upset
    return first_wins


def play_group_stage(tournament, upset=()):
    """The stronger team wins every group match 2-0, in match order."""
    for match in tournament.matches.exclude(group="").order_by("match_number"):
        match.refresh_from_db()
        if match.status != "upcoming":
            continue
        play(match, *((2, 0) if _stronger_first(match, upset) else (0, 2)))
    tournament.refresh_from_db()
    return tournament


def ready(tournament, bracket_type, round_number=None):
    """Upcoming matches of a bracket (and round) whose two teams are known."""
    qs = tournament.matches.filter(
        bracket_type=bracket_type, status="upcoming", team1__isnull=False, team2__isnull=False,
    )
    if tournament.format == "hybrid":
        qs = qs.filter(group="")
    if round_number is not None:
        qs = qs.filter(round_number=round_number)
    return list(qs.select_related("team1", "team2").order_by("round_number", "match_number"))


def play_ready(tournament, bracket_type="winners", upset=(), score=(2, 1)):
    """Play every ready match of the bracket's earliest ready round; the
    stronger team wins unless a team named in `upset` is playing."""
    matches = ready(tournament, bracket_type)
    if not matches:
        return []
    first_round = matches[0].round_number
    played = []
    for match in (m for m in matches if m.round_number == first_round):
        high, low = score
        played.append(play(match, *((high, low) if _stronger_first(match, upset) else (low, high))))
    tournament.refresh_from_db()
    return played


def withdraw(tournament, team, policy=None):
    """Withdraw `team` through the real withdrawal code."""
    from .withdrawals import handle_withdrawal

    if policy is not None and tournament.withdrawal_policy != policy:
        tournament.withdrawal_policy = policy
        tournament.save(update_fields=["withdrawal_policy"])
    handle_withdrawal(None, team, tournament)
    tournament.refresh_from_db()


def team(name):
    return Team.objects.get(name=name)


# -- Named scenarios (AI_STRUCTURE_PLAN.md ST-0) --------------------------------

def hybrid_after_groups(organizer, **fields):
    """Group A: Red Rovers 9, Green Giants 6, Silver Hawks 3, Black Bears 0.
    Group B: Golden Boots 9, Blue Jays 6, Purple Pumas 3, Orange Owls 0.
    Semi-finals: Red Rovers v Blue Jays, Green Giants v Golden Boots."""
    return play_group_stage(make_hybrid(organizer, name="Hybrid", **fields))


def hybrid_after_one_semi(organizer, **fields):
    """hybrid_after_groups, then Red Rovers beat Blue Jays 3-1."""
    tournament = hybrid_after_groups(organizer, **fields)
    semi = next(m for m in ready(tournament, "winners") if m.team1.name == "Red Rovers")
    play(semi, 3, 1)
    return tournament


def hybrid_finished(organizer, **fields):
    """hybrid_after_one_semi, then Green Giants beat Golden Boots 2-1 (an
    upset), then Red Rovers beat Green Giants 2-1 in the final."""
    tournament = hybrid_after_one_semi(organizer, **fields)
    semi = next(m for m in ready(tournament, "winners") if "Green Giants" in (m.team1.name, m.team2.name))
    play(semi, *((2, 1) if semi.team1.name == "Green Giants" else (1, 2)))
    final = ready(tournament, "winners")[0]
    play(final, *((2, 1) if final.team1.name == "Red Rovers" else (1, 2)))
    tournament.refresh_from_db()
    return tournament


def knockout_after_round_1(organizer, **fields):
    """8 teams; the stronger team wins every quarter-final 2-1."""
    tournament = make_knockout(organizer, name="Knockout", **fields)
    play_ready(tournament, "winners")
    return tournament


def league_with_withdrawal(organizer, **fields):
    """6 teams (the first six NAMES); the first 6 matches go to team1 1-0,
    then Blue Jays withdraw with the "void" policy."""
    tournament = make_league(organizer, NAMES[:6], name="League", **fields)
    for match in tournament.matches.order_by("match_number")[:6]:
        play(match, 1, 0)
    withdraw(tournament, team("Blue Jays"), policy="void")
    return tournament
