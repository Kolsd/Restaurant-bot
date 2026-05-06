/**
 * demo-chat.js — Live chat widget for the /demo page.
 *
 * Responsibilities:
 *   - On page load: POST /api/demo/start, store session_id in sessionStorage.
 *   - Send message: POST /api/demo/chat, render reply.
 *   - Reset: POST /api/demo/reset, start fresh session.
 *   - Counter: show "X / 30 mensajes" with warning at 25.
 *
 * Security:
 *   - All user-generated text rendered via _escHtml (never innerHTML with raw input).
 *   - Bot reply rendered via _escHtml (prevents XSS from LLM output).
 */

/* global _escHtml */

(function () {
  'use strict';

  // ── Constants ──────────────────────────────────────────────────────────────

  var MAX_MESSAGES = 30;
  var SESSION_KEY = 'mesio_demo_session_id';

  // ── State ──────────────────────────────────────────────────────────────────

  var _sessionId = null;
  var _messageCount = 0;
  var _sending = false;

  // ── DOM refs (populated after DOMContentLoaded) ────────────────────────────

  var chatEl, inputEl, sendBtn, counterEl, resetBtn, typingEl, statusEl;

  // ── Bootstrap ──────────────────────────────────────────────────────────────

  function init() {
    chatEl    = document.getElementById('demo-chat-messages');
    inputEl   = document.getElementById('demo-chat-input');
    sendBtn   = document.getElementById('demo-chat-send');
    counterEl = document.getElementById('demo-chat-counter');
    resetBtn  = document.getElementById('demo-chat-reset');
    typingEl  = document.getElementById('demo-chat-typing');
    statusEl  = document.getElementById('demo-chat-status');

    if (!chatEl || !inputEl || !sendBtn) return; // widget not in DOM

    sendBtn.addEventListener('click', handleSend);
    resetBtn && resetBtn.addEventListener('click', handleReset);
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });

    startSession();
  }

  // ── Session management ─────────────────────────────────────────────────────

  function startSession() {
    // Reuse an existing session from this tab if still valid
    var saved = sessionStorage.getItem(SESSION_KEY);
    if (saved) {
      _sessionId = saved;
      appendBotMessage('Hola, bienvenido de nuevo a la demo de Mesio. Soy el asistente de Demo Mesio — ¿en qué te puedo ayudar hoy?');
      updateCounter(0);
      return;
    }

    setStatus('Iniciando sesión…', false);
    fetch('/api/demo/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        _sessionId = data.session_id;
        sessionStorage.setItem(SESSION_KEY, _sessionId);
        setStatus('', false);
        appendBotMessage(
          'Hola, soy el asistente de <strong>' + _escHtml(data.restaurant_name) + '</strong> 👋. ' +
          'Puedo ayudarte a pedir comida, hacer una reserva o responder tus preguntas. ¿En qué te puedo ayudar?'
        );
        updateCounter(0);
      })
      .catch(function (err) {
        setStatus('No se pudo iniciar la sesión. Recarga la página.', true);
        console.error('demo-chat: start failed', err);
      });
  }

  // ── Send ───────────────────────────────────────────────────────────────────

  function handleSend() {
    if (_sending || !_sessionId) return;
    var text = inputEl.value.trim();
    if (!text) return;

    if (_messageCount >= MAX_MESSAGES) {
      appendBotMessage(
        'Esta sesión de demo está limitada a ' + MAX_MESSAGES + ' mensajes. ' +
        'Pulsa <strong>Reiniciar</strong> para comenzar una sesión nueva.'
      );
      return;
    }

    appendUserMessage(text);
    inputEl.value = '';
    _sending = true;
    sendBtn.disabled = true;
    showTyping(true);

    fetch('/api/demo/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: _sessionId, message: text }),
    })
      .then(function (res) {
        if (res.status === 404) {
          // Session expired server-side — start a new one
          sessionStorage.removeItem(SESSION_KEY);
          _sessionId = null;
          startSession();
          return null;
        }
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        showTyping(false);
        _sending = false;
        sendBtn.disabled = false;

        if (!data) return; // session was reset, startSession() already called

        _messageCount = data.message_count || 0;
        updateCounter(_messageCount);

        if (data.reply) {
          appendBotMessage(_escHtml(data.reply));
        }

        if (data.expired) {
          inputEl.disabled = true;
          sendBtn.disabled = true;
          setStatus(
            'Sesión terminada (' + MAX_MESSAGES + ' mensajes). Pulsa Reiniciar.',
            false
          );
        }
      })
      .catch(function (err) {
        showTyping(false);
        _sending = false;
        sendBtn.disabled = false;
        appendBotMessage('Hubo un problema de conexión. Por favor intenta de nuevo.');
        console.error('demo-chat: chat failed', err);
      });
  }

  // ── Reset ──────────────────────────────────────────────────────────────────

  function handleReset() {
    if (!_sessionId) {
      sessionStorage.removeItem(SESSION_KEY);
      startSession();
      return;
    }

    var sid = _sessionId;
    _sessionId = null;
    _messageCount = 0;
    sessionStorage.removeItem(SESSION_KEY);

    // Clear chat UI first for instant feedback
    chatEl.innerHTML = '';
    inputEl.disabled = false;
    sendBtn.disabled = false;
    setStatus('', false);
    updateCounter(0);

    fetch('/api/demo/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid }),
    }).catch(function (err) {
      console.warn('demo-chat: reset cleanup failed (non-fatal)', err);
    });

    startSession();
  }

  // ── DOM helpers ────────────────────────────────────────────────────────────

  function appendUserMessage(text) {
    var el = document.createElement('div');
    el.className = 'demo-bubble demo-bubble-user';
    el.innerHTML = '<span class="demo-bubble-text">' + _escHtml(text) + '</span>';
    chatEl.appendChild(el);
    scrollToBottom();
  }

  function appendBotMessage(htmlContent) {
    var el = document.createElement('div');
    el.className = 'demo-bubble demo-bubble-bot';
    // htmlContent is either _escHtml'd or a trusted static string from our code
    el.innerHTML =
      '<span class="demo-bubble-avatar" aria-hidden="true">M</span>' +
      '<span class="demo-bubble-text">' + htmlContent + '</span>';
    chatEl.appendChild(el);
    scrollToBottom();
  }

  function showTyping(visible) {
    if (typingEl) {
      typingEl.hidden = !visible;
      if (visible) scrollToBottom();
    }
  }

  function updateCounter(count) {
    if (!counterEl) return;
    var remaining = MAX_MESSAGES - count;
    counterEl.textContent = count + ' / ' + MAX_MESSAGES + ' mensajes';
    counterEl.classList.toggle('demo-counter-warn', remaining <= 5 && remaining > 0);
    counterEl.classList.toggle('demo-counter-danger', remaining <= 0);
  }

  function setStatus(text, isError) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.hidden = !text;
    statusEl.classList.toggle('demo-status-error', isError);
  }

  function scrollToBottom() {
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  // ── Wire up on DOM ready ───────────────────────────────────────────────────

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
