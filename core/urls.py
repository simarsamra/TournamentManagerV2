from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("register/", views.account_register_view, name="account_register"),
    path("profile/", views.profile_view, name="profile"),
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
    path("", views.public_home, name="home"),
    path("toggle-view/", views.toggle_view_preference, name="toggle_view_preference"),

    # Tournament setup
    path("tournament/setup/", views.tournament_setup, name="tournament_setup"),
    path("tournament/<int:pk>/config/", views.tournament_config, name="tournament_config"),
    path("tournament/<int:pk>/add-court/", views.add_court, name="add_court"),
    path("tournament/<int:pk>/add-court-availability/", views.add_court_availability, name="add_court_availability"),
    path("tournament/<int:pk>/estimate-availability-end-date/", views.estimate_court_availability_end_date, name="estimate_court_availability_end_date"),
    path("tournament/<int:pk>/delete-court-availability/<int:availability_pk>/", views.delete_court_availability, name="delete_court_availability"),
    path("tournament/<int:pk>/add-timeslot/", views.add_timeslot, name="add_timeslot"),
    path("tournament/<int:pk>/add-teams/", views.add_teams_bulk, name="add_teams_bulk"),
    path("tournament/<int:pk>/remove-team/<int:participation_pk>/", views.remove_team_from_tournament, name="remove_team_from_tournament"),
    path("tournament/<int:pk>/open-registration/", views.open_registration, name="open_registration"),
    path("tournament/<int:pk>/close-registration/", views.close_registration, name="close_registration"),
    path("tournament/<int:pk>/generate-schedule/", views.generate_schedule, name="generate_schedule"),
    path("tournament/<int:pk>/start/", views.start_tournament, name="start_tournament"),
    path("tournament/<int:pk>/complete/", views.complete_tournament, name="complete_tournament"),
    path("tournament/<int:pk>/proceed-to-knockout/", views.proceed_to_knockout_view, name="proceed_to_knockout"),
    path("tournament/<int:pk>/estimate-end-date/", views.estimate_tournament_end_date, name="estimate_tournament_end_date"),
    path("tournament/select/", views.select_tournament, name="select_tournament"),
    path("tournament/<int:pk>/delete/", views.delete_tournament, name="delete_tournament"),
    path("tournament/<int:pk>/cancel/", views.cancel_tournament, name="cancel_tournament"),
    path("tournament/<int:pk>/pause/", views.pause_tournament, name="pause_tournament"),
    path("tournament/<int:pk>/resume/", views.resume_tournament, name="resume_tournament"),
    path("tournament/<int:pk>/duplicate/", views.duplicate_tournament, name="duplicate_tournament"),
    path("tournament/<int:pk>/registrations/", views.registration_review_view, name="registration_review"),
    path("tournament/<int:tournament_pk>/registrations/<int:reg_pk>/approve/", views.approve_registration, name="approve_registration"),
    path("tournament/<int:tournament_pk>/registrations/<int:reg_pk>/reject/", views.reject_registration, name="reject_registration"),
    path("tournament/<int:tournament_pk>/disqualify/<int:participation_pk>/", views.disqualify_team, name="disqualify_team"),
    path("tournament/<int:pk>/announce/", views.organizer_announce_view, name="organizer_announce"),

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
    path("match/<int:pk>/override-result/", views.override_match_result, name="override_match_result"),

    # Rescheduling
    path("match/<int:pk>/reschedule/", views.request_reschedule, name="request_reschedule"),
    path("reschedule/<int:pk>/respond/", views.respond_reschedule, name="respond_reschedule"),
    path("rescheduling/", views.rescheduling_view, name="rescheduling"),

    # Teams
    path("teams/search/", views.team_search_view, name="team_search"),
    path("teams/", views.teams_view, name="teams"),
    path("team/<int:pk>/", views.team_detail, name="team_detail"),
    path("teams/create/", views.create_standalone_team_view, name="create_standalone_team"),
    path("team/<int:pk>/withdraw/", views.withdraw_team, name="withdraw_team"),
    path("team/<int:pk>/remove/", views.organizer_remove_team, name="organizer_remove_team"),
    path("team/<int:pk>/preferences/", views.team_preferences, name="team_preferences"),
    path("team/<int:pk>/members/add/", views.manage_team_members, name="manage_team_members"),
    path("team/<int:pk>/members/<int:user_pk>/remove/", views.remove_team_member, name="remove_team_member"),
    path("team/<int:pk>/members/<int:user_pk>/reset-password/", views.reset_member_password, name="reset_member_password"),
    path("team/<int:pk>/captain/reset-password/", views.reset_captain_password, name="reset_captain_password"),
    path("team/<int:pk>/leave/", views.leave_team_view, name="leave_team"),
    path("team/<int:pk>/transfer-captain/", views.transfer_captaincy_view, name="transfer_captaincy"),
    path("team/<int:pk>/delete/", views.delete_team_view, name="delete_team"),
    path("team/<int:pk>/history/", views.team_history_view, name="team_history"),
    path("team/<int:pk>/stats/", views.team_stats_view, name="team_stats"),
    path("team/<int:pk>/invite/", views.team_invite_view, name="team_invite"),
    path("team-invite/<int:pk>/accept/", views.accept_team_invite, name="accept_team_invite"),
    path("team-invite/<int:pk>/decline/", views.decline_team_invite, name="decline_team_invite"),
    path("teams/my-invites/", views.my_invites_view, name="my_invites"),
    path("tournament/<int:pk>/enter-team/", views.enter_existing_team_view, name="enter_existing_team"),

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
    path("settings/compute-end-date/<int:pk>/", views.compute_end_date_view, name="compute_end_date"),
    path("testing/", views.test_maker_view, name="test_maker"),
    path("settings/users/<int:user_pk>/organizer/", views.set_user_organizer, name="set_user_organizer"),
    path("settings/users/<int:user_pk>/delete/", views.delete_user_account, name="delete_user_account"),
    path("settings/users/<int:user_pk>/suspend/", views.toggle_user_suspension, name="toggle_user_suspension"),
    path("settings/impersonate/<int:user_pk>/", views.impersonate_user, name="impersonate_user"),
    path("settings/stop-impersonating/", views.stop_impersonating, name="stop_impersonating"),
    path("settings/organizer-applications/<int:pk>/review/", views.review_organizer_application, name="review_organizer_application"),

    # Public views (no login required)
    path("public/standings/", views.public_standings, name="public_standings"),
    path("public/fixtures/", views.public_fixtures, name="public_fixtures"),

    # NEW: Public tournament listing & detail (4.1, 4.2)
    path("tournaments/", views.tournament_list_view, name="tournament_list"),
    path("tournaments/<int:pk>/", views.tournament_public_detail, name="tournament_public_detail"),

    # NEW: User public profile (1.5)
    path("users/search/", views.user_search_view, name="user_search"),
    path("users/<str:username>/", views.user_public_profile, name="user_public_profile"),

    # NEW: My registrations (4.8)
    path("dashboard/registrations/", views.my_registrations_view, name="my_registrations"),

    # NEW: Notifications (8.1–8.3)
    path("notifications/", views.notifications_view, name="notifications"),
    path("notifications/mark-all-read/", views.mark_notifications_read, name="mark_notifications_read"),
    path("notifications/<int:pk>/read/", views.mark_notification_read, name="mark_notification_read"),

    # NEW: Organizer application (1.6)
    path("organizer/apply/", views.organizer_apply_view, name="organizer_apply"),

    # NEW: Organizer public page (9.4)
    path("organizers/<int:pk>/", views.organizer_public_page, name="organizer_public_page"),

    # NEW: Seed participants (3.10)
    path("tournament/<int:pk>/seed/", views.seed_participants_view, name="seed_participants"),

    # NEW: Substitute player management (7.3)
    path("tournament/<int:pk>/teams/<int:participation_pk>/sub/", views.tournament_team_sub_view, name="tournament_team_sub"),
]
