document.querySelectorAll('[data-send-otp]').forEach((button) => {
    button.addEventListener('click', async () => {
        const form = button.closest('form');
        const recipientInput = button.dataset.emailInput || button.dataset.mobileInput;
        const recipient = form?.querySelector(`#${recipientInput}`)?.value.trim();
        const csrf = form?.querySelector('[name="csrfmiddlewaretoken"]')?.value;
        const status = document.getElementById(button.dataset.statusId);
        if (!recipient || !csrf || !status) return;

        button.disabled = true;
        status.textContent = 'Sending OTP…';
        try {
            const payload = new FormData();
            if (button.dataset.emailInput) {
                payload.set('email', recipient);
            } else {
                payload.set('mobile_number', recipient);
                payload.set('purpose', button.dataset.otpPurpose);
            }
            const response = await fetch(button.dataset.otpUrl, {
                method: 'POST',
                body: payload,
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': csrf },
            });
            const result = await response.json();
            status.textContent = result.development_code
                ? `${result.message} Development OTP: ${result.development_code}`
                : result.message;
            status.classList.toggle('is-error', !response.ok);
        } catch {
            status.textContent = 'Could not send OTP. Check your connection and try again.';
            status.classList.add('is-error');
        } finally {
            button.disabled = false;
        }
    });
});