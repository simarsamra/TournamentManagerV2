// ── Theme system ──────────────────────────────────────────────────────────
// (theme is applied early via inline <script> in <head> to avoid flash)

// Sidebar toggle for mobile
document.addEventListener('DOMContentLoaded', function() {

    // Theme select
    var themeSelect = document.getElementById('themeSelect');
    if (themeSelect) {
        themeSelect.value = localStorage.getItem('tm-theme') || 'default';
        themeSelect.addEventListener('change', function() {
            var theme = this.value;
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('tm-theme', theme);
        });
    }

    const toggle = document.querySelector('.menu-toggle');
    const sidebar = document.querySelector('.sidebar');
    if (toggle && sidebar) {
        toggle.addEventListener('click', function() {
            sidebar.classList.toggle('open');
        });
        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', function(e) {
            if (window.innerWidth <= 768 && sidebar.classList.contains('open') 
                && !sidebar.contains(e.target) && !toggle.contains(e.target)) {
                sidebar.classList.remove('open');
            }
        });
    }

    // Auto-dismiss alerts after 5 seconds
    document.querySelectorAll('.alert').forEach(function(alert) {
        setTimeout(function() {
            alert.style.transition = 'opacity 0.3s';
            alert.style.opacity = '0';
            setTimeout(function() { alert.remove(); }, 300);
        }, 5000);
    });

    // Filter form auto-submit
    document.querySelectorAll('.filters select').forEach(function(select) {
        select.addEventListener('change', function() {
            this.closest('form').submit();
        });
    });

    // Bulk checkbox shortcuts for availability forms
    document.querySelectorAll('[data-check-target]').forEach(function(button) {
        button.addEventListener('click', function() {
            const target = this.getAttribute('data-check-target');
            const mode = this.getAttribute('data-check-mode');
            const container = document.querySelector('[data-check-group="' + target + '"]');
            if (!container) {
                return;
            }

            container.querySelectorAll('input[type="checkbox"]').forEach(function(checkbox) {
                if (mode === 'all') {
                    checkbox.checked = true;
                } else if (mode === 'none') {
                    checkbox.checked = false;
                } else if (mode === 'weekdays') {
                    checkbox.checked = ['0', '1', '2', '3', '4'].includes(checkbox.value);
                } else if (mode === 'weekend') {
                    checkbox.checked = ['5', '6'].includes(checkbox.value);
                }
            });
        });
    });

    // Live registration review summary
    const registrationForm = document.querySelector('#registration-form');
    if (registrationForm) {
        const teamInput = registrationForm.querySelector('[name="team_name"]');
        const usernameInput = registrationForm.querySelector('[name="username"]');
        const departmentInput = registrationForm.querySelector('[name="department"]');
        const playersInput = registrationForm.querySelector('[name="player_names"]');
        const preferredInputs = registrationForm.querySelectorAll('[name="preferred_courts"]');

        const updateRegistrationPreview = function() {
            const teamTarget = document.querySelector('[data-preview="team_name"]');
            const usernameTarget = document.querySelector('[data-preview="username"]');
            const departmentTarget = document.querySelector('[data-preview="department"]');
            const playerCountTarget = document.querySelector('[data-preview="player_count"]');
            const courtsTarget = document.querySelector('[data-preview="preferred_courts"]');

            if (teamTarget) {
                teamTarget.textContent = teamInput && teamInput.value.trim() ? teamInput.value.trim() : '-';
            }
            if (usernameTarget) {
                usernameTarget.textContent = usernameInput && usernameInput.value.trim() ? usernameInput.value.trim() : '-';
            }
            if (departmentTarget) {
                departmentTarget.textContent = departmentInput && departmentInput.value.trim() ? departmentInput.value.trim() : '-';
            }
            if (playerCountTarget) {
                const players = playersInput && playersInput.value
                    ? playersInput.value.split('\n').map(function(name) { return name.trim(); }).filter(Boolean)
                    : [];
                playerCountTarget.textContent = String(players.length);
            }
            if (courtsTarget) {
                const selectedCourts = Array.from(preferredInputs)
                    .filter(function(input) { return input.checked; })
                    .map(function(input) {
                        const label = registrationForm.querySelector('label[for="' + input.id + '"]');
                        return label ? label.textContent.trim() : input.value;
                    });
                courtsTarget.textContent = selectedCourts.length ? selectedCourts.join(', ') : 'None selected';
            }
        };

        [teamInput, usernameInput, departmentInput, playersInput].forEach(function(input) {
            if (input) {
                input.addEventListener('input', updateRegistrationPreview);
            }
        });
        preferredInputs.forEach(function(input) {
            input.addEventListener('change', updateRegistrationPreview);
        });
        updateRegistrationPreview();
    }

    // Lightweight live section refresh
    document.querySelectorAll('[data-auto-refresh="true"]').forEach(function(section) {
        const intervalMs = parseInt(section.getAttribute('data-refresh-interval') || '15000', 10);
        let refreshing = false;

        const refreshSection = function() {
            if (document.hidden || refreshing || !section.isConnected) {
                return;
            }

            const active = document.activeElement;
            if (active && section.contains(active) && ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(active.tagName)) {
                return;
            }

            const url = new URL(window.location.href);
            url.searchParams.set('partial', '1');
            refreshing = true;

            fetch(url.toString(), {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                cache: 'no-store'
            })
                .then(function(response) {
                    if (!response.ok) {
                        throw new Error('Auto refresh failed');
                    }
                    return response.text();
                })
                .then(function(html) {
                    if (html && section.isConnected) {
                        section.innerHTML = html;
                    }
                })
                .catch(function() {
                    // Silent fail to avoid interrupting the user experience.
                })
                .finally(function() {
                    refreshing = false;
                });
        };

        window.setInterval(refreshSection, intervalMs);
    });
});
