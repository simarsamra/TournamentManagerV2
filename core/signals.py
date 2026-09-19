"""Signal handlers for user-adjacent bookkeeping.

Defined at module level (rather than inside AppConfig.ready) so they can be
imported, given stable dispatch_uids, and temporarily suppressed — the restore
path needs to insert User rows without these handlers racing it.
"""
from contextlib import contextmanager

from django.contrib.auth.models import User
from django.db.models.signals import post_save

ORGANIZER_PROFILE_UID = "core.create_organizer_profile"
TEAM_ASSIGNMENT_UID = "core.create_user_team_assignment"


def create_organizer_profile(sender, instance, created, **kwargs):
    """Create an OrganizerProfile for a newly created user.

    `verified` is seeded from is_staff at creation and then left alone. It must
    NOT be re-synced on later saves: verified is granted independently (via an
    approved organizer application or the Settings page) for users who are not
    staff, and re-syncing silently revokes that grant the next time the User row
    is saved — including the update_last_login save on every login.
    """
    if not created:
        return
    from .models import OrganizerProfile

    OrganizerProfile.objects.get_or_create(
        user=instance, defaults={"verified": instance.is_staff}
    )


def create_user_team_assignment(sender, instance, created, **kwargs):
    """Create the one-per-user active-team pointer for a newly created user."""
    if not created:
        return
    from .models import UserTeamAssignment

    UserTeamAssignment.objects.get_or_create(user=instance)


def connect_user_signals():
    post_save.connect(
        create_organizer_profile, sender=User, dispatch_uid=ORGANIZER_PROFILE_UID
    )
    post_save.connect(
        create_user_team_assignment, sender=User, dispatch_uid=TEAM_ASSIGNMENT_UID
    )


@contextmanager
def suppress_user_autocreate():
    """Disable the auto-create handlers for the duration of the block.

    Used by restore_backup: the backup already contains the OrganizerProfile and
    UserTeamAssignment rows, so letting the handlers fire while User rows are
    being inserted produces a UNIQUE violation on their one-to-one user column.
    """
    post_save.disconnect(sender=User, dispatch_uid=ORGANIZER_PROFILE_UID)
    post_save.disconnect(sender=User, dispatch_uid=TEAM_ASSIGNMENT_UID)
    try:
        yield
    finally:
        connect_user_signals()
