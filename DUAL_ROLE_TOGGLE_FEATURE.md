# Dual-Role User Toggle Feature

## Overview
Users who are both organizers and team members (e.g., t2p1 after promotion) can now toggle between two views:
- **Team View**: Shows the team dashboard with match schedules, standings, and team-specific information
- **Organizer View**: Shows the tournament setup/management interface for creating and configuring tournaments

## How It Works

### 1. Detection
The system automatically detects dual-role users:
```python
_has_dual_roles(user) → returns True if user is BOTH:
  - is_staff=True (organizer)
  - Has at least one team membership
```

### 2. Default Behavior
When a dual-role user logs in:
- Default view is **Team View** (shows team dashboard)
- Toggle button appears in the top ribbon

### 3. Toggle Mechanism
- Located in top ribbon (right side, next to team name and logout)
- Button text changes based on current mode:
  - "⚙️ Organizer View" → when currently in Team View
  - "👤 Team View" → when currently in Organizer View
- Clicking the button:
  - Toggles `request.session['view_mode']` between 'team' and 'organizer'
  - Redirects to appropriate dashboard

### 4. View Routing
- **Team View**: Shows `core/dashboard.html` with team-focused content
- **Organizer View**: Redirects to `tournament_setup` view for tournament management

## Implementation Details

### Files Modified

#### 1. `core/views.py`
**New Helper Function:**
```python
def _has_dual_roles(user):
    """Check if user is both organizer and team member."""
    if not _is_organizer(user):
        return False
    return user.memberships.exists()
```

**New View:**
```python
@login_required
def toggle_view_preference(request):
    """Toggle between organizer and team view for dual-role users."""
    # Validates user has dual roles
    # Toggles request.session['view_mode'] between 'team' and 'organizer'
    # Logs the action for audit trail
    # Returns redirect to dashboard
```

**Modified Functions:**
- `dashboard_view()`: Added logic to check for dual-role users and redirect based on `view_mode` preference
- `_tournament_context()`: Now includes `has_dual_roles` and `view_mode` in context

#### 2. `core/urls.py`
**New Route:**
```python
path("toggle-view/", views.toggle_view_preference, name="toggle_view_preference"),
```

#### 3. `templates/core/base.html`
**New UI Component in Top Ribbon:**
```html
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

## Session Management

View mode preference is stored in Django session:
- **Key**: `request.session['view_mode']`
- **Values**: `'team'` (default) or `'organizer'`
- **Scope**: Per-session (persists until logout)
- **Default**: 'team' (when not set or user doesn't have dual roles)

## User Experience Flow

### Scenario: User t2p1 (Organizer + Team Captain)

1. **Login**
   - User logs in as t2p1
   - Session cleared (existing behavior)
   - Redirected to dashboard

2. **Initial Dashboard (Team View)**
   - Shows team-focused dashboard for Team 2
   - Top ribbon shows: "⚙️ Organizer View" toggle button
   - Can see team matches, standings, performance analytics

3. **Toggle to Organizer View**
   - Click "⚙️ Organizer View" button
   - `toggle_view_preference` view:
     - Sets `request.session['view_mode'] = 'organizer'`
     - Redirects to dashboard
   - Dashboard detects view_mode='organizer' and redirects to tournament_setup
   - Now seeing tournament management interface
   - Toggle button shows: "👤 Team View"

4. **Toggle Back to Team View**
   - Click "👤 Team View" button
   - Toggles back to team view
   - Returns to team dashboard

## Backend Logic Flow

```
User.is_staff=True + User.memberships.exists() → _has_dual_roles() = True
                    ↓
dashboard_view() checks: has_dual_roles && view_mode=='organizer'
                    ↓
NO  → Render team dashboard normally
YES → Redirect to tournament_setup
```

## Audit Logging

Each toggle action is logged to the audit trail:
```
Action: "view_mode_toggled"
Details: "View mode switched to 'organizer'" or "View mode switched to 'team'"
```

## Edge Cases Handled

1. **Non-dual-role organizer**: Toggle button not shown (only has is_staff, no teams)
2. **Non-dual-role team member**: Toggle button not shown (only has teams, not is_staff)
3. **Session expires**: Default view_mode is 'team' on new login
4. **Unauthorized access attempt**: `toggle_view_preference` validates dual-role status
5. **Direct URL access**: Both organizer and team views require authentication

## Testing the Feature

### Prerequisites
- User must be promoted to organizer: `user.is_staff = True`
- User must have team membership(s)

### Verify Feature
Run: `python manage.py shell < verify_dual_role_toggle.py`

### Promote User to Organizer
Run: `python promote_t2p1.py`
(Changes t2p1 to is_staff=True while retaining team memberships)

## Future Enhancements

Possible improvements:
1. Persist view preference to user profile (across sessions)
2. Remember last-viewed tournament when switching modes
3. Add breadcrumb showing current mode ("Team View" / "Organizer View")
4. Add tour/help for new dual-role users explaining the toggle
5. Keyboard shortcut to toggle view (e.g., Ctrl+Shift+V)
