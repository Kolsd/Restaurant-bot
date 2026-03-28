/**
 * Mesio Offline-Sync Engine
 * app/static/offline-sync.js
 *
 * Provides the global `MesioSync` object with:
 *   MesioSync.enqueue(type, data)  → stores an operation in IndexedDB
 *   MesioSync.flush()              → sends all pending ops to POST /api/sync
 *   MesioSync.getPendingCount()    → returns number of pending operations
 *
 * Operations are identified by a client-generated UUID so the backend can
 * safely upsert them without duplicates (ON CONFLICT (id) DO UPDATE).
 *
 * Phase 6 usage example:
 *   await MesioSync.enqueue('staff_shift', { staff_id: '...', clock_in: new Date().toISOString() });
 */

const MesioSync = (() => {
  const DB_NAME    = 'mesio_sync';
  const DB_VERSION = 1;
  const STORE      = 'sync_queue';
  const SYNC_TAG   = 'mesio-sync-queue';

  // ── IndexedDB helpers ──────────────────────────────────────────────────────

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);

      req.onupgradeneeded = e => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'id' });
          store.createIndex('status',    'status',    { unique: false });
          store.createIndex('client_ts', 'client_ts', { unique: false });
        }
      };

      req.onsuccess = e => resolve(e.target.result);
      req.onerror   = e => reject(e.target.error);
    });
  }

  function idbGetAll(store, indexName, value) {
    return new Promise((resolve, reject) => {
      const req = store.index(indexName).getAll(value);
      req.onsuccess = e => resolve(e.target.result);
      req.onerror   = e => reject(e.target.error);
    });
  }

  function idbPut(store, record) {
    return new Promise((resolve, reject) => {
      const req = store.put(record);
      req.onsuccess = () => resolve();
      req.onerror   = e => reject(e.target.error);
    });
  }

  // ── UUID v4 generator (no dependency on crypto.randomUUID polyfill) ────────
  function uuid4() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
      return crypto.randomUUID();
    }
    // Fallback for older browsers
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Add an operation to the offline queue.
   * @param {string} type  - Entity type: 'staff_shift', 'table_order', etc.
   * @param {object} data  - The record to upsert. May include an 'id' UUID;
   *                         if absent, one is generated here.
   * @returns {string} The operation ID (UUID).
   */
  async function enqueue(type, data) {
    const op = {
      id:          data.id || uuid4(),
      type,
      action:      'upsert',
      data:        { ...data, id: data.id || undefined },
      client_ts:   new Date().toISOString(),
      status:      'pending',
      retry_count: 0,
      error:       null,
    };
    // Ensure the operation data carries the same UUID so the backend can upsert.
    op.data.id = op.id;

    const db = await openDB();
    const tx = db.transaction(STORE, 'readwrite');
    await idbPut(tx.objectStore(STORE), op);

    // Register a background sync if the browser supports it.
    if ('serviceWorker' in navigator && 'SyncManager' in window) {
      const reg = await navigator.serviceWorker.ready;
      await reg.sync.register(SYNC_TAG).catch(() => {/* SW not active yet */});
    }

    _updateBadge();
    return op.id;
  }

  /**
   * Send all pending operations to the server.
   * Marks each op as 'syncing' before the request, then 'synced' or 'error'.
   * @returns {{ synced: number, errors: Array }} Server response summary.
   */
  async function flush() {
    if (!navigator.onLine) return { synced: 0, errors: [], skipped: true };

    const token = localStorage.getItem('rb_token');
    if (!token) return { synced: 0, errors: [], skipped: true };

    const db = await openDB();
    const tx = db.transaction(STORE, 'readonly');
    const pending = await idbGetAll(tx.objectStore(STORE), 'status', 'pending');

    if (pending.length === 0) return { synced: 0, errors: [] };

    // Mark all as 'syncing' before the network call.
    const tx2 = db.transaction(STORE, 'readwrite');
    const store2 = tx2.objectStore(STORE);
    for (const op of pending) {
      await idbPut(store2, { ...op, status: 'syncing' });
    }

    let result;
    try {
      const res = await fetch('/api/sync', {
        method:  'POST',
        headers: {
          'Content-Type':  'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({ operations: pending }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      result = await res.json();
    } catch (err) {
      // Network error: revert all back to 'pending' for next attempt.
      const tx3 = db.transaction(STORE, 'readwrite');
      const s3 = tx3.objectStore(STORE);
      for (const op of pending) {
        await idbPut(s3, { ...op, status: 'pending', retry_count: op.retry_count + 1 });
      }
      _updateBadge();
      return { synced: 0, errors: [{ error: err.message }] };
    }

    // Mark individual results.
    const failedIds = new Set((result.errors || []).map(e => e.id));
    const tx4 = db.transaction(STORE, 'readwrite');
    const s4 = tx4.objectStore(STORE);
    for (const op of pending) {
      if (failedIds.has(op.id)) {
        const err = result.errors.find(e => e.id === op.id);
        await idbPut(s4, { ...op, status: 'error', error: err?.error || 'unknown' });
      } else {
        await idbPut(s4, { ...op, status: 'synced' });
      }
    }

    _updateBadge();
    return result;
  }

  /**
   * Returns the count of operations still in 'pending' status.
   */
  async function getPendingCount() {
    const db = await openDB();
    const tx = db.transaction(STORE, 'readonly');
    return new Promise((resolve, reject) => {
      const req = tx.objectStore(STORE).index('status').count('pending');
      req.onsuccess = e => resolve(e.target.result);
      req.onerror   = e => reject(e.target.error);
    });
  }

  // ── Offline badge (optional UI indicator) ─────────────────────────────────

  async function _updateBadge() {
    const count = await getPendingCount();
    const badge = document.getElementById('pending-ops-badge');
    if (!badge) return;
    badge.textContent  = count > 0 ? count : '';
    badge.style.display = count > 0 ? 'inline-block' : 'none';
  }

  // ── Event listeners ───────────────────────────────────────────────────────

  window.addEventListener('online',  () => flush().then(_updateBadge));
  window.addEventListener('offline', () => {
    const ind = document.getElementById('offline-indicator');
    if (ind) ind.style.display = 'flex';
  });
  window.addEventListener('online', () => {
    const ind = document.getElementById('offline-indicator');
    if (ind) ind.style.display = 'none';
  });

  // Listen for SW_SYNC_NOW message from Service Worker background sync.
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', e => {
      if (e.data && e.data.type === 'SW_SYNC_NOW') flush();
    });
  }

  // Auto-flush on page load if online and there are pending ops.
  window.addEventListener('DOMContentLoaded', () => {
    if (navigator.onLine) flush();
    _updateBadge();
  });

  return { enqueue, flush, getPendingCount };
})();

// ── Service Worker registration ───────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js', { scope: '/' })
      .then(reg => console.log('[Mesio SW] registered, scope:', reg.scope))
      .catch(err => console.warn('[Mesio SW] registration failed:', err));
  });
}
