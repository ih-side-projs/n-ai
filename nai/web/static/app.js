// N-AI Dashboard JavaScript (WP8)

document.addEventListener('DOMContentLoaded', () => {
  // SSE log streaming
  const logStream = document.getElementById('log-stream');
  if (logStream) {
    const projectId = logStream.dataset.project;
    const jobId = logStream.dataset.job;
    let lastId = 0;
    let eventSource = null;

    function connect() {
      const url = `/projects/${projectId}/logs/stream${jobId ? '?job_id=' + jobId : ''}`;
      eventSource = new EventSource(url);
      
      eventSource.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data);
          if (event.id > lastId) {
            lastId = event.id;
            appendLogEntry(event);
          }
        } catch (err) {
          console.error('Failed to parse SSE event:', err);
        }
      };
      
      eventSource.onerror = () => {
        eventSource.close();
        setTimeout(connect, 5000);
      };
    }

    function appendLogEntry(event) {
      const entry = document.createElement('div');
      entry.className = 'log-entry';
      entry.dataset.id = event.id;
      entry.innerHTML = `
        <span class="log-time">${event.time}</span>
        <span class="log-level status-${event.level}">${event.level}</span>
        <span class="log-message">${escapeHtml(event.message)}</span>
        ${event.data ? `<pre class="log-data">${escapeHtml(JSON.stringify(event.data, null, 2))}</pre>` : ''}
      `;
      logStream.appendChild(entry);
      logStream.scrollTop = logStream.scrollHeight;
    }

    function escapeHtml(text) {
      const div = document.createElement('div');
      div.textContent = text;
      return div.innerHTML;
    }

    const clearBtn = document.getElementById('clear-logs');
    if (clearBtn) {
      clearBtn.addEventListener('click', () => {
        logStream.innerHTML = '';
        lastId = 0;
      });
    }

    connect();
  }

  // Tool try forms
  document.querySelectorAll('.tool-try-form').forEach(form => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      const resultDiv = form.parentElement.querySelector('.tool-result');
      btn.disabled = true;
      btn.textContent = 'Running...';

      const formData = new FormData(form);
      try {
        const response = await fetch('/tools/call', { method: 'POST', body: formData });
        const data = await response.json();
        const pre = document.createElement('pre');
        pre.className = 'code-block';
        pre.textContent = JSON.stringify(data, null, 2);
        resultDiv.replaceChildren(pre);
        resultDiv.className = 'tool-result ' + (data.isError ? 'error' : 'success');
      } catch (err) {
        const pre = document.createElement('pre');
        pre.className = 'code-block error-output';
        pre.textContent = 'Error: ' + err.message;
        resultDiv.replaceChildren(pre);
        resultDiv.className = 'tool-result error';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Execute';
      }
    });
  });

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // Native dialog handling for the structured new-project form.
  document.querySelectorAll('[data-open-dialog]').forEach(button => {
    button.addEventListener('click', () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (dialog && typeof dialog.showModal === 'function') {
        dialog.showModal();
      }
    });
  });
  document.querySelectorAll('[data-close-dialog]').forEach(button => {
    button.addEventListener('click', () => {
      const dialog = button.closest('dialog');
      if (dialog && typeof dialog.close === 'function') {
        dialog.close();
      }
    });
  });

  // Submit ordinary POST forms through fetch with a custom X-Requested-With
  // header. This preserves CSRF protection while allowing browsers/privacy
  // extensions that send `Origin: null` to use the dashboard.
  document.querySelectorAll('form[method="post"]').forEach(form => {
    if (form.classList.contains('control-form') || form.classList.contains('tool-try-form')) {
      return;
    }
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        return;
      }
      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          headers: { 'X-Requested-With': 'fetch' }
        });
        if (!response.ok) {
          const type = response.headers.get('content-type') || '';
          let detail = response.statusText;
          if (type.includes('json')) {
            const data = await response.json();
            detail = data.detail || detail;
          } else {
            detail = (await response.text()).slice(0, 300);
          }
          window.alert('Request failed: ' + detail);
          return;
        }
        if (response.redirected) {
          window.location.href = response.url;
        } else {
          window.location.reload();
        }
      } catch (err) {
        window.alert('Request failed: ' + err.message);
      }
    });
  });

  // Project control forms return JSON. Submit them via fetch and reload the page
  // instead of letting the browser navigate to a bare JSON document.
  document.querySelectorAll('form.control-form').forEach(form => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        return;
      }
      const button = form.querySelector('button[type="submit"]');
      const originalText = button ? button.textContent : '';
      if (button) {
        button.disabled = true;
        button.textContent = 'Working...';
      }
      try {
        const response = await fetch(form.action, {
          method: 'POST',
          body: new FormData(form),
          headers: { 'Accept': 'application/json', 'X-Requested-With': 'fetch' }
        });
        if (!response.ok) {
          let detail = response.statusText;
          try {
            const data = await response.json();
            detail = data.detail || detail;
          } catch (_) { /* non-JSON error */ }
          throw new Error(detail);
        }
        window.location.reload();
      } catch (err) {
        window.alert('Command failed: ' + err.message);
        if (button) {
          button.disabled = false;
          button.textContent = originalText;
        }
      }
    });
  });
});