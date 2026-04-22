#!/usr/bin/env python
"""
Quick verification script to check if the tournament completion celebration feature is working.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
django.setup()

from core.models import Tournament, Team
from core.standings import calculate_standings

# Check for tournaments with different statuses
print("=" * 60)
print("Tournament Status Overview")
print("=" * 60)

for status in ['setup', 'registration_open', 'ready', 'scheduled', 'active', 'completed']:
    count = Tournament.objects.filter(status=status).count()
    print(f"{status.upper():20} : {count:3} tournaments")

print("\n" + "=" * 60)
print("Completed Tournaments")
print("=" * 60)

completed_tournaments = Tournament.objects.filter(status='completed')
for t in completed_tournaments:
    print(f"\nTournament: {t.name} (ID: {t.id})")
    print(f"  Champion: {t.champion}")
    print(f"  Format: {t.format}")
    print(f"  Status: {t.status}")
    
    # Calculate standings if applicable
    if t.format in ("round_robin", "double_round_robin", "hybrid"):
        standings = calculate_standings(t)
        if standings:
            print(f"  Final Standings:")
            for i, s in enumerate(standings[:3]):
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else ""
                print(f"    {medal} #{s['rank']}: {s['team'].name} - {s['points']} pts ({s['wins']}W {s['losses']}L)")
        else:
            print("  No standings available")
    else:
        print(f"  (Bracket format - no round-robin standings)")

print("\n" + "=" * 60)
print("Verification: Context variables that would be passed to template:")
print("=" * 60)

for t in completed_tournaments[:1]:  # Show for first completed tournament
    print(f"\nTournament: {t.name}")
    standings = calculate_standings(t) if t.format in ("round_robin", "double_round_robin", "hybrid") else []
    
    context = {
        "tournament_champion": standings[0]["team"] if standings else t.champion,
        "tournament_runner_up_1": standings[1]["team"] if len(standings) > 1 else None,
        "tournament_runner_up_2": standings[2]["team"] if len(standings) > 2 else None,
    }
    
    print(f"  tournament_champion: {context['tournament_champion']}")
    print(f"  tournament_runner_up_1: {context['tournament_runner_up_1']}")
    print(f"  tournament_runner_up_2: {context['tournament_runner_up_2']}")

print("\n✓ Feature verification complete!")
