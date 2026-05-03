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

    function getCookie(name) {
        const value = `; ${document.cookie}`;
        const parts = value.split(`; ${name}=`);
        if (parts.length === 2) {
            return parts.pop().split(';').shift();
        }
        return null;
    }

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

    const estimateEndDateButton = document.getElementById('estimate-end-date-button');
    if (estimateEndDateButton) {
        const form = document.getElementById('court-availability-form');
        const resultContainer = document.getElementById('availability-estimate-result');
        estimateEndDateButton.addEventListener('click', function() {
            if (!form) {
                return;
            }

            resultContainer.style.color = '#333';
            resultContainer.textContent = 'Estimating end date...';
            const formData = new FormData(form);
            fetch(this.dataset.estimateUrl, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': getCookie('csrftoken') || '',
                },
                body: formData,
            })
                .then(function(response) {
                    return response.json().then(function(data) {
                        return {status: response.status, body: data};
                    });
                })
                .then(function(result) {
                    const data = result.body;
                    if (data.status === 'ok') {
                        resultContainer.style.color = '#155724';
                        if (data.estimated_end_date) {
                            const endDateInput = form.querySelector('[name="end_date"]');
                            if (endDateInput) {
                                endDateInput.value = data.estimated_end_date;
                            }
                        }
                        resultContainer.textContent = data.message;
                    } else {
                        resultContainer.style.color = '#856404';
                        resultContainer.textContent = data.message || 'Could not estimate an end date.';
                    }
                })
                .catch(function() {
                    resultContainer.style.color = '#856404';
                    resultContainer.textContent = 'Could not calculate the end date. Please try again.';
                });
        });
    }

    function formatMinutes(minutes) {
        var hours = Math.floor(minutes / 60);
        var mins = minutes % 60;
        return String(hours).padStart(2, '0') + ':' + String(mins).padStart(2, '0');
    }

    function parseTimeValue(value) {
        if (!value || typeof value !== 'string') {
            return null;
        }
        var parts = value.split(':');
        if (parts.length !== 2) {
            return null;
        }
        var hours = parseInt(parts[0], 10);
        var minutes = parseInt(parts[1], 10);
        if (Number.isNaN(hours) || Number.isNaN(minutes)) {
            return null;
        }
        return hours * 60 + minutes;
    }

    function buildMatchSlotRows() {
        var form = document.getElementById('court-availability-form');
        if (!form) {
            return;
        }

        var countInput = form.querySelector('[name="matches_per_court_per_day"]');
        var firstStartHidden = form.querySelector('[name="start_time"]');
        var defaultDuration = parseInt(form.dataset.defaultMatchDuration || '35', 10);
        var container = document.getElementById('match-slots-container');
        var section = document.getElementById('explicit-match-slots-section');
        if (!countInput || !firstStartHidden || !container || !section) {
            return;
        }

        var desiredCount = Math.max(1, parseInt(countInput.value, 10) || 1);
        section.style.display = '';

        var existingStarts = Array.from(container.querySelectorAll('input[name="match_start"]')).map(function(input) {
            return input.value || '';
        });
        var existingEnds = Array.from(container.querySelectorAll('input[name="match_end"]')).map(function(input) {
            return input.value || '';
        });

        container.innerHTML = '';
        var previousEndMinutes = null;

        for (var i = 1; i <= desiredCount; i += 1) {
            var startValue = existingStarts[i - 1] || '';
            var endValue = existingEnds[i - 1] || '';
            if (i === 1 && !startValue) {
                startValue = firstStartHidden.value || '';
            }
            if (startValue && !endValue) {
                var startMinutes = parseTimeValue(startValue);
                if (startMinutes !== null) {
                    endValue = formatMinutes(startMinutes + defaultDuration);
                }
            }
            if (i > 1 && !startValue && previousEndMinutes !== null) {
                startValue = formatMinutes(previousEndMinutes);
                if (!endValue) {
                    endValue = formatMinutes(previousEndMinutes + defaultDuration);
                }
            }
            if (endValue) {
                var endMinutes = parseTimeValue(endValue);
                if (endMinutes !== null) {
                    previousEndMinutes = endMinutes;
                }
            }

            var slotRow = document.createElement('div');
            slotRow.className = 'grid grid-2';
            slotRow.style.gap = '0.5rem';

            var startGroup = document.createElement('div');
            startGroup.className = 'form-group';
            var startLabel = document.createElement('label');
            startLabel.textContent = 'Match ' + i + ' start';
            var startInput = document.createElement('input');
            startInput.type = 'time';
            startInput.name = 'match_start';
            startInput.className = 'form-control';
            startInput.value = startValue;
            startInput.autocomplete = 'off';
            startInput.addEventListener('input', function() {
                var firstMatchInput = container.querySelector('input[name="match_start"]');
                if (firstMatchInput) {
                    firstStartHidden.value = firstMatchInput.value;
                }
            });
            startGroup.appendChild(startLabel);
            startGroup.appendChild(startInput);

            var endGroup = document.createElement('div');
            endGroup.className = 'form-group';
            var endLabel = document.createElement('label');
            endLabel.textContent = 'Match ' + i + ' end';
            var endInput = document.createElement('input');
            endInput.type = 'time';
            endInput.name = 'match_end';
            endInput.className = 'form-control';
            endInput.value = endValue;
            endInput.autocomplete = 'off';
            endGroup.appendChild(endLabel);
            endGroup.appendChild(endInput);

            slotRow.appendChild(startGroup);
            slotRow.appendChild(endGroup);
            container.appendChild(slotRow);
        }
    }

    var courtAvailabilityFormElement = document.getElementById('court-availability-form');
    if (courtAvailabilityFormElement) {
        courtAvailabilityFormElement.dataset.defaultMatchDuration = courtAvailabilityFormElement.dataset.defaultMatchDuration || '35';
        var dayCountInput = courtAvailabilityFormElement.querySelector('[name="matches_per_court_per_day"]');
        var hiddenStartInput = courtAvailabilityFormElement.querySelector('[name="start_time"]');
        if (dayCountInput) {
            dayCountInput.addEventListener('input', buildMatchSlotRows);
            dayCountInput.addEventListener('change', buildMatchSlotRows);
        }
        if (hiddenStartInput) {
            hiddenStartInput.addEventListener('input', buildMatchSlotRows);
        }
        buildMatchSlotRows();
    }

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
                        // Flicker-free swap: only replace when new content is ready.
                        // Build off-screen, then do a single atomic innerHTML assignment
                        // so there is never a blank frame between old and new content.
                        const trimmed = html.trim();
                        if (trimmed && trimmed !== section.innerHTML.trim()) {
                            section.innerHTML = trimmed;
                        }
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

// ── Fireworks Animation for Tournament Celebration ──────────────────────────
function startFireworks() {
    const canvas = document.getElementById('fireworks-canvas');
    if (!canvas) return;

    const container = canvas.parentElement;
    if (!container) return;

    canvas.style.display = 'block';
    const ctx = canvas.getContext('2d');
    canvas.width = container.clientWidth;
    canvas.height = container.clientHeight;

    const particles = [];
    const colors = ['#FFD700', '#FFA500', '#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEAA7'];

    class Particle {
        constructor(x, y) {
            this.x = x;
            this.y = y;
            this.size = Math.random() * 3 + 2;
            this.speedX = (Math.random() - 0.5) * 8;
            this.speedY = (Math.random() - 0.5) * 8 - 3;
            this.gravity = 0.1;
            this.alpha = 1;
            this.color = colors[Math.floor(Math.random() * colors.length)];
        }

        draw() {
            ctx.globalAlpha = this.alpha;
            ctx.fillStyle = this.color;
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
        }

        update() {
            this.x += this.speedX;
            this.y += this.speedY;
            this.speedY += this.gravity;
            this.alpha -= 0.015;
            this.speedX *= 0.99;
        }
    }

    function createFireworks(x, y) {
        for (let i = 0; i < 30; i++) {
            particles.push(new Particle(x, y));
        }
    }

    // Create multiple fireworks bursts
    const burstLocations = [
        [canvas.width * 0.5, canvas.height * 0.3],
        [canvas.width * 0.3, canvas.height * 0.5],
        [canvas.width * 0.7, canvas.height * 0.5],
        [canvas.width * 0.4, canvas.height * 0.2],
        [canvas.width * 0.6, canvas.height * 0.6]
    ];

    let burstIndex = 0;
    let frameCount = 0;

    function animate() {
        ctx.globalAlpha = 1;
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // Create new bursts
        if (frameCount % 15 === 0 && burstIndex < burstLocations.length) {
            createFireworks(burstLocations[burstIndex][0], burstLocations[burstIndex][1]);
            burstIndex++;
        }

        // Update and draw particles
        for (let i = particles.length - 1; i >= 0; i--) {
            particles[i].update();
            particles[i].draw();
            if (particles[i].alpha <= 0) {
                particles.splice(i, 1);
            }
        }

        frameCount++;

        // Continue animation until all particles are gone and bursts are complete
        if (particles.length > 0 || burstIndex < burstLocations.length) {
            requestAnimationFrame(animate);
        } else {
            canvas.style.display = 'none';
        }
    }

    animate();
}
