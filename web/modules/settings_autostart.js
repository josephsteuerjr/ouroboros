import { apiClient } from './api_client.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { setInlineStatus } from './ui_primitives.js';

/** Registry-backed control: independent of the settings.json draft and Save button. */
export function bindAutostartControl(page) {
    const section = page.querySelector('[data-autostart-section]');
    const checkbox = page.querySelector('#windows-autostart');
    const status = page.querySelector('#windows-autostart-status');
    let generation = 0;
    let busy = false;
    let disposed = false;
    async function refresh() {
        const current = ++generation;
        try {
            const value = await apiClient.ownerAutostart();
            if (disposed || current !== generation) return;
            section.hidden = !value.available;
            checkbox.checked = !!value.enabled;
            checkbox.disabled = false;
            setInlineStatus(status, '', 'muted');
        } catch (error) {
            if (disposed || current !== generation) return;
            section.hidden = false;
            checkbox.disabled = true;
            setInlineStatus(status, `Startup status unavailable: ${error.message}`, 'warn');
        }
    }
    async function change() {
        if (busy || disposed) return;
        const desired = checkbox.checked;
        checkbox.checked = !desired;
        generation++; // A pre-click GET cannot unlock the control during confirmation.
        busy = true;
        checkbox.disabled = true;
        try {
            const confirmed = await openConfirmDialog({
                title: desired ? 'Run at Windows logon' : 'Stop running at Windows logon',
                body: desired ? 'Add Ouroboros to your Windows startup apps?' : 'Remove Ouroboros from your Windows startup apps?',
                confirmLabel: desired ? 'Enable autostart' : 'Disable autostart',
            });
            if (!confirmed || disposed) { if (!disposed) await refresh(); return; }
            await apiClient.setOwnerAutostart(desired);
            await refresh();
        } catch (error) {
            await refresh();
            if (!disposed) setInlineStatus(status, `Startup change failed: ${error.message}`, 'warn');
        } finally {
            busy = false;
        }
    }
    checkbox.addEventListener('change', change);
    void refresh();
    return { refresh: () => { if (!busy) void refresh(); }, dispose: () => {
        disposed = true;
        generation++;
        checkbox.removeEventListener('change', change);
    } };
}
