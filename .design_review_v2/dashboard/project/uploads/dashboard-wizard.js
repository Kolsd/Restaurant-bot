/* ═══════════════════════════════════════════════════
   Mesio Dashboard — Setup Wizard
   app/static/js/dashboard-wizard.js
═══════════════════════════════════════════════════ */

(function() {
  'use strict';

  var STORAGE_KEY = 'rb_wizard_dismissed';

  var STEP_DEFS = [
    {
      key:   'menu',
      title: 'Configura tu menú',
      desc:  'Agrega tus platos, precios y categorías para que el bot los conozca.',
      cta:   'Ir al Menú',
      action: function() { _navTo('menu'); }
    },
    {
      key:   'staff',
      title: 'Agrega tu equipo',
      desc:  'Registra empleados, roles y horarios para gestionar turnos.',
      cta:   'Ir a Equipo',
      action: function() { _navTo('staff'); }
    },
    {
      key:   'billing',
      title: 'Activa facturación',
      desc:  'Configura tus datos fiscales para emitir facturas electrónicas.',
      cta:   'Ir a Facturación',
      action: function() { _navToBilling(); }
    },
    {
      key:   'whatsapp',
      title: 'Conecta tu WhatsApp',
      desc:  'Vincula tu número de WhatsApp Business para activar el bot.',
      cta:   'Ver instrucciones',
      action: function(stepEl) { _showInlinePanel(stepEl, 'whatsapp'); }
    },
    {
      key:   'first_order',
      title: 'Recibe tu primer pedido',
      desc:  'Prueba el flujo completo enviando un mensaje a tu bot.',
      cta:   'Probar ahora',
      action: function(stepEl) { _showInlinePanel(stepEl, 'first_order'); }
    }
  ];

  // ── Navigation helpers ─────────────────────────────────────────────────────

  function _navTo(sectionId) {
    var navBtn = document.querySelector('[onclick*="\'' + sectionId + '\'"]');
    if (navBtn && typeof showSection === 'function') {
      showSection(sectionId, navBtn);
    }
  }

  function _navToBilling() {
    // Try a billing section first; fall back to settings
    var billingSection = document.getElementById('billing');
    if (billingSection) {
      var navBtn = document.querySelector('[onclick*="\'billing\'"]');
      if (navBtn && typeof showSection === 'function') {
        showSection('billing', navBtn);
        return;
      }
    }
    if (typeof goToSettings === 'function') {
      goToSettings();
    } else {
      window.location.href = '/settings';
    }
  }

  function _showInlinePanel(stepEl, type) {
    // Remove any existing panel inside the wizard
    var existing = document.getElementById('wz-inline-panel');
    if (existing) existing.remove();

    var restaurant = JSON.parse(localStorage.getItem('rb_restaurant') || '{}');
    var phone = (restaurant.whatsapp_number || '').replace(/\D/g, '');

    var panel = document.createElement('div');
    panel.id = 'wz-inline-panel';
    panel.style.cssText = 'background:var(--surface,#fff);border:1.5px solid var(--brand,#1D9E75);border-radius:12px;padding:16px 20px;margin-top:12px;font-size:13px;line-height:1.6;color:var(--text,#111);position:relative;';

    var closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.setAttribute('aria-label', 'Cerrar panel');
    closeBtn.style.cssText = 'position:absolute;top:10px;right:12px;background:none;border:none;font-size:16px;cursor:pointer;color:var(--text-muted,#6b7280);line-height:1;';
    closeBtn.onclick = function() { panel.remove(); };

    var content = document.createElement('div');

    if (type === 'whatsapp') {
      var h = document.createElement('div');
      h.style.cssText = 'font-weight:700;margin-bottom:8px;font-size:14px;';
      h.textContent = '📱 Conectar WhatsApp Business';
      content.appendChild(h);
      var p = document.createElement('p');
      p.style.margin = '0';
      p.textContent = 'Para conectar tu número de WhatsApp Business, contacta a nuestro equipo de soporte:';
      content.appendChild(p);
      var link = document.createElement('a');
      link.href = 'mailto:soporte@mesio.com';
      link.textContent = 'soporte@mesio.com';
      link.style.cssText = 'display:block;margin-top:8px;font-weight:600;color:var(--brand,#1D9E75);';
      content.appendChild(link);
    } else if (type === 'first_order') {
      var h2 = document.createElement('div');
      h2.style.cssText = 'font-weight:700;margin-bottom:8px;font-size:14px;';
      h2.textContent = '🧪 Prueba tu bot';
      content.appendChild(h2);
      var p2 = document.createElement('p');
      p2.style.margin = '0 0 8px';
      p2.textContent = 'Envía un mensaje a tu bot de WhatsApp y haz tu primer pedido de prueba:';
      content.appendChild(p2);
      if (phone) {
        var waLink = document.createElement('a');
        waLink.href = 'https://wa.me/' + phone;
        waLink.target = '_blank';
        waLink.rel = 'noopener noreferrer';
        waLink.textContent = 'Abrir WhatsApp con tu bot →';
        waLink.style.cssText = 'display:inline-block;background:var(--brand,#1D9E75);color:#fff;text-decoration:none;padding:8px 16px;border-radius:8px;font-weight:600;font-size:13px;';
        content.appendChild(waLink);
      } else {
        var noPhone = document.createElement('span');
        noPhone.textContent = 'Conecta tu número de WhatsApp primero.';
        noPhone.style.color = 'var(--text-muted,#6b7280)';
        content.appendChild(noPhone);
      }
    }

    panel.appendChild(closeBtn);
    panel.appendChild(content);

    // Insert after the step card that was clicked
    var card = stepEl.closest('.wz-step');
    if (card) {
      card.parentNode.insertBefore(panel, card.nextSibling);
    } else {
      document.getElementById('wz-steps-list').appendChild(panel);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  function _render(container, data) {
    var score    = data.score || 0;        // 0–100
    var criteria = data.criteria || {};    // { menu: bool, staff: bool, ... }

    var completed = 0;
    STEP_DEFS.forEach(function(s) { if (criteria[s.key]) completed++; });
    var total = STEP_DEFS.length;

    // Celebration when all done
    if (score >= 100) {
      _renderCelebration(container);
      return;
    }

    // Build wizard HTML via DOM (no innerHTML with user data)
    container.innerHTML = '';

    var wizard = document.createElement('div');
    wizard.className = 'wz-card';
    wizard.style.cssText = 'background:var(--card-bg,#fff);border:1.5px solid var(--border,#e5e7eb);border-radius:16px;padding:20px 24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.04);';

    // ── Header row ──
    var header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:14px;';

    var titleWrap = document.createElement('div');
    titleWrap.style.cssText = 'display:flex;align-items:center;gap:10px;min-width:0;';

    var icon = document.createElement('div');
    icon.textContent = '🚀';
    icon.style.cssText = 'font-size:22px;line-height:1;flex-shrink:0;';

    var titleGroup = document.createElement('div');

    var title = document.createElement('div');
    title.style.cssText = 'font-weight:700;font-size:15px;color:var(--text,#111);line-height:1.2;';
    title.textContent = 'Configura tu restaurante';

    var subtitle = document.createElement('div');
    subtitle.style.cssText = 'font-size:12px;color:var(--text-muted,#6b7280);margin-top:2px;';
    subtitle.textContent = completed + ' de ' + total + ' pasos completados';

    titleGroup.appendChild(title);
    titleGroup.appendChild(subtitle);
    titleWrap.appendChild(icon);
    titleWrap.appendChild(titleGroup);

    var dismissLink = document.createElement('button');
    dismissLink.textContent = 'Ocultar';
    dismissLink.style.cssText = 'background:none;border:none;font-size:12px;color:var(--text-muted,#6b7280);cursor:pointer;text-decoration:underline;white-space:nowrap;flex-shrink:0;padding:4px 0;';
    dismissLink.onclick = function() {
      localStorage.setItem(STORAGE_KEY, 'true');
      container.style.display = 'none';
    };

    header.appendChild(titleWrap);
    header.appendChild(dismissLink);

    // ── Progress bar ──
    var progressWrap = document.createElement('div');
    progressWrap.style.cssText = 'margin-bottom:14px;';

    var barBg = document.createElement('div');
    barBg.style.cssText = 'height:8px;background:var(--surface-2,#f3f4f6);border-radius:99px;overflow:hidden;';

    var barFill = document.createElement('div');
    barFill.style.cssText = 'height:100%;background:var(--brand,#1D9E75);border-radius:99px;transition:width 0.6s ease;width:0%;';
    // Animate on next tick
    setTimeout(function() { barFill.style.width = score + '%'; }, 30);

    var pctLabel = document.createElement('div');
    pctLabel.style.cssText = 'font-size:11px;color:var(--text-muted,#6b7280);margin-top:4px;text-align:right;';
    pctLabel.textContent = score + '% completado';

    barBg.appendChild(barFill);
    progressWrap.appendChild(barBg);
    progressWrap.appendChild(pctLabel);

    // ── Steps toggle ──
    var collapsed = true;

    var toggleBtn = document.createElement('button');
    toggleBtn.style.cssText = 'background:none;border:1px solid var(--border,#e5e7eb);border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;color:var(--text-muted,#6b7280);cursor:pointer;width:100%;margin-bottom:0;transition:background 0.15s;';
    toggleBtn.textContent = 'Ver todos los pasos';
    toggleBtn.onmouseover = function() { this.style.background = 'var(--surface-2,#f3f4f6)'; };
    toggleBtn.onmouseout  = function() { this.style.background = 'none'; };

    var stepsList = document.createElement('div');
    stepsList.id = 'wz-steps-list';
    stepsList.style.cssText = 'display:none;margin-top:12px;';

    toggleBtn.onclick = function() {
      collapsed = !collapsed;
      stepsList.style.display = collapsed ? 'none' : 'block';
      toggleBtn.textContent = collapsed ? 'Ver todos los pasos' : 'Ocultar pasos';
    };

    // Build step cards
    STEP_DEFS.forEach(function(stepDef) {
      var done = !!criteria[stepDef.key];

      var stepCard = document.createElement('div');
      stepCard.className = 'wz-step';
      stepCard.style.cssText = 'display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border-radius:10px;background:' + (done ? 'var(--surface-2,#f9fafb)' : 'var(--card-bg,#fff)') + ';border:1px solid var(--border,#e5e7eb);margin-bottom:8px;transition:background 0.2s;';

      // Icon
      var stepIcon = document.createElement('div');
      stepIcon.style.cssText = 'font-size:20px;line-height:1;flex-shrink:0;margin-top:1px;';
      stepIcon.setAttribute('aria-hidden', 'true');
      stepIcon.textContent = done ? '✅' : '⭕';

      // Text group
      var stepBody = document.createElement('div');
      stepBody.style.cssText = 'flex:1;min-width:0;';

      var stepTitle = document.createElement('div');
      stepTitle.style.cssText = 'font-weight:600;font-size:13px;color:' + (done ? 'var(--text-muted,#6b7280)' : 'var(--text,#111)') + ';';
      stepTitle.textContent = stepDef.title;

      var stepDesc = document.createElement('div');
      stepDesc.style.cssText = 'font-size:12px;color:var(--text-muted,#6b7280);margin-top:2px;line-height:1.4;';
      stepDesc.textContent = stepDef.desc;

      stepBody.appendChild(stepTitle);
      stepBody.appendChild(stepDesc);

      // CTA button — only for incomplete steps
      if (!done) {
        var ctaBtn = document.createElement('button');
        ctaBtn.textContent = stepDef.cta;
        ctaBtn.style.cssText = 'flex-shrink:0;background:var(--brand,#1D9E75);color:#fff;border:none;border-radius:8px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;transition:background 0.15s;white-space:nowrap;align-self:center;';
        ctaBtn.onmouseover = function() { this.style.background = '#0F6E56'; };
        ctaBtn.onmouseout  = function() { this.style.background = 'var(--brand,#1D9E75)'; };
        (function(def, card) {
          ctaBtn.onclick = function() { def.action(card); };
        })(stepDef, stepCard);
        stepCard.appendChild(stepIcon);
        stepCard.appendChild(stepBody);
        stepCard.appendChild(ctaBtn);
      } else {
        stepCard.appendChild(stepIcon);
        stepCard.appendChild(stepBody);
      }

      stepsList.appendChild(stepCard);
    });

    // ── Assemble ──
    wizard.appendChild(header);
    wizard.appendChild(progressWrap);
    wizard.appendChild(toggleBtn);
    wizard.appendChild(stepsList);
    container.appendChild(wizard);
  }

  function _renderCelebration(container) {
    container.innerHTML = '';
    var card = document.createElement('div');
    card.style.cssText = 'background:var(--brand,#1D9E75);color:#fff;border-radius:16px;padding:20px 24px;margin-bottom:20px;text-align:center;box-shadow:0 4px 16px rgba(29,158,117,0.25);';

    var emoji = document.createElement('div');
    emoji.style.cssText = 'font-size:36px;line-height:1;margin-bottom:8px;';
    emoji.textContent = '🎉';

    var msg = document.createElement('div');
    msg.style.cssText = 'font-weight:700;font-size:16px;margin-bottom:4px;';
    msg.textContent = '¡Configuración completa!';

    var sub = document.createElement('div');
    sub.style.cssText = 'font-size:13px;opacity:0.85;';
    sub.textContent = 'Tu restaurante está listo para recibir pedidos por WhatsApp.';

    card.appendChild(emoji);
    card.appendChild(msg);
    card.appendChild(sub);
    container.appendChild(card);

    setTimeout(function() {
      card.style.transition = 'opacity 0.5s';
      card.style.opacity = '0';
      setTimeout(function() {
        container.style.display = 'none';
        localStorage.setItem(STORAGE_KEY, 'true');
      }, 500);
    }, 3000);
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  window.SetupWizard = {
    mount: function(selector) {
      var container = document.querySelector(selector);
      if (!container) return;

      // Don't show if dismissed (and score will be checked after fetch)
      var dismissed = localStorage.getItem(STORAGE_KEY) === 'true';

      fetch('/api/onboarding/status', { headers: mesioHeaders() })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
          if (!data) return; // Degrade gracefully on API error

          var score = data.score || 0;

          // If dismissed and not complete, stay hidden
          if (dismissed && score < 100) return;

          // If score is 100, show celebration (dismisses itself)
          // If score is 0–99 and not dismissed, show wizard
          _render(container, data);
        })
        .catch(function() {
          // Degrade gracefully — don't show wizard on error
        });
    }
  };

})();
