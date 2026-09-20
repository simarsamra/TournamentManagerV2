# scripts/

The thirteen ad-hoc diagnostic, seeding and verification scripts that used to
live here are gone (FOLLOWUP_PLAN.md F-6). None of them were tests — several
hardcoded primary keys or usernames from one developer's database (for
example `Tournament.objects.get(pk=12)`, or the user `t2p1`), and one
(`seed_tt1.py`) had stopped working entirely: it referenced `Team` fields
(`tournament=`, `user=`) that don't exist since the
Team/TeamTournamentParticipation split.

Where each one went:

- **Read-only dumps** (`check_db.py`, `check_knockout.py`, `check_upcoming.py`,
  `check_perms.py`) — deleted. The Django admin and `manage.py shell` already
  cover this ground.
- **Diagnostics for a bug that was fixed** (`diagnose_match_195.py`,
  `diagnose_get_team.py`, `diagnose_knockout.py`, `promote_t2p1.py`) —
  deleted. Bound to one developer's data.
- **`check_delete_fix.py`** — deleted.
- **`verify_dual_role_toggle.py`** — deleted. Everything it printed is
  already covered, more thoroughly, by `core/tests_dual_role.py`.
- **`verify_role_separation.py`** — deleted, not converted. Its printed
  claims ("join_team_view - Blocks organizers", "create_team_view - Blocks
  organizers") don't match the current code: neither view checks
  `_is_organizer` at all. Rather than encode a false claim as a test, it was
  removed; `_is_organizer` itself is already covered by
  `core/tests_auth_predicates.py`. If blocking organizers from
  joining/creating teams is still wanted, that's a real feature gap, not a
  test-coverage one.
- **`verify_completion_feature.py`** — converted to
  `core/tests_dashboard_completion.py`. Writing the test surfaced a real bug
  it fixed along the way: `dashboard_content.html` unconditionally resolved
  `tournament.champion.name` as a filter argument even when
  `tournament.champion` is `None` -- the normal case for a completed
  round-robin tournament -- which 500'd the dashboard for exactly the
  scenario this script existed to demonstrate.
- **`seed_tt1.py`** — promoted to `python manage.py seed_demo`, rewritten
  against the current schema (the old script no longer ran). See
  `core/management/commands/seed_demo.py` and `core/tests_seed_demo.py`.

## fixtures/

Sample data for manual testing. Nothing currently imports these files
programmatically — including `seed_demo`, which generates its own
`t<N>p1`/`t<N>p2` usernames rather than reading them, the same as the script
it replaced.

**`fixtures/teams.txt` contains plaintext passwords** (`pass123`) in its
`team_name,username,password,players` rows. It is sample data for local testing
only — never import it into a deployment that real people log in to.

`fixtures/sample_match_results.csv` holds 187 sample match results
(`Team_A,Team_B,Score_A,Score_B,Notes`). Nothing in the application imports it;
it is reference data only. It was committed at the repository root under a
truncated filename (`eam_A,Team_B,...`), evidently a shell redirect accident;
the header's leading `T` has been restored here.
