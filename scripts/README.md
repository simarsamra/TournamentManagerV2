# Ad-hoc scripts

One-off diagnostic, seeding and verification scripts. These are **not** tests and
are not run by `manage.py test` — several of them execute database queries at
import time, which is why they live outside the project root where Django's
`test*.py` discovery would otherwise pick them up.

Run them from anywhere; each puts the project root on `sys.path` itself:

```bash
python scripts/<name>.py
```

They operate on whatever database `DJANGO_SETTINGS_MODULE` resolves to — by
default the real `db.sqlite3`, **not** a test database. Several of them write.
Read a script before running it.

Several hardcode primary keys or usernames from one developer's database (for
example `Tournament.objects.get(pk=12)`, or the user `t2p1`) and will not work
unmodified elsewhere.

| Script | What it does | Writes? |
|---|---|---|
| `check_db.py` | Prints tournaments, courts and availability rows | no |
| `check_delete_fix.py` | Exercises the team-delete path through the test client | yes |
| `check_knockout.py` | Prints knockout bracket state for the active tournament | no |
| `check_perms.py` | Dumps one upcoming knockout match plus the roster and status flags that gate score entry | no |
| `check_upcoming.py` | Lists upcoming knockout matches for the active tournament | no |
| `diagnose_get_team.py` | Traces `core.views._get_team` for a given user | no |
| `diagnose_knockout.py` | Dumps knockout round/scheduling detail | no |
| `diagnose_match_195.py` | Inspects one hardcoded match (pk 195) | no |
| `promote_t2p1.py` | Legacy: sets `is_staff` on user `t2p1` | **yes** |
| `seed_tt1.py` | Seeds teams/members into tournament pk 12 | **yes** |
| `verify_completion_feature.py` | Prints tournament counts by status and standings for completed ones | no |
| `verify_dual_role_toggle.py` | Checks dual-role detection | no |
| `verify_role_separation.py` | Checks organizer/team role separation | no |

`promote_t2p1.py` predates the `OrganizerProfile` model: it grants organizer
access by setting `is_staff`, which is no longer the primary organizer signal.
Prefer promoting through the Settings page, or by creating
`OrganizerProfile(user=..., verified=True)`. See `DUAL_ROLE_TOGGLE_FEATURE.md`.

## fixtures/

Sample data used by the seeding scripts and for manual testing.

**`fixtures/teams.txt` contains plaintext passwords** (`pass123`) in its
`team_name,username,password,players` rows. It is sample data for local testing
only — never import it into a deployment that real people log in to.

`fixtures/sample_match_results.csv` holds 187 sample match results
(`Team_A,Team_B,Score_A,Score_B,Notes`). Nothing in the application imports it;
it is reference data only. It was committed at the repository root under a
truncated filename (`eam_A,Team_B,...`), evidently a shell redirect accident;
the header's leading `T` has been restored here.
