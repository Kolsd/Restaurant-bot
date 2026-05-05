(function () {
  'use strict';

  /* ── State ─────────────────────────────────────────────────────────── */
  var _email = '';       // captured in view 1, reused in view 2 POST
  var _currentView = 'email';  // 'email' | 'code' | 'success'

  /* ── View switching ─────────────────────────────────────────────────── */
  function showView(name) {
    _currentView = name;
    ['email', 'code', 'success'].forEach(function (v) {
      var el = document.getElementById('rp-view-' + v);
      if (el) el.classList.toggle('active', v === name);
    });
  }

  /* ── Alert banners ──────────────────────────────────────────────────── */
  function showAlert(id, msg, type) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg;          // textContent — safe, never innerHTML
    el.className = 'rp-alert' + (type === 'info' ? ' info' : '');
    el.style.display = 'block';
  }

  function hideAlert(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  /* ── Inline field messages ──────────────────────────────────────────── */
  function showFieldMsg(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'block';
  }

  function hideFieldMsg(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  }

  function setFieldError(inputId, msgId, hasError) {
    var input = document.getElementById(inputId);
    var msg   = document.getElementById(msgId);
    if (input) input.classList.toggle('field-error', hasError);
    if (msg)   msg.style.display = hasError ? 'block' : 'none';
  }

  /* ── Button loading state ───────────────────────────────────────────── */
  function setBtn(id, loading, text) {
    var btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled    = loading;
    btn.textContent = text;
  }

  /* ── PASSWORD SHOW/HIDE TOGGLE ──────────────────────────────────────── */
  function bindToggle(btnId, inputId) {
    var btn   = document.getElementById(btnId);
    var input = document.getElementById(inputId);
    if (!btn || !input) return;
    btn.addEventListener('click', function () {
      var isHidden = input.type === 'password';
      input.type      = isHidden ? 'text' : 'password';
      btn.textContent = isHidden ? 'Ocultar' : 'Mostrar';
    });
  }

  bindToggle('toggle-new-pwd',     'rp-new-password');
  bindToggle('toggle-confirm-pwd', 'rp-confirm-password');

  /* ── VIEW 1: Email form ─────────────────────────────────────────────── */
  document.getElementById('form-email').addEventListener('submit', async function (e) {
    e.preventDefault();
    hideAlert('rp-alert-email');

    var emailVal = document.getElementById('rp-email').value.trim();
    if (!emailVal || !emailVal.includes('@')) {
      setFieldError('rp-email', 'msg-email', true);
      return;
    }
    setFieldError('rp-email', 'msg-email', false);

    setBtn('btn-send-code', true, 'Enviando...');

    try {
      var res = await fetch('/api/auth/forgot-password', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ email: emailVal }),
      });

      // Always treat as success — anti-enumeration (server always returns 200)
      _email = emailVal;
      showAlert(
        'rp-alert-email',
        'Si el correo está registrado, te enviamos un código por WhatsApp. Revisá tu celular.',
        'info'
      );

      // Move to view 2 after 1.5s so the user reads the message
      setTimeout(function () {
        hideAlert('rp-alert-email');
        showView('code');
        var codeInput = document.getElementById('rp-code');
        if (codeInput) codeInput.focus();
      }, 1500);

    } catch (err) {
      showAlert('rp-alert-email', 'Error de conexión. Intentá de nuevo.', '');
    } finally {
      setBtn('btn-send-code', false, 'Enviar código');
    }
  });

  /* Clear field error on input */
  document.getElementById('rp-email').addEventListener('input', function () {
    setFieldError('rp-email', 'msg-email', false);
  });

  /* ── VIEW 2: Code + new password form ──────────────────────────────── */
  document.getElementById('form-code').addEventListener('submit', async function (e) {
    e.preventDefault();
    hideAlert('rp-alert-code');

    var codeVal    = document.getElementById('rp-code').value.trim();
    var newPwd     = document.getElementById('rp-new-password').value;
    var confirmPwd = document.getElementById('rp-confirm-password').value;

    /* Client-side validation */
    var hasError = false;

    if (!/^[0-9]{6}$/.test(codeVal)) {
      setFieldError('rp-code', 'msg-code', true);
      hasError = true;
    } else {
      setFieldError('rp-code', 'msg-code', false);
    }

    if (newPwd.length < 8) {
      setFieldError('rp-new-password', 'msg-new-password', true);
      hasError = true;
    } else {
      setFieldError('rp-new-password', 'msg-new-password', false);
    }

    if (newPwd !== confirmPwd) {
      setFieldError('rp-confirm-password', 'msg-confirm-password', true);
      hasError = true;
    } else {
      setFieldError('rp-confirm-password', 'msg-confirm-password', false);
    }

    if (hasError) return;

    setBtn('btn-change-password', true, 'Cambiando...');

    try {
      var res = await fetch('/api/auth/reset-password', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
          email:        _email,
          code:         codeVal,
          new_password: newPwd,
        }),
      });

      var data = await res.json().catch(function () { return {}; });

      if (res.ok && data.ok) {
        showView('success');
        return;
      }

      /* Handle 400 error codes */
      var errCode = data.error || '';

      if (errCode === 'invalid_code') {
        showAlert('rp-alert-code', 'El código no es correcto. Revisá tu WhatsApp.', '');
        var codeInput = document.getElementById('rp-code');
        if (codeInput) { codeInput.value = ''; codeInput.focus(); }
        setFieldError('rp-code', 'msg-code', false);
        return;
      }

      if (errCode === 'expired') {
        if (typeof mesioToast === 'function') {
          mesioToast('El código expiró. Solicitá uno nuevo.', 'error', 4000);
        }
        _resetToEmailView();
        return;
      }

      if (errCode === 'too_many_attempts') {
        if (typeof mesioToast === 'function') {
          mesioToast('Demasiados intentos. Solicitá un nuevo código.', 'error', 4000);
        }
        _resetToEmailView();
        return;
      }

      if (errCode === 'weak_password') {
        setFieldError('rp-new-password', 'msg-new-password', true);
        return;
      }

      /* Generic fallback */
      showAlert('rp-alert-code', 'Ocurrió un error. Intentá de nuevo.', '');

    } catch (err) {
      showAlert('rp-alert-code', 'Error de conexión. Intentá de nuevo.', '');
    } finally {
      setBtn('btn-change-password', false, 'Cambiar contraseña');
    }
  });

  /* Clear field errors on input */
  document.getElementById('rp-code').addEventListener('input', function () {
    setFieldError('rp-code', 'msg-code', false);
  });
  document.getElementById('rp-new-password').addEventListener('input', function () {
    setFieldError('rp-new-password', 'msg-new-password', false);
    // Re-check confirm match live
    var newPwd     = document.getElementById('rp-new-password').value;
    var confirmPwd = document.getElementById('rp-confirm-password').value;
    if (confirmPwd && newPwd !== confirmPwd) {
      setFieldError('rp-confirm-password', 'msg-confirm-password', true);
    } else {
      setFieldError('rp-confirm-password', 'msg-confirm-password', false);
    }
  });
  document.getElementById('rp-confirm-password').addEventListener('input', function () {
    var newPwd     = document.getElementById('rp-new-password').value;
    var confirmPwd = document.getElementById('rp-confirm-password').value;
    setFieldError('rp-confirm-password', 'msg-confirm-password', newPwd !== confirmPwd);
  });

  /* ── Back to email view ─────────────────────────────────────────────── */
  function _resetToEmailView() {
    hideAlert('rp-alert-code');
    setFieldError('rp-code',             'msg-code',             false);
    setFieldError('rp-new-password',     'msg-new-password',     false);
    setFieldError('rp-confirm-password', 'msg-confirm-password', false);
    document.getElementById('rp-code').value             = '';
    document.getElementById('rp-new-password').value     = '';
    document.getElementById('rp-confirm-password').value = '';
    showView('email');
    var emailInput = document.getElementById('rp-email');
    if (emailInput) emailInput.focus();
  }

  document.getElementById('btn-back-to-email').addEventListener('click', _resetToEmailView);

}());
