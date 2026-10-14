document.querySelectorAll('[data-share-url]').forEach((button) => {
    button.addEventListener('click', async () => {
        const shareData = {
            title: 'KhelMP athlete update',
            text: button.dataset.shareText,
            url: button.dataset.shareUrl,
        };
        try {
            if (navigator.share) {
                await navigator.share(shareData);
                return;
            }
            await navigator.clipboard.writeText(shareData.url);
            button.textContent = 'Link copied';
        } catch (error) {
            if (error.name === 'AbortError') return;
            window.prompt('Copy this post link', shareData.url);
        }
    });
});