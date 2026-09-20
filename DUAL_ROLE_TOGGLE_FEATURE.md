# Dual-Role User Toggle

## Overview

A user can be both an organizer and a member of a team. Such a user sees a
toggle in the top ribbon that switches the dashboard between two presentations:

- **Team view** — match schedule, standings and team-specific blocks.
- **Organizer view** — tournament management blocks.

Both are the *same page*. The toggle changes which blocks render; it does not
navigate anywhere else.

## Detection

```python
# core/views.py
def _has_dual_roles(user):
    if not _is_organizer(user):
        return False
    return user.memberships.exists()
```

`_is_organizer` is the organizer signal, and it is **not** `is_staff` alone:

```python
def _is_organizer(user):
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True
    return hasattr(user, "organizer_profile") and user.organizer_profile.verified
```

The primary path is a **verified `OrganizerProfile`**, granted through the
organizer application and approval flow. `is_superuser` and `is_staff` are
accepted as well, so a Django superuser is always an organizer, but promoting
someone by setting `is_staff` is the legacy route, not the intended one.

So a dual-role user is anyone who satisfies `_is_organizer` *and* has at least
one `TeamMembership`.

## View routing

`dashboard_view` computes an `effective_view` and passes it to the template.
There is no redirect.

```
_is_organizer(user) and user.memberships.exists()  ->  has_dual_roles = True
                    |
                    v
dashboard_view computes effective_view:
    has_dual_roles  -> session["view_mode"]   ('team' | 'organizer', default 'team')
    organizer only  -> 'organizer'
    otherwise       -> 'team'
                    |
                    v
partials/dashboard_content.html renders the matching blocks (no redirect)
```

The template gates on `effective_view`:

```django
{% if effective_view == 'organizer' %} ... {% endif %}
{% if team and effective_view == 'team' %} ... {% endif %}
```

`view_mode` is passed to the context too, but only the ribbon uses it — to
decide which way the toggle button should point.

## Toggle mechanism

Route (`core/urls.py`):

```python
path("toggle-view/", views.toggle_view_preference, name="toggle_view_preference"),
```

View (`core/views.py`):

```python
@login_required
def toggle_view_preference(request):
    if not _has_dual_roles(request.user):
        messages.error(request, "This action is only available for users with dual roles.")
        return redirect("dashboard")

    current_mode = request.session.get("view_mode", "team")
    new_mode = "organizer" if current_mode == "team" else "team"
    request.session["view_mode"] = new_mode
    log_action(request, "view_mode_toggled", f"View mode switched to '{new_mode}'")
    return redirect("dashboard")
```

Ribbon control (`templates/core/base.html`), shown only to dual-role users:

```django
{% if has_dual_roles %}
<a href="{% url 'toggle_view_preference' %}" class="btn btn-outline btn-sm" title="Switch view mode">
    {% if view_mode == 'organizer' %}
    👤 Team View
    {% else %}
    ⚙️ Organizer View
    {% endif %}
</a>
{% endif %}
```

Note that this is a `GET` link, so the toggle is not CSRF-protected. It changes
only a presentation preference in the session, which is why that is tolerable;
do not extend this view to do anything else without converting it to `POST`.

## Session state

| | |
|---|---|
| Key | `request.session["view_mode"]` |
| Values | `"team"` (default) or `"organizer"` |
| Scope | Per session — not persisted to the user record |
| Default | `"team"` when unset, or whenever the user is not dual-role |

`_tournament_context` also reads the same key, so a toggled preference applies
consistently to the contexts built from it.

## Audit logging

Every toggle is recorded:

```
Action:  "view_mode_toggled"
Details: "View mode switched to 'organizer'"  (or "'team'")
```

## Edge cases

1. **Organizer with no team membership** — `_has_dual_roles` is `False`, no
   toggle button, `effective_view` is always `'organizer'`.
2. **Team member who is not an organizer** — no toggle button,
   `effective_view` is always `'team'`.
3. **New session** — `view_mode` is unset, so the default `'team'` applies.
4. **Direct `GET /toggle-view/` by a non-dual-role user** — the view rejects it
   with an error message and redirects to the dashboard; the session is not
   modified.
5. **Anonymous access** — `@login_required` redirects to the login page.
6. **Organizer access revoked while `view_mode == 'organizer'`** — the stale
   session key is harmless: `has_dual_roles` becomes `False`, so
   `effective_view` is recomputed from the user's actual roles and the
   organizer blocks stop rendering.

## Trying it out

Make a user dual-role by giving them a verified organizer profile *and* a team
membership:

```python
# python manage.py shell
from django.contrib.auth.models import User
from core.models import OrganizerProfile

user = User.objects.get(username="someone")
OrganizerProfile.objects.update_or_create(user=user, defaults={"verified": True})
# ... and make sure the user has at least one TeamMembership.
```

Verified organizer status can also be granted through the Settings page by an
existing site admin.

`scripts/promote_t2p1.py` does the same thing the legacy way — it sets
`is_staff` on the hardcoded user `t2p1`. It still works, because `_is_organizer`
accepts `is_staff`, but it grants Django admin access as a side effect and is
kept only for reference. `scripts/verify_dual_role_toggle.py` prints the
detection result for existing users.

## Coverage

`core/tests_dual_role.py` covers detection, the default view, both toggle
directions, rejection for non-dual-role users, and the revoked-organizer case.
`_is_organizer` itself is covered more broadly in `core/tests_authorization.py`.
