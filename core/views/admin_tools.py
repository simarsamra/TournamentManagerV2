"""Site-administrator tools: settings, user management and impersonation."""
"""Core views for tournament management."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..models import OrganizerApplication, OrganizerProfile
from ..forms import TournamentForm
from ..audit import log_action

from .helpers import (
    _auto_end_date,
    _can_manage_tournament,
    _get_tournament,
    _is_htmx_request,
    _is_organizer,
    _is_site_admin,
    _organizer_count,
    _render_refreshable_page,
    _tournament_context,
)



@login_required
def settings_view(request):
    if not _is_organizer(request.user):
        return redirect("dashboard")
    tournament = _get_tournament(request)
    if not tournament:
        return _render_refreshable_page(
            request,
            "core/settings.html",
            "core/partials/settings_content.html",
            _tournament_context(request, tournament),
        )
    is_settings_locked = bool(tournament.started_at or tournament.status in ("active", "completed"))
    if request.method == "POST":
        if not _can_manage_tournament(request.user, tournament):
            messages.error(request, "You do not manage that tournament.")
            return redirect("dashboard")
        if is_settings_locked:
            messages.error(request, "Tournament settings are locked after the tournament has started.")
            return redirect("settings")
        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            t = form.save(commit=False)
            if not t.end_date and t.start_date:
                t.end_date = _auto_end_date(t)
            t.save()
            log_action(request, "settings_updated", "Tournament settings updated", tournament=tournament)
            messages.success(request, "Settings updated.")
            return redirect("settings")
    else:
        form = TournamentForm(instance=tournament)
    context = {
        "tournament": tournament,
        "form": form,
        "is_settings_locked": is_settings_locked,
        "users": (
            User.objects.filter(is_superuser=False).order_by("username")
            if _is_site_admin(request.user)
            else User.objects.none()
        ),
        "organizer_applications": (
            OrganizerApplication.objects.order_by("-created_at")
            if _is_site_admin(request.user)
            else OrganizerApplication.objects.none()
        ),
        "is_site_admin": _is_site_admin(request.user),
        **_tournament_context(request, tournament),
    }
    return _render_refreshable_page(
        request,
        "core/settings.html",
        "core/partials/settings_content.html",
        context,
    )


# -- User Management --

@login_required
@require_POST
def set_user_organizer(request, user_pk):
    if not _is_site_admin(request.user):
        messages.error(request, "Only site administrators can manage user accounts.")
        return redirect("settings")
    target = get_object_or_404(User, pk=user_pk)
    if target.is_superuser:
        messages.error(request, "Superuser accounts cannot be modified here.")
        return redirect("settings")

    role_value = request.POST.get("is_organizer")
    if role_value not in {"0", "1"}:
        messages.error(request, "Invalid organizer role update request.")
        return redirect("settings")
    make_organizer = role_value == "1"
    if not make_organizer:
        organizer_count = _organizer_count(exclude_user_id=target.pk)
        if organizer_count < 1:
            messages.error(request, "At least one organizer account is required.")
            return redirect("settings")
    
    from ..models import OrganizerProfile
    org_profile, _ = OrganizerProfile.objects.get_or_create(user=target)
    org_profile.verified = make_organizer
    org_profile.save(update_fields=["verified"])

    action = "user_promoted_to_organizer" if make_organizer else "user_demoted_from_organizer"
    detail = f"User '{target.username}' role updated to {'organizer' if make_organizer else 'user'}."
    log_action(request, action, detail)
    messages.success(request, detail)
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("settings")})
    return redirect("settings")


@login_required
@require_POST
def delete_user_account(request, user_pk):
    if not _is_site_admin(request.user):
        messages.error(request, "Only site administrators can manage user accounts.")
        return redirect("settings")
    target = get_object_or_404(User, pk=user_pk)
    if target == request.user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("settings")
    if target.is_superuser:
        messages.error(request, "Superuser accounts cannot be deleted here.")
        return redirect("settings")
    
    from ..models import OrganizerProfile
    if hasattr(target, 'organizer_profile') and target.organizer_profile.verified:
        organizer_count = _organizer_count(exclude_user_id=target.pk)
        if organizer_count < 1:
            messages.error(request, "At least one organizer account is required.")
            return redirect("settings")

    username = target.username
    target.delete()
    log_action(request, "user_deleted", f"User '{username}' account deleted.")
    messages.success(request, f"User '{username}' deleted.")
    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("settings")})
    return redirect("settings")


# =============================================================================
# FLOW 4 — Suspend/unsuspend user (11.2)
# =============================================================================

@require_POST
@login_required
def toggle_user_suspension(request, user_pk):
    """Suspend or unsuspend a user account (11.2)."""
    if not _is_site_admin(request.user):
        messages.error(request, "Only site administrators can manage user accounts.")
        return redirect("settings")

    target = get_object_or_404(User, pk=user_pk)

    if target == request.user:
        messages.error(request, "You cannot suspend yourself.")
        return redirect("settings")

    if target.is_superuser:
        messages.error(request, "Cannot suspend a superuser.")
        return redirect("settings")

    if target.is_active:
        target.is_active = False
        target.save(update_fields=["is_active"])
        log_action(request, "user_suspended", f"User '{target.username}' suspended")
        messages.success(request, f"User '{target.username}' has been suspended.")
    else:
        target.is_active = True
        target.save(update_fields=["is_active"])
        log_action(request, "user_unsuspended", f"User '{target.username}' unsuspended")
        messages.success(request, f"User '{target.username}' has been unsuspended.")

    if _is_htmx_request(request):
        return HttpResponse(status=204, headers={"HX-Redirect": reverse("settings")})
    return redirect("settings")


# =============================================================================
# FLOW 5 — Impersonate user (11.7)
# =============================================================================

@login_required
@require_POST
def impersonate_user(request, user_pk):
    """Admin can impersonate any user for debugging (11.7)."""
    if not request.user.is_superuser:
        messages.error(request, "Only admins can impersonate users.")
        return redirect("settings")
    target = get_object_or_404(User, pk=user_pk)
    if target.is_superuser:
        messages.error(request, "Cannot impersonate a superuser.")
        return redirect("settings")
    # Store original user pk before switching
    original_pk = request.user.pk
    request.session["impersonating_original_user_pk"] = original_pk
    # Store the admin's session auth hash as it is *now*, so stop_impersonating
    # restores exactly the session that was suspended rather than minting a
    # fresh one. If the admin's password is rotated while the impersonation is
    # running, this stale hash no longer matches get_session_auth_hash() and
    # Django flushes the session on the next request — the admin has to log in
    # again. That is deliberate: a credential rotation must not be survivable
    # by resuming a suspended session.
    request.session["impersonating_original_hash"] = request.user.get_session_auth_hash()
    # Switch session to target user (manually update Django's internal session keys)
    request.session["_auth_user_id"] = str(target.pk)
    request.session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    request.session["_auth_user_hash"] = target.get_session_auth_hash()
    log_action(request, "impersonation_started", f"Admin '{request.user.username}' impersonating '{target.username}'")
    messages.warning(request, f"You are now impersonating {target.username}. Click 'Stop Impersonating' to return.")
    return redirect("dashboard")


@require_POST
def stop_impersonating(request):
    """Stop impersonation and restore the original admin session.

    Deliberately not @login_required: the impersonated account may have been
    deactivated mid-session, and the admin still has to be able to get back
    out. Authorisation comes from the session key instead, which only
    impersonate_user (superuser-only, POST-only) can set.

    @require_POST is present so a third-party page cannot end an admin's
    impersonation by pointing them at this URL.
    """
    original_pk = request.session.get("impersonating_original_user_pk")
    if not original_pk:
        messages.info(request, "You are not impersonating anyone.")
        return redirect("dashboard")
    original_user = get_object_or_404(User, pk=original_pk)
    impersonated_user_id = request.session.get("_auth_user_id", "unknown")
    original_hash = request.session.pop("impersonating_original_hash", original_user.get_session_auth_hash())
    request.session["_auth_user_id"] = str(original_pk)
    request.session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
    request.session["_auth_user_hash"] = original_hash
    del request.session["impersonating_original_user_pk"]
    log_action(request, "impersonation_ended", f"Admin '{original_user.username}' stopped impersonating user pk={impersonated_user_id}")
    messages.success(request, "Impersonation ended.")
    return redirect("settings")
