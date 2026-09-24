"""Test Maker: a development-only data generator. Disabled outside DEBUG."""
import random
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone

from ..models import (
    Player,
    Team,
    TeamMembership,
    TeamTournamentCourtPreference,
    TeamTournamentParticipation,
    TournamentIndividualRegistration,
)
from ..standings import advance_loser_to_third_place, advance_winner
from ..audit import log_action
from ..services.enrollment import active_participant_count

from .helpers import (
    DEFAULT_DISPUTE_WINDOW_MINUTES,
    _ensure_shadow_team_for_registration,
    _get_tournament,
    _is_site_admin,
    _tournament_context,
)



@login_required
def test_maker_view(request):
    # Test Maker creates real accounts and rewrites live match data. It is a
    # development tool, not an organizer feature.
    if not getattr(settings, "ENABLE_TEST_MAKER", False):
        raise Http404
    if not _is_site_admin(request.user):
        messages.error(request, "Test Maker is restricted to site administrators.")
        return redirect("dashboard")

    tournament = _get_tournament(request)
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        actions_without_tournament = {"create_user_team_pool"}
        if not tournament and action not in actions_without_tournament:
            messages.error(request, "No tournament selected. Create/select a tournament first.")
            return redirect("test_maker")

        def _next_unique_username(base_username):
            if not User.objects.filter(username=base_username).exists():
                return base_username
            suffix = 1
            while User.objects.filter(username=f"{base_username}_{suffix}").exists():
                suffix += 1
            return f"{base_username}_{suffix}"

        def _next_unique_display_name(base_name, tournament_obj):
            if not tournament_obj.individual_registrations.filter(display_name__iexact=base_name).exists():
                return base_name
            suffix = 1
            while tournament_obj.individual_registrations.filter(
                display_name__iexact=f"{base_name}_{suffix}"
            ).exists():
                suffix += 1
            return f"{base_name}_{suffix}"

        if action == "create_user_team_pool":
            user_count_raw = request.POST.get("pool_user_count", "50")
            team_count_raw = request.POST.get("pool_team_count", "25")
            members_raw = request.POST.get("pool_members_per_team", "2")
            user_prefix = (request.POST.get("pool_user_prefix") or "tm_user_").strip() or "tm_user_"
            team_prefix = (request.POST.get("pool_team_prefix") or "tm_team_").strip() or "tm_team_"
            pool_password = request.POST.get("pool_password") or "pass123"

            try:
                user_count = max(1, int(user_count_raw))
                team_count = max(0, int(team_count_raw))
                members_per_team = max(1, int(members_raw))
            except ValueError:
                messages.error(request, "Pool counts and members per team must be valid numbers.")
                return redirect("test_maker")

            created_users = 0
            reused_users = 0
            created_teams = 0
            created_memberships = 0

            pool_users = []
            user_width = max(3, len(str(max(user_count, team_count * members_per_team))))
            for idx in range(1, user_count + 1):
                username = f"{user_prefix}{idx:0{user_width}d}"
                user = User.objects.filter(username=username).first()
                if user is None:
                    user = User.objects.create_user(
                        username=username,
                        password=pool_password,
                        first_name=f"U{idx:0{user_width}d}",
                    )
                    created_users += 1
                else:
                    reused_users += 1
                pool_users.append(user)

            required_members = team_count * members_per_team
            next_user_idx = len(pool_users) + 1
            while len(pool_users) < required_members:
                username = _next_unique_username(f"{user_prefix}{next_user_idx:0{user_width}d}")
                user = User.objects.create_user(
                    username=username,
                    password=pool_password,
                    first_name=f"U{next_user_idx:0{user_width}d}",
                )
                created_users += 1
                pool_users.append(user)
                next_user_idx += 1

            if tournament:
                sport_type = tournament.sport_type
            else:
                sport_type = "football"

            team_width = max(3, len(str(max(1, team_count))))
            for idx in range(1, team_count + 1):
                team_name = f"{team_prefix}{idx:0{team_width}d}"
                team, team_created = Team.objects.get_or_create(
                    name=team_name,
                    defaults={"sport_type": sport_type, "is_internal": False},
                )
                if team_created:
                    created_teams += 1

                start = (idx - 1) * members_per_team
                members = pool_users[start:start + members_per_team]
                for member_idx, user in enumerate(members, start=1):
                    role = "captain" if member_idx == 1 else "member"
                    membership, membership_created = TeamMembership.objects.get_or_create(
                        team=team,
                        user=user,
                        defaults={"role": role},
                    )
                    if not membership_created and member_idx == 1 and membership.role != "captain":
                        membership.role = "captain"
                        membership.save(update_fields=["role"])
                    if membership_created:
                        created_memberships += 1
                    Player.objects.get_or_create(team=team, name=user.username)

            summary = (
                f"Pool ready: {created_users} user(s) created, {reused_users} reused, "
                f"{created_teams} team(s) created, {created_memberships} membership(s) created."
            )
            log_action(
                request,
                "test_maker_create_user_team_pool",
                summary,
                tournament=tournament,
            )
            messages.success(request, summary)

        elif action == "create_test_teams":
            team_count_raw = request.POST.get("team_count", "10")
            members_raw = request.POST.get("members_per_team") or str(tournament.players_per_team or 2)
            team_prefix = (request.POST.get("team_prefix") or "team").strip() or "team"
            username_prefix = (request.POST.get("username_prefix") or "t").strip() or "t"
            default_password = request.POST.get("default_password") or "pass123"

            try:
                team_count = max(1, int(team_count_raw))
                members_per_team = max(1, int(members_raw))
            except ValueError:
                messages.error(request, "Team count and members per team must be valid numbers.")
                return redirect("test_maker")

            if tournament.registration_mode == "individual":
                created_regs = 0
                created_users = 0
                created_shadow_teams = 0
                for idx in range(1, team_count + 1):
                    display_name = f"{team_prefix}{idx}"[:100]
                    if tournament.individual_registrations.filter(display_name__iexact=display_name).exists():
                        continue
                    captain_username = _next_unique_username(f"{username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=default_password,
                        first_name=display_name,
                    )
                    created_users += 1
                    reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=captain,
                        display_name=display_name,
                        status="active",
                    )
                    created_regs += 1
                    shadow = _ensure_shadow_team_for_registration(reg, tournament.sport_type)
                    if shadow:
                        created_shadow_teams += 1
                log_action(
                    request,
                    "test_maker_create_teams",
                    (
                        f"Individual test data: {created_regs} registration(s), {created_users} user(s), "
                        f"{created_shadow_teams} shadow competitor(s)"
                    ),
                    tournament=tournament,
                )
                messages.success(
                    request,
                    (
                        f"Test participants created: {created_regs} registration(s), {created_users} user(s), "
                        f"{created_shadow_teams} internal competitor(s)."
                    ),
                )
            else:
                created_teams = 0
                created_users = 0
                created_memberships = 0
                created_participations = 0

                for idx in range(1, team_count + 1):
                    team_name = f"{team_prefix}{idx}"
                    if TeamTournamentParticipation.objects.filter(
                        team__name=team_name,
                        tournament=tournament,
                    ).exists():
                        continue

                    captain_username = _next_unique_username(f"{username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=default_password,
                        first_name=team_name,
                    )
                    created_users += 1

                    team, team_created = Team.objects.get_or_create(
                        name=team_name, defaults={"sport_type": tournament.sport_type}
                    )
                    if team_created:
                        created_teams += 1

                    _, participation_created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if participation_created:
                        created_participations += 1

                    TeamMembership.objects.create(team=team, user=captain, role="captain")
                    created_memberships += 1
                    Player.objects.get_or_create(team=team, name=captain_username)

                    for member_idx in range(2, members_per_team + 1):
                        member_username = _next_unique_username(f"{username_prefix}{idx}p{member_idx}")
                        member = User.objects.create_user(
                            username=member_username,
                            password=default_password,
                            first_name=team_name,
                        )
                        created_users += 1
                        TeamMembership.objects.create(team=team, user=member, role="member")
                        created_memberships += 1
                        Player.objects.get_or_create(team=team, name=member_username)

                log_action(
                    request,
                    "test_maker_create_teams",
                    (
                        f"Created {created_teams} team(s), {created_users} user(s), "
                        f"{created_memberships} membership(s), {created_participations} participation(s)"
                    ),
                    tournament=tournament,
                )
                messages.success(
                    request,
                    (
                        f"Test data created: {created_teams} team(s), {created_users} user(s), "
                        f"{created_memberships} membership(s), {created_participations} participation(s)."
                    ),
                )
        elif action == "register_to_open_tournament":
            if tournament.status != "registration_open":
                messages.error(request, "Tournament must have status 'Registration Open' to use this action.")
                return redirect("test_maker")

            reg_count_raw = request.POST.get("reg_count", "5")
            reg_prefix = (request.POST.get("reg_prefix") or ("p" if tournament.registration_mode == "individual" else "rteam")).strip()
            reg_username_prefix = (request.POST.get("reg_username_prefix") or "r").strip() or "r"
            reg_password = request.POST.get("reg_password") or "pass123"

            try:
                reg_count = max(1, int(reg_count_raw))
            except ValueError:
                messages.error(request, "Count must be a valid number.")
                return redirect("test_maker")

            created_users = 0
            created_regs = 0
            created_teams = 0
            created_participations = 0

            if tournament.registration_mode == "individual":
                for idx in range(1, reg_count + 1):
                    display_name = f"{reg_prefix}{idx}"[:100]
                    if tournament.individual_registrations.filter(display_name__iexact=display_name).exists():
                        continue
                    username = _next_unique_username(f"{reg_username_prefix}{idx}")
                    user = User.objects.create_user(
                        username=username,
                        password=reg_password,
                        first_name=display_name,
                    )
                    created_users += 1
                    ind_reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=user,
                        display_name=display_name,
                        status="active",
                    )
                    created_regs += 1
                    shadow = _ensure_shadow_team_for_registration(ind_reg, tournament.sport_type)
                    if shadow:
                        created_participations += 1
            else:
                members_per_team = max(1, int(request.POST.get("reg_members_per_team") or tournament.players_per_team or 1))
                for idx in range(1, reg_count + 1):
                    team_name = f"{reg_prefix}{idx}"
                    if TeamTournamentParticipation.objects.filter(tournament=tournament, team__name=team_name).exists():
                        continue
                    captain_username = _next_unique_username(f"{reg_username_prefix}{idx}p1")
                    captain = User.objects.create_user(
                        username=captain_username,
                        password=reg_password,
                        first_name=team_name,
                    )
                    created_users += 1
                    team, team_created = Team.objects.get_or_create(
                        name=team_name,
                        defaults={"sport_type": tournament.sport_type},
                    )
                    if team_created:
                        created_teams += 1
                    TeamMembership.objects.get_or_create(team=team, user=captain, defaults={"role": "captain"})
                    _, part_created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if part_created:
                        created_regs += 1
                        created_participations += 1
                    for member_idx in range(2, members_per_team + 1):
                        member_username = _next_unique_username(f"{reg_username_prefix}{idx}p{member_idx}")
                        member = User.objects.create_user(
                            username=member_username,
                            password=reg_password,
                            first_name=team_name,
                        )
                        created_users += 1
                        TeamMembership.objects.create(team=team, user=member, role="member")

            if tournament.registration_mode == "individual":
                summary = (
                    f"Registered {created_regs} individual(s), {created_users} user(s), "
                    f"{created_participations} shadow competitor(s)."
                )
            else:
                summary = (
                    f"Registered {created_regs} team(s), {created_teams} new team(s) created, "
                    f"{created_users} user(s), {created_participations} participation(s)."
                )
            log_action(request, "test_maker_register_to_open", summary, tournament=tournament)
            messages.success(request, summary)

        elif action == "register_existing_to_open_tournament":
            if tournament.registration_mode != "individual" and tournament.status != "registration_open":
                messages.error(request, "Tournament must have status 'Registration Open' to use this action.")
                return redirect("test_maker")

            existing_count_raw = request.POST.get("existing_count", "5")
            try:
                existing_count = max(1, int(existing_count_raw))
            except ValueError:
                messages.error(request, "Count must be a valid number.")
                return redirect("test_maker")

            registered = 0
            skipped = 0
            created_shadows = 0

            if tournament.registration_mode == "individual":
                # Only accounts Test Maker itself created — this used to sweep
                # up real users and register them without their consent.
                candidates = list(
                    User.objects.filter(
                        is_staff=False,
                        is_superuser=False,
                        username__startswith=settings.TEST_MAKER_USER_PREFIX,
                    )
                    .exclude(individual_registrations__tournament=tournament)
                    .order_by("username", "id")[:existing_count]
                )
                for user in candidates:
                    base_name = (user.first_name or user.username or "participant").strip()[:100] or "participant"
                    display_name = _next_unique_display_name(base_name, tournament)[:100]
                    ind_reg = TournamentIndividualRegistration.objects.create(
                        tournament=tournament,
                        user=user,
                        display_name=display_name,
                        status="active",
                    )
                    registered += 1
                    shadow = _ensure_shadow_team_for_registration(ind_reg, tournament.sport_type)
                    if shadow:
                        created_shadows += 1
                skipped = max(0, existing_count - len(candidates))
                summary = (
                    f"Registered {registered} existing individual(s) from existing user accounts; "
                    f"created {created_shadows} shadow competitor(s)."
                )
            else:
                candidates = list(
                    Team.objects.filter(is_internal=False)
                    .exclude(participations__tournament=tournament)
                    .order_by("name", "id")[:existing_count]
                )
                for team in candidates:
                    _, created = TeamTournamentParticipation.objects.get_or_create(
                        team=team,
                        tournament=tournament,
                        defaults={"status": "active"},
                    )
                    if created:
                        registered += 1
                    else:
                        skipped += 1
                skipped += max(0, existing_count - len(candidates))
                summary = f"Registered {registered} existing team(s) to tournament."

            if skipped > 0:
                summary = f"{summary} Skipped {skipped} slot(s) due to unavailable candidates."

            log_action(request, "test_maker_register_existing", summary, tournament=tournament)
            messages.success(request, summary)

        elif action == "randomize_court_preferences":
            if tournament.registration_mode == "individual":
                messages.warning(
                    request,
                    "Court preference randomization is team-level and is skipped for individual-mode tournaments.",
                )
                return redirect("test_maker")
            teams = list(
                Team.objects.filter(
                    participations__tournament=tournament,
                    participations__status="active",
                    is_internal=False,
                ).distinct().order_by("id")
            )
            courts = list(tournament.courts.filter(is_available=True).order_by("id"))
            if not courts:
                courts = list(tournament.courts.order_by("id"))

            if not teams:
                messages.warning(request, "No teams found in selected tournament.")
                return redirect("test_maker")
            if not courts:
                messages.warning(request, "No courts available to assign preferences.")
                return redirect("test_maker")

            for team in teams:
                pick_count = random.randint(1, min(3, len(courts)))
                picked = random.sample(courts, pick_count)
                participation, _ = TeamTournamentParticipation.objects.get_or_create(
                    team=team,
                    tournament=tournament,
                    defaults={"status": "active"},
                )
                TeamTournamentCourtPreference.objects.filter(participation=participation).delete()
                TeamTournamentCourtPreference.objects.bulk_create([
                    TeamTournamentCourtPreference(participation=participation, court=court)
                    for court in picked
                ])

            log_action(
                request,
                "test_maker_randomize_courts",
                f"Randomized court preferences for {len(teams)} team(s)",
                tournament=tournament,
            )
            messages.success(request, f"Randomized court preferences for {len(teams)} team(s).")

        elif action == "randomize_scores":
            limit_raw = request.POST.get("match_count", "10")
            try:
                limit = max(1, int(limit_raw))
            except ValueError:
                messages.error(request, "Match count must be a valid number.")
                return redirect("test_maker")

            terminal_statuses = ["confirmed", "forfeited", "cancelled", "bye"]
            matches = list(
                tournament.matches.filter(team1__isnull=False, team2__isnull=False)
                .exclude(status__in=terminal_statuses)
                .order_by("match_number", "id")[:limit]
            )

            if not matches:
                messages.warning(request, "No eligible matches found for score randomization.")
                return redirect("test_maker")

            updated = 0
            for match in matches:
                s1 = random.randint(0, 5)
                s2 = random.randint(0, 5)
                if s1 == s2:
                    if random.random() < 0.5:
                        s1 += 1
                    else:
                        s2 += 1

                match.score_team1 = s1
                match.score_team2 = s2
                match.winner = match.team1 if s1 > s2 else match.team2
                match.status = "confirmed"
                match.submitted_by = None
                match.confirmed_by = None
                match.save(update_fields=[
                    "score_team1", "score_team2", "winner", "status", "submitted_by", "confirmed_by"
                ])
                advance_winner(match)
                advance_loser_to_third_place(match)
                updated += 1

            log_action(
                request,
                "test_maker_randomize_scores",
                f"Randomized and confirmed scores for {updated} match(es)",
                tournament=tournament,
            )
            messages.success(request, f"Randomized and confirmed scores for {updated} match(es).")

        elif action == "randomize_schedule":
            limit_raw = request.POST.get("schedule_count", "20")
            try:
                limit = max(1, int(limit_raw))
            except ValueError:
                messages.error(request, "Schedule count must be a valid number.")
                return redirect("test_maker")

            courts = list(tournament.courts.filter(is_available=True).order_by("id"))
            if not courts:
                courts = list(tournament.courts.order_by("id"))
            if not courts:
                messages.warning(request, "No courts found. Add courts before random scheduling.")
                return redirect("test_maker")

            matches = list(
                tournament.matches.filter(team1__isnull=False, team2__isnull=False, scheduled_time__isnull=True)
                .exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"])
                .order_by("match_number", "id")[:limit]
            )
            if not matches:
                messages.warning(request, "No unscheduled eligible matches found.")
                return redirect("test_maker")

            duration_minutes = max(5, int(tournament.default_match_duration or 30))
            start_at = timezone.now().replace(second=0, microsecond=0) + timedelta(hours=1)

            updated = 0
            for idx, match in enumerate(matches):
                slot_start = start_at + timedelta(minutes=idx * duration_minutes)
                slot_end = slot_start + timedelta(minutes=duration_minutes)
                match.scheduled_time = slot_start
                match.scheduled_end_time = slot_end
                match.court = courts[idx % len(courts)]
                if match.status not in ["in_progress", "pending_confirmation"]:
                    match.status = "upcoming"
                match.save(update_fields=["scheduled_time", "scheduled_end_time", "court", "status"])
                updated += 1

            log_action(
                request,
                "test_maker_randomize_schedule",
                f"Randomly scheduled {updated} match(es)",
                tournament=tournament,
            )
            messages.success(request, f"Randomly scheduled {updated} match(es).")

        elif action == "set_dispute_window":
            minutes_raw = request.POST.get("dispute_window_minutes", "")
            try:
                minutes = max(1, int(minutes_raw))
            except (ValueError, TypeError):
                messages.error(request, "Dispute window must be a valid number of minutes.")
                return redirect("test_maker")
            if not tournament:
                messages.error(request, "Select a tournament first.")
                return redirect("test_maker")
            tournament.dispute_window_minutes = minutes
            tournament.save(update_fields=["dispute_window_minutes"])
            log_action(
                request,
                "test_maker_set_dispute_window",
                f"Dispute window set to {minutes} minute(s) by {request.user.username}",
                tournament=tournament,
            )
            messages.success(request, f"Dispute window updated to {minutes} minute(s) (takes effect on next score submission).")

        else:
            messages.error(request, "Unknown Test Maker action.")

        return redirect("test_maker")

    roster_count = 0
    roster_label = "Teams"
    if tournament:
        if tournament.registration_mode == "individual":
            roster_count = active_participant_count(tournament)
            roster_label = "Participants"
        else:
            roster_count = active_participant_count(tournament)
            roster_label = "Teams"
    available_existing_users = 0
    available_existing_teams = Team.objects.filter(is_internal=False).count()
    if tournament:
        if tournament.registration_mode == "individual":
            available_existing_users = User.objects.exclude(
                individual_registrations__tournament=tournament
            ).count()
        available_existing_teams = Team.objects.filter(is_internal=False).exclude(
            participations__tournament=tournament
        ).count()

    context = {
        "tournament": tournament,
        "registration_mode": tournament.registration_mode if tournament else "team",
        "total_teams": roster_count,
        "roster_label": roster_label,
        "total_courts": tournament.courts.count() if tournament else 0,
        "total_matches": tournament.matches.count() if tournament else 0,
        "pending_matches": (
            tournament.matches.exclude(status__in=["confirmed", "forfeited", "cancelled", "bye"]).count()
            if tournament else 0
        ),
        "available_existing_users": available_existing_users,
        "available_existing_teams": available_existing_teams,
        "dispute_window_minutes": (
            tournament.dispute_window_minutes if tournament
            else DEFAULT_DISPUTE_WINDOW_MINUTES
        ),
        **_tournament_context(request, tournament),
    }
    return render(request, "core/test_maker.html", context)
