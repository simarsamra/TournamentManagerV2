from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("register/", views.account_register_view, name="account_register"),
    # Legacy aliases kept for backwards compat
    path("register/legacy/", views.register_view, name="register"),
    path("tournament/<int:pk>/register/", views.register_view, name="tournament_register"),

    # Tournament join flow (post-login)
    path("join/", views.join_tournament_list_view, name="join_tournament_list"),
    path("tournament/<int:pk>/join/", views.join_tournament_view, name="join_tournament"),
    path("tournament/<int:tournament_pk>/join/<int:team_pk>/", views.join_team_view, name="join_team"),
    path("tournament/<int:pk>/create-team/", views.create_team_view, name="create_team"),

    # Dashboard
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("", views.dashboard_view, name="home"),

    # Tournament setup
    path("tournament/setup/", views.tournament_setup, name="tournament_setup"),
    path("tournament/<int:pk>/config/", views.tournament_config, name="tournament_config"),
    path("tournament/<int:pk>/add-court/", views.add_court, name="add_court"),
    path("tournament/<int:pk>/add-court-availability/", views.add_court_availability, name="add_court_availability"),
    path("tournament/<int:pk>/add-timeslot/", views.add_timeslot, name="add_timeslot"),
    path("tournament/<int:pk>/add-teams/", views.add_teams_bulk, name="add_teams_bulk"),
    path("tournament/<int:pk>/open-registration/", views.open_registration, name="open_registration"),
    path("tournament/<int:pk>/close-registration/", views.close_registration, name="close_registration"),
    path("tournament/<int:pk>/generate-schedule/", views.generate_schedule, name="generate_schedule"),
    path("tournament/<int:pk>/start/", views.start_tournament, name="start_tournament"),
    path("tournament/<int:pk>/complete/", views.complete_tournament, name="complete_tournament"),
    path("tournament/<int:pk>/proceed-to-knockout/", views.proceed_to_knockout_view, name="proceed_to_knockout"),
    path("tournament/select/", views.select_tournament, name="select_tournament"),
    path("tournament/<int:pk>/delete/", views.delete_tournament, name="delete_tournament"),

    # Fixtures
    path("fixtures/", views.fixtures_view, name="fixtures"),

    # Match
    path("match/<int:pk>/", views.match_detail, name="match_detail"),
    path("match/<int:pk>/submit-score/", views.submit_score, name="submit_score"),
    path("match/<int:pk>/confirm-score/", views.confirm_score, name="confirm_score"),
    path("match/<int:pk>/dispute-score/", views.dispute_score, name="dispute_score"),
    path("match/<int:pk>/resolve-dispute/", views.resolve_dispute, name="resolve_dispute"),
    path("match/<int:pk>/report-no-show/", views.report_no_show, name="report_no_show"),
    path("match/<int:pk>/mark-no-show/", views.mark_no_show, name="mark_no_show"),

    # Rescheduling
    path("match/<int:pk>/reschedule/", views.request_reschedule, name="request_reschedule"),
    path("reschedule/<int:pk>/respond/", views.respond_reschedule, name="respond_reschedule"),
    path("rescheduling/", views.rescheduling_view, name="rescheduling"),

    # Teams
    path("teams/", views.teams_view, name="teams"),
    path("team/<int:pk>/", views.team_detail, name="team_detail"),
    path("team/<int:pk>/withdraw/", views.withdraw_team, name="withdraw_team"),
    path("team/<int:pk>/preferences/", views.team_preferences, name="team_preferences"),
    path("team/<int:pk>/members/add/", views.manage_team_members, name="manage_team_members"),
    path("team/<int:pk>/members/<int:user_pk>/remove/", views.remove_team_member, name="remove_team_member"),
    path("team/<int:pk>/members/<int:user_pk>/reset-password/", views.reset_member_password, name="reset_member_password"),
    path("team/<int:pk>/captain/reset-password/", views.reset_captain_password, name="reset_captain_password"),

    # Standings
    path("standings/", views.standings_view, name="standings"),

    # Open Slots
    path("open-slots/", views.open_slots_view, name="open_slots"),

    # Analytics
    path("analytics/", views.analytics_view, name="analytics"),

    # Backup & Restore
    path("backup/", views.backup_view, name="backup"),
    path("backup/create/", views.create_backup_view, name="create_backup"),
    path("backup/restore/", views.restore_backup_view, name="restore_backup"),
    path("backup/delete/", views.delete_backup_view, name="delete_backup"),

    # Audit Log
    path("audit-log/", views.audit_log_view, name="audit_log"),

    # Settings
    path("settings/", views.settings_view, name="settings"),

    # Public views (no login required)
    path("public/standings/", views.public_standings, name="public_standings"),
    path("public/fixtures/", views.public_fixtures, name="public_fixtures"),
]
