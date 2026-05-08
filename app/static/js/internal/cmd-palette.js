/**
 * app/static/js/internal/cmd-palette.js
 *
 * Cmd+K command palette for Mesio HQ.
 * Opens on Cmd+K (Mac) / Ctrl+K (Win/Linux) or when the topbar search is focused.
 *
 * Public API (window.MesioCmdPalette):
 *   .open()   — open and focus the palette
 *   .close()  — close the palette
 *   .toggle() — toggle open/closed
 */
(function () {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────────────
  const SESSION_KEY = 'hq_session';
  const DEBOUNCE_MS = 200;
  const MAX_RESULTS = 20;

  // Type → icon mapping
  const TYPE_ICONS = {
    tenant:   '🏢',
    prospect: '👤',
    action:   '⚡',
  };

  const TYPE_LABELS = {
    tenant:   'Tenant',
    prospect: 'Prospecto',
    action:   'Acción',
  };

  // ── State ──────────────────────────────────────────────────────────────────
  let _overlay = null;
  let _modal = null;
  let _input = null;
  let _list = null;
  let _debounceTimer = null;
  let _results = [];
  let _selectedIdx = -1;
  let _isOpen = false;
  let _lastQuery = null;

  // ── Helpers ────────────────────────────────────────────────────────────────
  function _getToken() {
    return sessionStorage.getItem(SESSION_KEY) ||
           sessionStorage.getItem('mesio_admin_key') || '';
  }

  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── DOM construction ───────────────────────────────────────────────────────
  function _build() {
    // Overlay
    _overlay = document.createElement('div');
    _overlay.id = 'cmd-palette-overlay';
    _overlay.setAttribute('role', 'dialog');
    _overlay.setAttribute('aria-modal', 'true');
    _overlay.setAttribute('aria-label', 'Búsqueda global HQ');
    _overlay.addEventListener('mousedown', function (e) {
      if (e.target === _overlay) _close();
    });

    // Modal
    _modal = document.createElement('div');
    _modal.id = 'cmd-palette-modal';

    // Search input row
    var inputWrap = document.createElement('div');
    inputWrap.id = 'cmd-palette-input-wrap';

    var searchIcon = document.createElement('span');
    searchIcon.id = 'cmd-palette-search-icon';
    searchIcon.textContent = '🔍';

    _input = document.createElement('input');
    _input.id = 'cmd-palette-input';
    _input.type = 'search';
    _input.placeholder = 'Buscar tenant, prospecto, acción…';
    _input.autocomplete = 'off';
    _input.setAttribute('autocorrect', 'off');
    _input.setAttribute('spellcheck', 'false');
    _input.addEventListener('input', _onInput);
    _input.addEventListener('keydown', _onKeydown);

    var kbdHint = document.createElement('kbd');
    kbdHint.id = 'cmd-palette-esc-hint';
    kbdHint.textContent = 'Esc';

    inputWrap.appendChild(searchIcon);
    inputWrap.appendChild(_input);
    inputWrap.appendChild(kbdHint);

    // Results list
    _list = document.createElement('div');
    _list.id = 'cmd-palette-list';
    _list.setAttribute('role', 'listbox');

    // Footer
    var footer = document.createElement('div');
    footer.id = 'cmd-palette-footer';
    footer.innerHTML =
      '<span><kbd>↑</kbd><kbd>↓</kbd> navegar</span>' +
      '<span><kbd>↵</kbd> abrir</span>' +
      '<span><kbd>Esc</kbd> cerrar</span>' +
      '<span><kbd>' + (navigator.platform.indexOf('Mac') >= 0 ? '⌘' : 'Ctrl') + 'K</kbd> abrir</span>';

    _modal.appendChild(inputWrap);
    _modal.appendChild(_list);
    _modal.appendChild(footer);
    _overlay.appendChild(_modal);
    document.body.appendChild(_overlay);
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  function _render() {
    if (!_list) return;

    if (_results.length === 0) {
      var q = (_input && _input.value.trim()) || '';
      if (!q) {
        _list.innerHTML =
          '<div class="cmd-palette-empty">Escribe para buscar tenants, prospectos o acciones…</div>';
      } else {
        _list.innerHTML =
          '<div class="cmd-palette-empty">Sin resultados para "<strong>' + _esc(q) + '</strong>"</div>';
      }
      _selectedIdx = -1;
      return;
    }

    // Group by type
    var groups = {};
    var groupOrder = [];
    _results.forEach(function (r) {
      var t = r.type || 'action';
      if (!groups[t]) {
        groups[t] = [];
        groupOrder.push(t);
      }
      groups[t].push(r);
    });

    var html = '';
    var flatIdx = 0;

    groupOrder.forEach(function (type) {
      var label = TYPE_LABELS[type] || type;
      var items = groups[type];

      // Section header (only if more than one type present)
      if (groupOrder.length > 1) {
        html +=
          '<div class="cmd-palette-section-header">' + _esc(label) + 's</div>';
      }

      items.forEach(function (item) {
        var icon = TYPE_ICONS[item.type] || '•';
        var idx = flatIdx++;
        var sel = idx === _selectedIdx ? ' selected' : '';
        html +=
          '<div class="cmd-palette-item' + sel + '" data-idx="' + idx + '" data-url="' + _esc(item.url || '#') + '"' +
          ' role="option" aria-selected="' + (idx === _selectedIdx) + '">' +
          '<span class="cmd-palette-item-icon">' + icon + '</span>' +
          '<span class="cmd-palette-item-body">' +
            '<span class="cmd-palette-item-title">' + _esc(item.title || '') + '</span>' +
            '<span class="cmd-palette-item-sub">' + _esc(item.subtitle || '') + '</span>' +
          '</span>' +
          '<span class="cmd-palette-item-type">' + _esc(TYPE_LABELS[item.type] || '') + '</span>' +
          '</div>';
      });
    });

    _list.innerHTML = html;

    // Wire click handlers
    _list.querySelectorAll('.cmd-palette-item').forEach(function (el) {
      el.addEventListener('mousedown', function (e) {
        e.preventDefault(); // prevent blur before click fires
        var url = el.getAttribute('data-url');
        if (url) _navigate(url);
      });
      el.addEventListener('mousemove', function () {
        var idx = parseInt(el.getAttribute('data-idx'), 10);
        _setSelected(idx);
      });
    });

    // Scroll selected item into view
    _scrollSelectedIntoView();
  }

  function _setSelected(idx) {
    _selectedIdx = idx;
    if (!_list) return;
    _list.querySelectorAll('.cmd-palette-item').forEach(function (el) {
      var elIdx = parseInt(el.getAttribute('data-idx'), 10);
      el.classList.toggle('selected', elIdx === _selectedIdx);
      el.setAttribute('aria-selected', elIdx === _selectedIdx ? 'true' : 'false');
    });
  }

  function _scrollSelectedIntoView() {
    if (!_list || _selectedIdx < 0) return;
    var el = _list.querySelector('.cmd-palette-item.selected');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }

  // ── Navigation ─────────────────────────────────────────────────────────────
  function _navigate(url) {
    _close();
    window.location.href = url;
  }

  // ── Input handling ─────────────────────────────────────────────────────────
  function _onInput() {
    clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(_doSearch, DEBOUNCE_MS);
  }

  function _onKeydown(e) {
    var count = _results.length;
    if (e.key === 'Escape') {
      _close();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _setSelected(count === 0 ? -1 : (_selectedIdx + 1) % count);
      _scrollSelectedIntoView();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      _setSelected(count === 0 ? -1 : (_selectedIdx - 1 + count) % count);
      _scrollSelectedIntoView();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      if (_selectedIdx >= 0 && _results[_selectedIdx]) {
        _navigate(_results[_selectedIdx].url || '#');
      } else if (_results.length > 0) {
        _navigate(_results[0].url || '#');
      }
      return;
    }
  }

  // ── Search ─────────────────────────────────────────────────────────────────
  function _doSearch() {
    if (!_input) return;
    var q = _input.value.trim();
    if (q === _lastQuery) return;
    _lastQuery = q;

    var token = _getToken();

    _list.innerHTML = '<div class="cmd-palette-empty">Buscando…</div>';
    _results = [];
    _selectedIdx = -1;

    fetch('/api/internal/search?q=' + encodeURIComponent(q), {
      headers: { 'Authorization': 'Bearer ' + token }
    })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        _results = (data.results || []).slice(0, MAX_RESULTS);
        _selectedIdx = _results.length > 0 ? 0 : -1;
        _render();
      })
      .catch(function (err) {
        _results = [];
        _selectedIdx = -1;
        if (_list) {
          _list.innerHTML =
            '<div class="cmd-palette-empty">Error al buscar. Intenta de nuevo.</div>';
        }
      });
  }

  // ── Open / Close ───────────────────────────────────────────────────────────
  function _open(prefill) {
    if (!_overlay) _build();
    if (_isOpen) {
      // Already open — just focus and optionally prefill
      if (prefill !== undefined && _input) {
        _input.value = prefill;
        _lastQuery = null;
        _doSearch();
      }
      if (_input) _input.focus();
      return;
    }

    _isOpen = true;
    _selectedIdx = -1;
    _lastQuery = null;
    _results = [];

    _overlay.classList.add('open');
    document.body.classList.add('cmd-palette-open');

    if (_input) {
      if (prefill !== undefined) {
        _input.value = prefill;
      }
      _input.focus();
      _input.select();
    }

    // Load initial results (empty query = quick actions)
    _doSearch();
  }

  function _close() {
    if (!_isOpen) return;
    _isOpen = false;
    if (_overlay) _overlay.classList.remove('open');
    document.body.classList.remove('cmd-palette-open');
    if (_input) _input.value = '';
    _results = [];
    _selectedIdx = -1;
    _lastQuery = null;
    if (_list) _list.innerHTML = '';

    // Return focus to topbar search input if it triggered us
    var topbarSearch = document.getElementById('hq-search');
    if (topbarSearch) topbarSearch.blur();
  }

  function _toggle() {
    if (_isOpen) _close(); else _open();
  }

  // ── Keyboard shortcut ─────────────────────────────────────────────────────
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      _toggle();
    }
  });

  // ── Public API ─────────────────────────────────────────────────────────────
  window.MesioCmdPalette = {
    open: _open,
    close: _close,
    toggle: _toggle,
  };
})();
