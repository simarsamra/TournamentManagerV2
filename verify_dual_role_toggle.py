#!/usr/bin/env python
"""
Verification script to demonstrate the dual-role toggle feature.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
django.setup()

from django.contrib.auth.models import User
from core.models import Tournament, Team, TeamMembership
from core.views import _is_organizer, _has_dual_roles

# Get t2p1 user
try:
    user = User.objects.get(username='t2p1')
except User.DoesNotExist:
    print("❌ User t2p1 not found")
    exit(1)

print("=" * 70)
print("DUAL-ROLE USER TOGGLE FEATURE VERIFICATION")
print("=" * 70)

print(f"\nUser: {user.username}")
print(f"Is Organizer (is_staff): {user.is_staff}")
print(f"Is Superuser: {user.is_superuser}")

print(f"\n_is_organizer(user): {_is_organizer(user)}")
print(f"_has_dual_roles(user): {_has_dual_roles(user)}")

print(f"\nTeam Memberships for {user.username}:")
memberships = user.memberships.all()
if memberships:
    for m in memberships:
        print(f"  - {m.team.name} ({m.team.tournament.name}): {m.role}")
else:
    print("  (none)")

if _has_dual_roles(user):
    print("\n✅ USER HAS DUAL ROLES - Toggle feature ENABLED")
    print("\nToggle Feature Details:")
    print("  • View Mode Preference stored in: request.session['view_mode']")
    print("  • Default view_mode: 'team' (shows team dashboard)")
    print("  • When view_mode == 'organizer': redirects to tournament_setup")
    print("  • When view_mode == 'team': shows team dashboard")
    print("\nUI Component:")
    print("  • Toggle button appears in top ribbon")
    print("  • Button text changes based on current view mode")
    print("  • Clicking toggles between 'Team View' and 'Organizer View'")
    print("\nBehavior:")
    print("  1. User logs in with dual-role account (e.g., t2p1)")
    print("  2. Dashboard shows team view (default)")
    print("  3. Toggle button shows '⚙️ Organizer View'")
    print("  4. Clicking toggle sets view_mode to 'organizer' in session")
    print("  5. Dashboard redirects to tournament_setup")
    print("  6. Now toggle button shows '👤 Team View'")
    print("  7. Clicking toggle sets view_mode to 'team' in session")
    print("  8. Back to team dashboard")
else:
    print("\n❌ USER DOES NOT HAVE DUAL ROLES")
    print("   - To enable dual roles: make user is_staff=True and ensure they have team memberships")

print("\n" + "=" * 70)
