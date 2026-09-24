'use strict';
const panel = document.querySelector('[data-job-status]');
async function refreshJob() {
    try {
        const response = await fetch(panel.dataset.jobStatus, {cache: 'no-store'});
        if (!response.ok) return;
        const job = await response.json();
        document.getElementById('job-state').textContent = job.label;
        document.getElementById('job-progress').value = job.progress;
        document.getElementById('job-percent').textContent = job.progress;
        document.getElementById('job-file').textContent = job.filename;
        if (job.state === 'failed') {
            const error = document.getElementById('job-error');
            error.textContent = job.error;
            error.hidden = false;
            return;
        }
        if (job.state === 'done') {
            const link = document.getElementById('job-result');
            link.href = job.target;
            link.textContent = job.open_label;
            window.location.assign(job.target);
            return;
        }
        setTimeout(refreshJob, 800);
    } catch (_) {
        // Preserve the current progress; retry when the local server is reachable.
        setTimeout(refreshJob, 2000);
    }
}
if (panel) refreshJob();
