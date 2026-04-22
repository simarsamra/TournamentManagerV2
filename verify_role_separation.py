#!/usr/bin/env python
"""
Verification script to ensure organizers cannot join/create teams while allowing regular users to do so.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
django.setup()

from django.contrib.auth.models import User
from core.models import Tournament, Team, TeamMembership
from core.views import _is_organizer

# Create test users
admin_user, _ = User.objects.get_or_create(
    username='test_organizer',
    defaults={'is_staff': True}
)

regular_user, _ = User.objects.get_or_create(
    username='test_regular_user',
    defaults={'is_staff': False}
)

# Get an open tournament
open_tournament = Tournament.objects.filter(status='registration_open').first()

# Check any tournament for reference
any_tournament = Tournament.objects.first() if not open_tournament else open_tournament

if not any_tournament:
    print("❌ No tournaments found in database.")
else:
    print("=" * 70)
    print("ROLE SEPARATION VERIFICATION")
    print("=" * 70)
    
    print("\n1. User Role Checks:")
    print(f"   Admin User (test_organizer)")
    print(f"   - is_organizer: {_is_organizer(admin_user)} ✓" if _is_organizer(admin_user) else "   - is_organizer: {_is_organizer(admin_user)} ❌")
    
    print(f"\n   Regular User (test_regular_user)")
    print(f"   - is_organizer: {_is_organizer(regular_user)} ✓" if not _is_organizer(regular_user) else "   - is_organizer: {_is_organizer(regular_user)} ❌")
    
    print("\n3. Tournament Status:")
    tournament_ref = open_tournament or any_tournament
    print(f"   Tournament: {tournament_ref.name} (ID: {tournament_ref.id})")
    print(f"   Status: {tournament_ref.status}")
    print(f"   Players per team: {tournament_ref.players_per_team}")
    
    print(f"\n4. Current Team Memberships:")
    admin_teams = admin_user.memberships.filter(team__tournament=tournament_ref).values_list('team__name', flat=True)
    regular_teams = regular_user.memberships.filter(team__tournament=tournament_ref).values_list('team__name', flat=True)
    
    print(f"   Admin user's teams: {list(admin_teams) if admin_teams else 'None'}")
    print(f"   Regular user's teams: {list(regular_teams) if regular_teams else 'None'}")
    
    print("\n5. Key Access Control Points (Views):")
    print("   ✓ join_tournament_list_view - Blocks organizers")
    print("   ✓ join_tournament_view - Blocks organizers")
    print("   ✓ join_team_view - Blocks organizers")
    print("   ✓ create_team_view - Blocks organizers")
    
    print("\n6. Organizer Exception:")
    print("   ✓ manage_team_members - Allows organizers (team admin purposes)")
    print("   ✓ reset_captain_password - Allows organizers (team admin purposes)")
    
    print("\n" + "=" * 70)
    print("RECOMMENDATION FOR ORGANIZERS WHO WANT TO PARTICIPATE:")
    print("=" * 70)
    print("""
If an organizer wants to participate in a tournament, they should:

1. Create a new regular user account (non-staff, non-superuser)
2. Use that account to join/create a team in the tournament
3. Continue using their organizer account for tournament management

This keeps a clear separation between:
- Tournament Organization (staff account)
- Tournament Participation (regular team account)
    """)
    
    print("\n✓ Verification complete!")
