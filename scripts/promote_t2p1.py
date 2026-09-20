#!/usr/bin/env python
"""
Script to promote t2p1 to organizer (make staff) while retaining team membership.
This enables the dual-role toggle feature.
"""
import os
import django

import sys

# Scripts live in scripts/; put the project root on sys.path so that
# `tournament_manager` and `core` import no matter where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
django.setup()

from django.contrib.auth.models import User

# Get t2p1 user
try:
    user = User.objects.get(username='t2p1')
except User.DoesNotExist:
    print("❌ User t2p1 not found")
    exit(1)

print("=" * 70)
print("PROMOTING USER TO ORGANIZER")
print("=" * 70)

print("\nBefore:")
print(f"  User: {user.username}")
print(f"  is_staff: {user.is_staff}")
print(f"  is_superuser: {user.is_superuser}")

# Promote to staff
user.is_staff = True
user.save()

print("\nAfter:")
print(f"  User: {user.username}")
print(f"  is_staff: {user.is_staff}")
print(f"  is_superuser: {user.is_superuser}")

print("\nTeam Memberships (retained):")
for m in user.memberships.all():
    print(f"  - {m.team.name} ({m.team.tournament.name}): {m.role}")

print("\n✅ User t2p1 is now an organizer!")
print("\nNow when t2p1 logs in:")
print("  1. The system will detect dual-role status")
print("  2. A toggle button will appear in the top ribbon")
print("  3. Can toggle between 'Team View' and 'Organizer View'")

print("\n" + "=" * 70)
