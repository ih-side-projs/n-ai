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
        resultDiv.innerHTML = `<pre class="code-block">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
        resultDiv.className = 'tool-result ' + (data.isError ? 'error' : 'success');
      } catch (err) {
        resultDiv.innerHTML = `<pre class="code-block error-output">Error: ${err.message}</pre>`;
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

  // Confirmation for destructive actions
  document.querySelectorAll('form[onsubmit]').forEach(form => {
    const originalSubmit = form.onsubmit;
    form.addEventListener('submit', (e) => {
      if (originalSubmit && !originalSubmit.call(form, e)) {
        e.preventDefault();
      }
    });
  });
});