/**
 * equipo-comms.js — Admin UI for staff announcements and task checklist.
 *
 * Wires the two new sections added to equipo.html:
 *   • Anuncios del equipo  — CRUD via /api/staff/announcements
 *   • Tareas del turno     — CRUD via /api/staff/tasks + completions modal
 *
 * Security: all user-supplied strings are rendered via textContent (never innerHTML)
 * except for static structural HTML built exclusively from our own data shapes.
 */

(function () {
  'use strict';

  // ── Auth headers ──────────────────────────────────────────────────────────
  function _headers() {
    const token = localStorage.getItem('rb_token') || localStorage.getItem('rb_staff_token') || '';
    const branch = localStorage.getItem('rb_branch_id') || '';
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = 'Bearer ' + token;
    if (branch) h['X-Branch-ID'] = branch;
    return h;
  }

  async function _api(method, path, body) {
    const opts = { method, headers: _headers() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'Error ' + res.status);
    return data;
  }

  function _fmtDate(iso) {
    return mesioDate(iso, { format: 'long' });
  }

  function _toast(msg, isError) {
    if (typeof mesioToast === 'function') {
      mesioToast(msg, isError ? 'error' : 'success');
    } else {
      alert(msg);
    }
  }

  // ── Modal helpers ─────────────────────────────────────────────────────────
  function _openModal(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'flex';
  }
  function _closeModal(id) {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  // Click outside to close
  ['annModal', 'taskModal', 'completionsModal'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('click', e => { if (e.target === el) _closeModal(id); });
  });

  // ════════════════════════════════════════════════════════════════════════════
  // ANNOUNCEMENTS
  // ════════════════════════════════════════════════════════════════════════════

  async function loadAnnouncements() {
    const container = document.getElementById('annList');
    const sub = document.getElementById('annCountSub');
    if (!container) return;
    try {
      const data = await _api('GET', '/api/staff/announcements');
      const items = data.announcements || [];
      if (sub) sub.textContent = items.length + ' anuncio' + (items.length !== 1 ? 's' : '') + ' activo' + (items.length !== 1 ? 's' : '');
      _renderAnnouncements(container, items);
    } catch (err) {
      if (container) container.innerHTML = '<div style="font-size:13px;color:var(--text-4);padding:16px 0;">No se pudieron cargar los anuncios.</div>';
    }
  }

  function _renderAnnouncements(container, items) {
    container.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:13px;color:var(--text-4);padding:16px 0;';
      empty.textContent = 'No hay anuncios activos. Publica uno para que tu equipo lo vea.';
      container.appendChild(empty);
      return;
    }

    items.forEach(ann => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:12px;padding:12px 0;border-bottom:1px solid var(--surface-3,#f0f0f0);';

      const info = document.createElement('div');
      info.style.flex = '1';

      const titleEl = document.createElement('div');
      titleEl.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);';
      titleEl.textContent = ann.title;

      const bodyEl = document.createElement('div');
      bodyEl.style.cssText = 'font-size:13px;color:var(--text-3);margin-top:3px;white-space:pre-wrap;';
      bodyEl.textContent = ann.body;

      const metaEl = document.createElement('div');
      metaEl.style.cssText = 'font-size:11px;color:var(--text-4);margin-top:5px;display:flex;gap:10px;flex-wrap:wrap;';

      const dateSpan = document.createElement('span');
      dateSpan.textContent = 'Publicado: ' + _fmtDate(ann.created_at);
      metaEl.appendChild(dateSpan);

      if (ann.expires_at) {
        const expSpan = document.createElement('span');
        expSpan.textContent = 'Expira: ' + _fmtDate(ann.expires_at);
        metaEl.appendChild(expSpan);
      }

      const badge = document.createElement('span');
      badge.style.cssText = 'display:inline-block;background:#D1FAE5;color:#065F46;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:600;';
      badge.textContent = 'Activo';
      metaEl.appendChild(badge);

      info.appendChild(titleEl);
      info.appendChild(bodyEl);
      info.appendChild(metaEl);

      const delBtn = document.createElement('button');
      delBtn.className = 'btn sm';
      delBtn.style.cssText = 'color:var(--error,#dc2626);border-color:var(--error,#dc2626);flex-shrink:0;';
      delBtn.textContent = 'Eliminar';
      delBtn.dataset.annId = ann.id;
      delBtn.addEventListener('click', () => _deleteAnnouncement(ann.id, ann.title));

      row.appendChild(info);
      row.appendChild(delBtn);
      container.appendChild(row);
    });
  }

  async function _deleteAnnouncement(id, title) {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Eliminar el anuncio "' + title + '"? Esta acción no se puede deshacer.')
      : confirm('¿Eliminar el anuncio "' + title + '"?');
    if (!confirmed) return;
    try {
      await _api('DELETE', '/api/staff/announcements/' + id);
      _toast('Anuncio eliminado.');
      loadAnnouncements();
    } catch (err) {
      _toast(err.message || 'Error al eliminar el anuncio.', true);
    }
  }

  // Modal wiring — Announcements
  const btnNewAnn = document.getElementById('btnNewAnnouncement');
  if (btnNewAnn) {
    btnNewAnn.addEventListener('click', () => {
      document.getElementById('annTitle').value = '';
      document.getElementById('annBody').value = '';
      document.getElementById('annExpires').value = '';
      _openModal('annModal');
      document.getElementById('annTitle').focus();
    });
  }

  const annModalCancel = document.getElementById('annModalCancel');
  if (annModalCancel) annModalCancel.addEventListener('click', () => _closeModal('annModal'));
  const annModalClose = document.getElementById('annModalClose');
  if (annModalClose) annModalClose.addEventListener('click', () => _closeModal('annModal'));

  const annModalSubmit = document.getElementById('annModalSubmit');
  if (annModalSubmit) {
    annModalSubmit.addEventListener('click', async () => {
      const title = (document.getElementById('annTitle').value || '').trim();
      const body = (document.getElementById('annBody').value || '').trim();
      const expiresRaw = (document.getElementById('annExpires').value || '').trim();

      if (!title) { _toast('El título es obligatorio.', true); return; }
      if (!body)  { _toast('El mensaje es obligatorio.', true); return; }

      const payload = { title, body, expires_at: expiresRaw ? expiresRaw + 'T23:59:00Z' : null };
      annModalSubmit.disabled = true;
      try {
        await _api('POST', '/api/staff/announcements', payload);
        _toast('Anuncio publicado.');
        _closeModal('annModal');
        loadAnnouncements();
      } catch (err) {
        _toast(err.message || 'Error al publicar el anuncio.', true);
      } finally {
        annModalSubmit.disabled = false;
      }
    });
  }

  // ════════════════════════════════════════════════════════════════════════════
  // TASKS
  // ════════════════════════════════════════════════════════════════════════════

  async function loadTasks() {
    const container = document.getElementById('taskList');
    const sub = document.getElementById('taskCountSub');
    if (!container) return;
    try {
      const data = await _api('GET', '/api/staff/tasks');
      const tasks = data.tasks || [];
      if (sub) sub.textContent = tasks.length + ' tarea' + (tasks.length !== 1 ? 's' : '') + ' activa' + (tasks.length !== 1 ? 's' : '');
      _renderTasks(container, tasks);
    } catch (err) {
      if (container) container.innerHTML = '<div style="font-size:13px;color:var(--text-4);padding:16px 0;">No se pudieron cargar las tareas.</div>';
    }
  }

  function _renderTasks(container, tasks) {
    container.innerHTML = '';
    if (!tasks.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:13px;color:var(--text-4);padding:16px 0;';
      empty.textContent = 'No hay tareas activas. Crea una para asignarla a tu equipo.';
      container.appendChild(empty);
      return;
    }

    tasks.forEach(task => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:12px;padding:12px 0;border-bottom:1px solid var(--surface-3,#f0f0f0);';

      const info = document.createElement('div');
      info.style.flex = '1';

      const titleEl = document.createElement('div');
      titleEl.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);';
      titleEl.textContent = task.title;

      info.appendChild(titleEl);

      if (task.description) {
        const descEl = document.createElement('div');
        descEl.style.cssText = 'font-size:13px;color:var(--text-3);margin-top:3px;white-space:pre-wrap;';
        descEl.textContent = task.description;
        info.appendChild(descEl);
      }

      const metaEl = document.createElement('div');
      metaEl.style.cssText = 'font-size:11px;color:var(--text-4);margin-top:5px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;';

      if (task.due_date) {
        const dueSpan = document.createElement('span');
        dueSpan.textContent = 'Vence: ' + task.due_date;
        metaEl.appendChild(dueSpan);
      }

      const countBadge = document.createElement('span');
      const count = task.completions_count || 0;
      countBadge.style.cssText = 'display:inline-block;background:var(--brand-light,#D1FAE5);color:var(--brand,#1D9E75);border-radius:4px;padding:1px 7px;font-size:11px;font-weight:600;cursor:pointer;';
      countBadge.textContent = count + ' completada' + (count !== 1 ? 's' : '');
      countBadge.title = 'Ver quién completó esta tarea';
      countBadge.addEventListener('click', () => _showCompletions(task.id, task.title));
      metaEl.appendChild(countBadge);

      info.appendChild(metaEl);

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;flex-shrink:0;';

      const viewBtn = document.createElement('button');
      viewBtn.className = 'btn sm';
      viewBtn.textContent = 'Ver';
      viewBtn.addEventListener('click', () => _showCompletions(task.id, task.title));

      const delBtn = document.createElement('button');
      delBtn.className = 'btn sm';
      delBtn.style.cssText = 'color:var(--error,#dc2626);border-color:var(--error,#dc2626);';
      delBtn.textContent = 'Eliminar';
      delBtn.addEventListener('click', () => _deleteTask(task.id, task.title));

      actions.appendChild(viewBtn);
      actions.appendChild(delBtn);

      row.appendChild(info);
      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  async function _deleteTask(id, title) {
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm('¿Eliminar la tarea "' + title + '"? Esta acción no se puede deshacer.')
      : confirm('¿Eliminar la tarea "' + title + '"?');
    if (!confirmed) return;
    try {
      await _api('DELETE', '/api/staff/tasks/' + id);
      _toast('Tarea eliminada.');
      loadTasks();
    } catch (err) {
      _toast(err.message || 'Error al eliminar la tarea.', true);
    }
  }

  async function _showCompletions(taskId, taskTitle) {
    const list = document.getElementById('completionsList');
    const titleEl = document.getElementById('completionsModalTitle');
    if (titleEl) titleEl.textContent = '"' + taskTitle + '" — completado por';
    if (list) list.innerHTML = '<div style="font-size:13px;color:var(--text-4);">Cargando…</div>';
    _openModal('completionsModal');
    try {
      const data = await _api('GET', '/api/staff/tasks/' + taskId + '/completions');
      const comps = data.completions || [];
      list.innerHTML = '';
      if (!comps.length) {
        const empty = document.createElement('div');
        empty.style.cssText = 'font-size:13px;color:var(--text-4);padding:8px 0;';
        empty.textContent = 'Nadie ha completado esta tarea todavía.';
        list.appendChild(empty);
        return;
      }
      comps.forEach(c => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--surface-3,#f0f0f0);font-size:13px;';

        const nameEl = document.createElement('span');
        nameEl.style.fontWeight = '500';
        nameEl.textContent = c.staff_name || 'Staff';

        const timeEl = document.createElement('span');
        timeEl.style.color = 'var(--text-4)';
        timeEl.textContent = _fmtDate(c.completed_at);

        row.appendChild(nameEl);
        row.appendChild(timeEl);
        list.appendChild(row);
      });
    } catch (err) {
      if (list) {
        list.innerHTML = '';
        const errEl = document.createElement('div');
        errEl.style.cssText = 'font-size:13px;color:var(--error,#dc2626);';
        errEl.textContent = 'Error al cargar completaciones.';
        list.appendChild(errEl);
      }
    }
  }

  // Modal wiring — Tasks
  const btnNewTask = document.getElementById('btnNewTask');
  if (btnNewTask) {
    btnNewTask.addEventListener('click', () => {
      document.getElementById('taskTitle').value = '';
      document.getElementById('taskDesc').value = '';
      document.getElementById('taskDue').value = '';
      _openModal('taskModal');
      document.getElementById('taskTitle').focus();
    });
  }

  const taskModalCancel = document.getElementById('taskModalCancel');
  if (taskModalCancel) taskModalCancel.addEventListener('click', () => _closeModal('taskModal'));
  const taskModalClose = document.getElementById('taskModalClose');
  if (taskModalClose) taskModalClose.addEventListener('click', () => _closeModal('taskModal'));
  const completionsModalClose = document.getElementById('completionsModalClose');
  if (completionsModalClose) completionsModalClose.addEventListener('click', () => _closeModal('completionsModal'));

  const taskModalSubmit = document.getElementById('taskModalSubmit');
  if (taskModalSubmit) {
    taskModalSubmit.addEventListener('click', async () => {
      const title = (document.getElementById('taskTitle').value || '').trim();
      const description = (document.getElementById('taskDesc').value || '').trim();
      const due_date = (document.getElementById('taskDue').value || '').trim() || null;

      if (!title) { _toast('El título es obligatorio.', true); return; }

      taskModalSubmit.disabled = true;
      try {
        await _api('POST', '/api/staff/tasks', { title, description, due_date });
        _toast('Tarea creada.');
        _closeModal('taskModal');
        loadTasks();
      } catch (err) {
        _toast(err.message || 'Error al crear la tarea.', true);
      } finally {
        taskModalSubmit.disabled = false;
      }
    });
  }

  // ════════════════════════════════════════════════════════════════════════════
  // SHIFT SWAP (admin view)
  // ════════════════════════════════════════════════════════════════════════════

  const _SWAP_STATUS_LABELS = {
    pending:          { text: 'Pendiente',        bg: '#FEF3C7', color: '#92400E' },
    target_accepted:  { text: 'Aceptado (staff)',  bg: '#D1FAE5', color: '#065F46' },
    target_rejected:  { text: 'Rechazado (staff)', bg: '#FEE2E2', color: '#991B1B' },
    admin_approved:   { text: 'Aprobado',          bg: '#D1FAE5', color: '#065F46' },
    admin_rejected:   { text: 'Rechazado',         bg: '#FEE2E2', color: '#991B1B' },
    withdrawn:        { text: 'Retirado',          bg: '#F3F4F6', color: '#6B7280' },
  };

  async function loadShiftSwaps() {
    const container = document.getElementById('swapList');
    const sub       = document.getElementById('swapCountSub');
    if (!container) return;
    try {
      const data  = await _api('GET', '/api/staff/shift-swap');
      const items = data.swaps || [];
      const pending = items.filter(s => s.status === 'pending' || s.status === 'target_accepted');
      if (sub) sub.textContent = pending.length + ' solicitud' + (pending.length !== 1 ? 'es' : '') + ' pendiente' + (pending.length !== 1 ? 's' : '');
      _renderSwaps(container, items);
    } catch (err) {
      container.innerHTML = '<div style="font-size:13px;color:var(--text-4);padding:16px 0;">No se pudieron cargar las solicitudes.</div>';
    }
  }

  function _renderSwaps(container, items) {
    container.innerHTML = '';
    if (!items.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'font-size:13px;color:var(--text-4);padding:16px 0;';
      empty.textContent = 'No hay solicitudes de cambio de turno.';
      container.appendChild(empty);
      return;
    }

    items.forEach(s => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--surface-3,#f0f0f0);flex-wrap:wrap;';

      const info = document.createElement('div');
      info.style.cssText = 'flex:1;min-width:200px;';

      const nameEl = document.createElement('div');
      nameEl.style.cssText = 'font-weight:600;font-size:14px;color:var(--text-1);';
      // user-supplied names via textContent (safe)
      const reqNameTxt = document.createTextNode((s.requester_name || 'Staff') + ' → ' + (s.target_name || 'Staff'));
      nameEl.appendChild(reqNameTxt);

      const metaEl = document.createElement('div');
      metaEl.style.cssText = 'font-size:12px;color:var(--text-3);margin-top:3px;';
      const dateSpan = document.createElement('span');
      dateSpan.textContent = 'Fecha: ' + (s.shift_date || '—');
      if (s.shift_start_time) {
        const timeSpan = document.createElement('span');
        timeSpan.textContent = '  ·  ' + s.shift_start_time + (s.shift_end_time ? '–' + s.shift_end_time : '');
        metaEl.appendChild(dateSpan);
        metaEl.appendChild(timeSpan);
      } else {
        metaEl.appendChild(dateSpan);
      }

      if (s.reason) {
        const reasonEl = document.createElement('div');
        reasonEl.style.cssText = 'font-size:12px;color:var(--text-4);margin-top:2px;';
        reasonEl.textContent = 'Razón: ' + s.reason;
        info.appendChild(nameEl);
        info.appendChild(metaEl);
        info.appendChild(reasonEl);
      } else {
        info.appendChild(nameEl);
        info.appendChild(metaEl);
      }

      const meta2 = _SWAP_STATUS_LABELS[s.status] || { text: s.status, bg: '#F3F4F6', color: '#6B7280' };
      const badge = document.createElement('span');
      badge.style.cssText = 'display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;background:' + meta2.bg + ';color:' + meta2.color + ';';
      badge.textContent = meta2.text;

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:6px;flex-shrink:0;align-items:center;';
      actions.appendChild(badge);

      if (s.status === 'target_accepted') {
        const approveBtn = document.createElement('button');
        approveBtn.className = 'btn sm';
        approveBtn.style.cssText = 'background:var(--brand,#1D9E75);color:#fff;border-color:var(--brand,#1D9E75);';
        approveBtn.textContent = 'Aprobar';
        approveBtn.addEventListener('click', () => _approveSwap(s.id, s.requester_name, s.target_name, s.shift_date));

        const rejectBtn = document.createElement('button');
        rejectBtn.className = 'btn sm';
        rejectBtn.style.cssText = 'color:var(--error,#dc2626);border-color:var(--error,#dc2626);';
        rejectBtn.textContent = 'Rechazar';
        rejectBtn.addEventListener('click', () => _rejectSwap(s.id));

        actions.appendChild(approveBtn);
        actions.appendChild(rejectBtn);
      }

      row.appendChild(info);
      row.appendChild(actions);
      container.appendChild(row);
    });
  }

  async function _approveSwap(id, reqName, tgtName, shiftDate) {
    const msg = '¿Aprobar el cambio de turno del ' + (shiftDate || '') + ' entre ' + (reqName || 'Staff') + ' y ' + (tgtName || 'Staff') + '?';
    const confirmed = typeof mesioConfirm === 'function'
      ? await mesioConfirm(msg)
      : confirm(msg);
    if (!confirmed) return;
    try {
      await _api('POST', '/api/staff/shift-swap/' + id + '/approve');
      _toast('Cambio de turno aprobado y ejecutado.');
      loadShiftSwaps();
    } catch (err) {
      _toast(err.message || 'Error al aprobar el cambio.', true);
    }
  }

  async function _rejectSwap(id) {
    const notes = prompt('Razón del rechazo (opcional):') || '';
    try {
      await _api('POST', '/api/staff/shift-swap/' + id + '/reject', { admin_notes: notes || null });
      _toast('Cambio de turno rechazado.');
      loadShiftSwaps();
    } catch (err) {
      _toast(err.message || 'Error al rechazar el cambio.', true);
    }
  }

  // ── Init ─────────────────────────────────────────────────────────────────
  loadAnnouncements();
  loadTasks();
  loadShiftSwaps();
})();
