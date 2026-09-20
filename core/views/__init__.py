"""Core views.

This was a single 7,400-line module holding request handling, authorisation
and business logic together. It is now a package split by domain:

    helpers       authorisation predicates, lookup, match finalisation
    auth          sign-in, registration, profile, dashboard
    tournaments   setup, courts, availability, lifecycle transitions
    teams         rosters, invitations, captaincy
    matches       fixtures, scores, disputes, reschedules, no-shows
    registration  joining, registration review, participant seeding
    reporting     standings, analytics, backups, search, public pages
    admin_tools   settings, user management, impersonation
    test_maker    development-only data generator

Every name is re-exported here, so `from core.views import X` and the
`views.X` references in urls.py resolve exactly as they did before. The star
imports are controlled by each module's public surface -- helpers declares an
explicit ``__all__`` covering its underscore-prefixed names, which several
tests and scripts import by name.

Modules depend on helpers, never the reverse. The single exception is
registration importing tournament_config from tournaments, which is a
redirect target rather than a cycle.
"""
from .helpers import *  # noqa: F401,F403
from .auth import *  # noqa: F401,F403
from .tournaments import *  # noqa: F401,F403
from .teams import *  # noqa: F401,F403
from .matches import *  # noqa: F401,F403
from .matches import _redirect_to_match_detail  # noqa: F401
from .registration import *  # noqa: F401,F403
from .reporting import *  # noqa: F401,F403
from .admin_tools import *  # noqa: F401,F403
from .test_maker import *  # noqa: F401,F403
