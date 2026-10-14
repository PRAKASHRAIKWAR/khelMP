(() => {
    const list = document.querySelector('#chat-messages');
    const form = document.querySelector('#chat-form');
    if (!list || !form) return;

    const input = form.querySelector('textarea[name="text"]');
    const button = form.querySelector('button[type="submit"]');
    const status = document.querySelector('#chat-status');
    let lastId = Number(list.dataset.lastId) || 0;

    const appendMessage = (message) => {
        if (list.querySelector(`[data-message-id="${message.id}"]`)) return;
        const shouldScroll = list.scrollHeight - list.scrollTop - list.clientHeight < 96;
        list.querySelector('.chat-first-message')?.remove();
        const item = document.createElement('li');
        item.className = `chat-bubble${message.own ? ' is-own' : ''}`;
        item.dataset.messageId = message.id;
        const text = document.createElement('p');
        text.textContent = message.text;
        const time = document.createElement('time');
        time.textContent = message.time;
        item.append(text, time);
        list.append(item);
        lastId = Math.max(lastId, Number(message.id));
        list.dataset.lastId = String(lastId);
        if (shouldScroll) list.scrollTop = list.scrollHeight;
    };

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        button.disabled = true;
        status.textContent = '';
        try {
            const response = await fetch(form.action, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                    'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value,
                },
                body: new URLSearchParams({ text }),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.message || 'Message could not be sent.');
            appendMessage(result.message);
            input.value = '';
            input.focus();
        } catch (error) {
            status.textContent = error.message || 'Connection problem. Try again.';
        } finally {
            button.disabled = false;
        }
    });

    const refreshMessages = async () => {
        if (document.visibilityState !== 'visible') return;
        try {
            const separator = list.dataset.updatesUrl.includes('?') ? '&' : '?';
            const response = await fetch(`${list.dataset.updatesUrl}${separator}after=${lastId}`, {
                credentials: 'same-origin',
                headers: { 'Accept': 'application/json' },
            });
            if (!response.ok) return;
            const result = await response.json();
            result.messages.forEach(appendMessage);
        } catch {
            status.textContent = 'Reconnecting to chat…';
        }
    };

    list.scrollTop = list.scrollHeight;
    window.setInterval(refreshMessages, 3000);
})();