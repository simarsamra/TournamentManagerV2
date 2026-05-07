#!/usr/bin/env python
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.test import Client
from django.contrib.auth import get_user_model
from core.models import Tournament, CourtAvailability

User = get_user_model()

# Get first tournament and user
tournament = Tournament.objects.first()
user = User.objects.first()

if tournament and user:
    # Get first court availability from tournament's courts
    ca = CourtAvailability.objects.filter(court__tournament=tournament).first()
    
    if ca:
        client = Client()
        client.force_login(user)
        
        print(f"Testing DELETE endpoint for Court Availability {ca.pk}")
        print(f"URL: /tournament/{tournament.pk}/delete-court-availability/{ca.pk}/")
        
        # Test POST request
        response = client.post(
            f'/tournament/{tournament.pk}/delete-court-availability/{ca.pk}/',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            HTTP_HX_REQUEST='true'
        )
        
        print(f"\nPOST Response Status: {response.status_code}")
        print(f"Content-Type: {response.get('Content-Type')}")
        if response.status_code == 200:
            print("✓ DELETE with POST successful!")
        else:
            print(f"✗ Unexpected status: {response.status_code}")
            if response.content:
                print(f"Response: {response.content[:500]}")
    else:
        print("No court availabilities found in tournament")
else:
    print("No tournaments or users found in database")
