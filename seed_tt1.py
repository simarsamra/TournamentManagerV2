import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tournament_manager.settings')
django.setup()

from core.models import Tournament, Team, TeamMembership
from django.contrib.auth.models import User

t = Tournament.objects.get(pk=12)
print(f'Tournament: {t.name!r} (pk={t.pk}) status={t.status}')
print()

# Player 2 map: prefer t{N}p2, fallback p2t{N}
p2_map = {}
for n in range(1, 21):
    if User.objects.filter(username=f't{n}p2').exists():
        p2_map[n] = f't{n}p2'
    elif User.objects.filter(username=f'p2t{n}').exists():
        p2_map[n] = f'p2t{n}'
    else:
        p2_map[n] = f't{n}p2'  # will be created

for n in range(1, 21):
    team_name = f'Team {n}'
    cap_uname = f't{n}p1'
    p2_uname = p2_map[n]

    # Captain
    cap, created = User.objects.get_or_create(username=cap_uname)
    cap.set_password('pass123')
    if not cap.first_name:
        cap.first_name = f'Player T{n}P1'
    cap.save()
    label = 'created' if created else 'found'
    print(f'  Captain  {cap_uname}: {label}')

    # Player 2
    p2, created2 = User.objects.get_or_create(username=p2_uname)
    p2.set_password('pass123')
    if not p2.first_name:
        p2.first_name = f'Player T{n}P2'
    p2.save()
    label2 = 'created' if created2 else 'found'
    print(f'  Player2  {p2_uname}: {label2}')

    # Team
    team, _ = Team.objects.get_or_create(
        tournament=t, name=team_name, defaults={'user': cap}
    )

    # Memberships
    TeamMembership.objects.get_or_create(team=team, user=cap, defaults={'role': 'captain'})
    TeamMembership.objects.get_or_create(team=team, user=p2, defaults={'role': 'member'})
    print(f'  -> {team_name} registered')
    print()

print(f'Total teams in TT 1: {t.teams.count()}')
print()
for team in t.teams.order_by('name'):
    mems = team.memberships.select_related('user').all()
    print(f'  {team.name}: ' + ', '.join(f'{m.user.username}({m.role})' for m in mems))
