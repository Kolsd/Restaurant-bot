/* ═══════════════════════════════════════════════════
   Mesio — Setup checklist card (dashboard)
   app/static/js/pages/onboarding-card.js

   Shows a new restaurant what it still has to do before it can sell
   through Mesio, with a link to where each thing is done. Reads
   GET /api/onboarding. Hidden when every REQUIRED step is done — optional
   ones (inviting a team) never keep it on screen.

   Every string from the server goes in through textContent; the step
   titles are ours today, but the card must not become the one place where
   that assumption turns into an XSS the day a detail includes a dish name.
═══════════════════════════════════════════════════ */
(function () {
  'use strict';

  var card = document.getElementById('onboarding-card');
  if (!card) return;

  function _el(tag, style, text) {
    var n = document.createElement(tag);
    if (style) n.setAttribute('style', style);
    if (text != null) n.textContent = text;
    return n;
  }

  function _trialText(days) {
    if (days == null) return '';
    if (days <= 0) return 'Tu prueba gratis terminó';
    return days === 1 ? 'Te queda 1 día de prueba' : 'Te quedan ' + days + ' días de prueba';
  }

  // A plain link to an authenticated endpoint does not work here: the app
  // authenticates with a Bearer token that lives in JS (mesioHeaders), not
  // a cookie, so a new-tab navigation arrives with no credentials and gets
  // a 401. Found by clicking it — the endpoint tests stub out auth and could
  // not see it.
  //
  // Opening a new window and filling it was the first fix, and it depends
  // on the browser allowing a popup — which a locked-down restaurant tablet
  // may not. So the sheet is fetched with the token, written into a hidden
  // iframe on this page, and printed from there: the owner gets the print
  // dialog (with its own preview) and no popup is involved at all.
  var _PRINT_FRAME_ID = 'onboarding-print-frame';

  function _printAuthenticated(href) {
    mesioToast('Preparando los códigos para imprimir…');
    fetch(href, { headers: mesioHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        var old = document.getElementById(_PRINT_FRAME_ID);
        if (old) old.remove();
        var frame = document.createElement('iframe');
        frame.id = _PRINT_FRAME_ID;
        frame.setAttribute('aria-hidden', 'true');
        frame.setAttribute('style', 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;');
        document.body.appendChild(frame);

        frame.addEventListener('load', function () {
          // `load` fires once the QR library has loaded; the sheet draws its
          // codes in its own onload, so give it a tick before printing or
          // the dialog opens on empty boxes.
          setTimeout(function () {
            try {
              frame.contentWindow.focus();
              frame.contentWindow.print();
            } catch (e) {
              mesioToast('No se pudo abrir la impresión: ' + e.message, 'error');
            }
          }, 400);
        });
        var doc = frame.contentWindow.document;
        doc.open();
        doc.write(html);
        doc.close();
      })
      .catch(function (e) {
        mesioToast('No se pudo preparar la hoja de códigos: ' + e.message, 'error');
      });
  }

  function _link(action, className, style) {
    var link = _el('a', style || null, action.label);
    if (className) link.className = className;
    link.href = action.href;
    if (action.external && action.href.indexOf('/api/') === 0) {
      link.addEventListener('click', function (ev) {
        ev.preventDefault();
        _printAuthenticated(action.href);
      });
    } else if (action.external) {
      link.target = '_blank';
      link.rel = 'noopener';
    }
    return link;
  }

  function _renderStep(step) {
    var row = _el('div', 'display:flex;gap:12px;align-items:flex-start;');

    var mark = _el('div',
      'flex:0 0 22px;height:22px;border-radius:50%;display:flex;align-items:center;' +
      'justify-content:center;font-size:13px;font-weight:700;' +
      (step.done
        ? 'background:var(--brand);color:#fff;'
        : 'border:1.5px solid var(--border);color:var(--text-3);'),
      step.done ? '✓' : '');
    row.appendChild(mark);

    var body = _el('div', 'flex:1;min-width:0;');
    var title = _el('div',
      'font-size:13.5px;font-weight:600;' + (step.done ? 'color:var(--text-3);' : ''),
      step.title);
    if (step.optional) {
      title.appendChild(_el('span', 'font-weight:400;color:var(--text-3);margin-left:6px;', '(opcional)'));
    }
    body.appendChild(title);
    body.appendChild(_el('div', 'font-size:12.5px;color:var(--text-2);margin-top:2px;', step.detail));

    if (!step.done && step.actions && step.actions.length) {
      var actions = _el('div', 'display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;');
      step.actions.forEach(function (a, i) {
        actions.appendChild(_link(a, 'btn sm' + (i === 0 ? '' : ' ghost')));
      });
      body.appendChild(actions);
    } else if (step.done && step.key === 'tables') {
      // Done, but the sheet stays reachable: tables get added later, and
      // a lost sticker needs reprinting.
      var reprint = (step.actions || []).filter(function (a) { return a.external; })[0];
      if (reprint) {
        body.appendChild(_link(reprint, null, 'font-size:12px;margin-top:4px;display:inline-block;'));
      }
    }

    row.appendChild(body);
    return row;
  }

  async function loadOnboarding() {
    var res;
    try {
      res = await fetch('/api/onboarding', { headers: mesioHeaders() });
    } catch (e) {
      return;   // the dashboard works without the card; never block it
    }
    if (!res.ok) return;
    var data = await res.json().catch(function () { return null; });
    if (!data || !Array.isArray(data.steps)) return;

    if (data.complete) { card.style.display = 'none'; return; }

    var list = document.getElementById('onboarding-steps');
    list.textContent = '';
    data.steps.forEach(function (s) { list.appendChild(_renderStep(s)); });

    document.getElementById('onboarding-progress').textContent =
      data.done + ' de ' + data.total + ' pasos';
    document.getElementById('onboarding-trial').textContent = _trialText(data.trial_days_left);

    card.style.display = 'block';
  }

  loadOnboarding();
})();
