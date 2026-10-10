// The composer's controls that write GLOBAL owner settings (docs/DESIGN.md "Composer
// owner controls"): the Nano/Low/Max context mode and the effort range. Both save
// through their audited owner endpoint, show the server's refusal as a toast, and
// resync every open composer from the next `/api/state` snapshot. Swarm is not one of
// them: it is a per-message flag that never leaves the frame it rides.

/**
 * @param {object} deps
 * @param {(suffix: string) => Element|null} deps.byId  the instance-namespaced lookup
 * @param {typeof fetch} deps.apiFetch
 * @param {(message: string, tone?: string) => void} deps.showToast
 * @param {(force?: boolean) => Promise<void>|void} deps.refreshState  a forced `/api/state` re-read
 */
export function createComposerOwnerControls({ byId, apiFetch, showToast, refreshState }) {
    // Context-mode quick toggle: the owner endpoint hot-applies the setting
    // without a restart; Max -> Low is accepted only while Ouroboros is idle.
    const contextModeBtn = byId('context-mode');
    const onContextMode = async (event) => {
        const seg = event.target.closest('.chat-seg');
        if (!seg || contextModeBtn.dataset.disabled === 'true') return;
        const next = ['nano', 'low', 'max'].includes(seg.dataset.mode) ? seg.dataset.mode : 'max';
        const current = ['nano', 'low', 'max'].includes(contextModeBtn.dataset.contextMode) ? contextModeBtn.dataset.contextMode : 'max';
        if (next === current) return;
        contextModeBtn.dataset.disabled = 'true';
        const postMode = (mode) => apiFetch('/api/owner/context-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode }),
        });
        try {
            const resp = await postMode(next);
            if (resp.ok) {
                contextModeBtn.dataset.contextMode = next;
            } else {
                let message = 'Could not change context mode.';
                try { const p = await resp.json(); if (p?.error) message = p.error; } catch {}
                showToast(message, 'error');
            }
        } catch (e) {
            showToast(`Could not change context mode: ${e.message || e}`, 'error');
            /* leave the current value; /api/state refresh will resync */
        } finally {
            contextModeBtn.dataset.disabled = 'false';
            refreshState(true);
        }
    };
    contextModeBtn?.addEventListener('click', onContextMode);

    return {
        destroy() {
            contextModeBtn?.removeEventListener('click', onContextMode);
        },
    };
}
