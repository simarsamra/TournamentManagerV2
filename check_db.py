#!/usr/bin/env python
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from core.models import Tournament, Court, CourtAvailability

print("Tournaments:")
for t in Tournament.objects.all()[:5]:
    print(f"  {t.pk}: {t.name}")
    for court in t.courts.all():
        print(f"    Court {court.pk}: {court.name}")
        for ca in court.availabilities.all():
            print(f"      CourtAvailability {ca.pk}: {ca.get_weekday_display()}")

print(f"\nTotal CourtAvailabilities: {CourtAvailability.objects.count()}")
