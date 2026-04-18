/**
 * app/static/js/internal/superadmin.js
 *
 * Org/Location management for /internal/superadmin
 * Tab 1: Organizaciones | Tab 2: Sedes
 *
 * Uses mesio-utils.js: mesioToast, mesioConfirm, _escHtml (via window._escHtml fallback)
 * Auth: Bearer session token stored in sessionStorage under SESSION_KEY (hq_session).
 */
(function () {
  "use strict";

  // ── Constants ───────────────────────────────────────────────────────────────
  const SESSION_KEY = "hq_session";
  const API = "/api/internal/admin";

  // ── Helpers ─────────────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const escHtml = (s) =>
    String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  function getToken() {
    return sessionStorage.getItem(SESSION_KEY) || "";
  }

  async function hqFetch(path, opts = {}) {
    opts.headers = Object.assign({}, opts.headers || {}, {
      Authorization: "Bearer " + getToken(),
    });
    if (opts.body && typeof opts.body === "object" && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const r = await fetch(API + path, opts);
    return r;
  }

  function toast(msg, type = "success") {
    if (window.mesioToast) {
      mesioToast(msg, type);
    } else {
      alert(msg);
    }
  }

  async function confirm(msg) {
    if (window.mesioConfirm) {
      return mesioConfirm(msg);
    }
    return window.confirm(msg);
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    return new Date(iso).toLocaleDateString("es-CO", { day: "numeric", month: "short", year: "numeric" });
  }

  // ── State ────────────────────────────────────────────────────────────────────
  let ALL_ORGS = [];
  let ALL_LOCATIONS = [];
  let _locOrgFilter = null; // null = all orgs

  // ── Tab switching ────────────────────────────────────────────────────────────
  window.switchOrgTab = function (tabId) {
    document.querySelectorAll(".org-tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".org-tab-panel").forEach((p) => p.classList.remove("active"));
    const btn = $("orgtab-btn-" + tabId);
    const panel = $("orgtab-" + tabId);
    if (btn) btn.classList.add("active");
    if (panel) panel.classList.add("active");
    if (tabId === "orgs") loadOrganizations();
    if (tabId === "sedes") loadAllLocations(_locOrgFilter);
  };

  // ── Load Organizations ───────────────────────────────────────────────────────
  window.loadOrganizations = async function () {
    const tbody = $("orgs-tbody");
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Cargando...</td></tr>';
    try {
      const r = await hqFetch("/organizations");
      if (!r.ok) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Error al cargar</td></tr>';
        return;
      }
      const d = await r.json();
      ALL_ORGS = (d.data && d.data.organizations) || [];
      renderOrgsTable(ALL_ORGS);
      // Populate org filter dropdown in Sedes tab
      const sel = $("sedes-org-filter");
      if (sel) {
        const prev = sel.value;
        sel.innerHTML =
          '<option value="">Todas las organizaciones</option>' +
          ALL_ORGS.map(
            (o) =>
              '<option value="' +
              o.id +
              '"' +
              (String(o.id) === String(prev) ? " selected" : "") +
              ">" +
              escHtml(o.name) +
              "</option>"
          ).join("");
      }
      // Populate new-location org select
      const orgSel = $("new-loc-org-id");
      if (orgSel) {
        orgSel.innerHTML =
          '<option value="">Selecciona organización...</option>' +
          ALL_ORGS.map(
            (o) => '<option value="' + o.id + '">' + escHtml(o.name) + "</option>"
          ).join("");
      }
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Error de conexión</td></tr>';
    }
  };

  function renderOrgsTable(orgs) {
    const tbody = $("orgs-tbody");
    if (!orgs.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Sin organizaciones</td></tr>';
      return;
    }
    const rows = orgs.map((o) => {
      const active = o.subscription_status === "active";
      const statusBadge = active
        ? '<span class="badge badge-active">Activo</span>'
        : '<span class="badge badge-suspended">' + escHtml(o.subscription_status || "—") + "</span>";
      const planBadge =
        '<span class="badge badge-chip plan-' +
        escHtml(o.subscription_plan || "free") +
        '">' +
        escHtml(o.subscription_plan || "free") +
        "</span>";
      const tr = document.createElement("tr");
      // ID
      const tdId = document.createElement("td");
      tdId.style.cssText = "color:#aaa;font-size:12px;";
      tdId.textContent = "#" + o.id;
      // Name
      const tdName = document.createElement("td");
      const strong = document.createElement("strong");
      strong.textContent = o.name;
      tdName.appendChild(strong);
      // Slug
      const tdSlug = document.createElement("td");
      tdSlug.style.cssText = "font-size:12px;color:#888;";
      tdSlug.textContent = o.slug || "—";
      // WhatsApp
      const tdWa = document.createElement("td");
      tdWa.style.cssText = "font-size:12px;color:#888;";
      tdWa.textContent = o.whatsapp_number || "—";
      // Plan
      const tdPlan = document.createElement("td");
      tdPlan.innerHTML = planBadge;
      // Sedes
      const tdSedes = document.createElement("td");
      const sedesBtn = document.createElement("button");
      sedesBtn.className = "btn btn-secondary btn-sm";
      sedesBtn.textContent = (o.location_count || 0) + " sede(s)";
      sedesBtn.onclick = function () {
        _locOrgFilter = o.id;
        switchOrgTab("sedes");
        const sel = $("sedes-org-filter");
        if (sel) sel.value = String(o.id);
      };
      tdSedes.appendChild(sedesBtn);
      // Status
      const tdStatus = document.createElement("td");
      tdStatus.innerHTML = statusBadge;
      // Created
      const tdCreated = document.createElement("td");
      tdCreated.style.cssText = "font-size:11px;color:#aaa;";
      tdCreated.textContent = fmtDate(o.created_at);
      // Actions
      const tdActions = document.createElement("td");
      tdActions.style.cssText = "white-space:nowrap;";

      const editBtn = document.createElement("button");
      editBtn.className = "btn btn-secondary btn-sm";
      editBtn.textContent = "Editar";
      editBtn.style.marginRight = "4px";
      editBtn.onclick = function () { openOrgModal(o.id); };

      const toggleBtn = document.createElement("button");
      toggleBtn.className = active ? "btn btn-secondary btn-sm" : "btn btn-primary btn-sm";
      toggleBtn.textContent = active ? "Suspender" : "Activar";
      toggleBtn.style.marginRight = "4px";
      toggleBtn.onclick = function () { toggleOrgStatus(o.id, active ? "suspended" : "active"); };

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger btn-sm";
      delBtn.textContent = "Eliminar";
      delBtn.onclick = function () { deleteOrg(o.id); };

      tdActions.appendChild(editBtn);
      tdActions.appendChild(toggleBtn);
      tdActions.appendChild(delBtn);

      tr.appendChild(tdId);
      tr.appendChild(tdName);
      tr.appendChild(tdSlug);
      tr.appendChild(tdWa);
      tr.appendChild(tdPlan);
      tr.appendChild(tdSedes);
      tr.appendChild(tdStatus);
      tr.appendChild(tdCreated);
      tr.appendChild(tdActions);
      return tr;
    });
    tbody.innerHTML = "";
    rows.forEach((r) => tbody.appendChild(r));
  }

  // ── Load Locations ───────────────────────────────────────────────────────────
  window.loadAllLocations = async function (orgId) {
    _locOrgFilter = orgId || null;
    const tbody = $("sedes-tbody");
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Cargando...</td></tr>';
    try {
      let url = orgId ? "/organizations/" + orgId + "/locations" : "/organizations";
      let locations = [];

      if (orgId) {
        const r = await hqFetch(url);
        if (!r.ok) { tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Error al cargar</td></tr>'; return; }
        const d = await r.json();
        locations = (d.data && d.data.locations) || [];
      } else {
        // Collect from all orgs (or use a flat list if available)
        if (!ALL_ORGS.length) {
          const r = await hqFetch("/organizations");
          if (r.ok) { const d = await r.json(); ALL_ORGS = (d.data && d.data.organizations) || []; }
        }
        for (const org of ALL_ORGS) {
          const r = await hqFetch("/organizations/" + org.id + "/locations");
          if (r.ok) {
            const d = await r.json();
            const locs = (d.data && d.data.locations) || [];
            locs.forEach((l) => { l._org_name = org.name; });
            locations = locations.concat(locs);
          }
        }
      }

      ALL_LOCATIONS = locations;
      renderSedesTable(locations);
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Error de conexión</td></tr>';
    }
  };

  function _orgName(orgId) {
    const o = ALL_ORGS.find((x) => x.id === orgId);
    return o ? o.name : "Org #" + orgId;
  }

  function renderSedesTable(locations) {
    const tbody = $("sedes-tbody");
    if (!locations.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Sin sedes</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    locations.forEach((loc) => {
      const tr = document.createElement("tr");

      const tdId = document.createElement("td");
      tdId.style.cssText = "color:#aaa;font-size:12px;";
      tdId.textContent = "#" + loc.id;

      const tdOrg = document.createElement("td");
      tdOrg.style.cssText = "font-size:12px;";
      tdOrg.textContent = loc._org_name || _orgName(loc.org_id);

      const tdName = document.createElement("td");
      const strong = document.createElement("strong");
      strong.textContent = loc.name;
      tdName.appendChild(strong);

      const tdCode = document.createElement("td");
      tdCode.style.cssText = "font-size:12px;color:#888;";
      tdCode.textContent = loc.code || "—";

      const tdAddr = document.createElement("td");
      tdAddr.style.cssText = "font-size:12px;color:#888;max-width:200px;";
      tdAddr.textContent = loc.address || "—";

      const tdWa = document.createElement("td");
      tdWa.style.cssText = "font-size:12px;color:#888;";
      tdWa.textContent = loc.whatsapp_number || "hereda";

      const tdPrimary = document.createElement("td");
      tdPrimary.innerHTML = loc.is_primary
        ? '<span class="badge badge-active">Principal</span>'
        : '<span class="badge-chip">No</span>';

      const tdActive = document.createElement("td");
      tdActive.innerHTML = loc.active
        ? '<span class="badge badge-active">Activa</span>'
        : '<span class="badge badge-suspended">Inactiva</span>';

      const tdActions = document.createElement("td");
      tdActions.style.cssText = "white-space:nowrap;";

      const editBtn = document.createElement("button");
      editBtn.className = "btn btn-secondary btn-sm";
      editBtn.textContent = "Editar";
      editBtn.style.marginRight = "4px";
      editBtn.onclick = function () { openLocationModal(loc.org_id, loc.id); };

      if (!loc.is_primary) {
        const promBtn = document.createElement("button");
        promBtn.className = "btn btn-secondary btn-sm";
        promBtn.textContent = "Hacer principal";
        promBtn.style.marginRight = "4px";
        promBtn.onclick = function () { setPrimaryLocation(loc.id); };
        tdActions.appendChild(promBtn);
      }

      const toggleBtn = document.createElement("button");
      toggleBtn.className = loc.active ? "btn btn-secondary btn-sm" : "btn btn-primary btn-sm";
      toggleBtn.textContent = loc.active ? "Desactivar" : "Activar";
      toggleBtn.style.marginRight = "4px";
      toggleBtn.onclick = function () { toggleLocationActive(loc.id, !loc.active); };

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger btn-sm";
      delBtn.textContent = "Eliminar";
      delBtn.onclick = function () { deleteLocation(loc.id); };

      tdActions.appendChild(editBtn);
      tdActions.appendChild(toggleBtn);
      tdActions.appendChild(delBtn);

      tr.appendChild(tdId);
      tr.appendChild(tdOrg);
      tr.appendChild(tdName);
      tr.appendChild(tdCode);
      tr.appendChild(tdAddr);
      tr.appendChild(tdWa);
      tr.appendChild(tdPrimary);
      tr.appendChild(tdActive);
      tr.appendChild(tdActions);
      tbody.appendChild(tr);
    });
  }

  // ── Org Modal ────────────────────────────────────────────────────────────────
  window.openOrgModal = function (orgId) {
    const modal = $("org-modal");
    if (!modal) return;
    $("org-modal-title").textContent = orgId ? "Editar organización" : "Nueva organización";
    $("org-modal-org-id").value = orgId || "";

    if (orgId) {
      const org = ALL_ORGS.find((o) => o.id === orgId);
      if (org) {
        $("om-name").value = org.name || "";
        $("om-slug").value = org.slug || "";
        $("om-whatsapp").value = org.whatsapp_number || "";
        $("om-wa-phone-id").value = org.wa_phone_id || "";
        $("om-wa-token").value = "";
        $("om-plan").value = org.subscription_plan || "free";
      }
    } else {
      ["om-name", "om-slug", "om-whatsapp", "om-wa-phone-id", "om-wa-token"].forEach((id) => { const el = $(id); if (el) el.value = ""; });
      const planSel = $("om-plan");
      if (planSel) planSel.value = "free";
    }
    $("org-modal-error").textContent = "";
    modal.style.display = "flex";
  };

  window.closeOrgModal = function () {
    const modal = $("org-modal");
    if (modal) modal.style.display = "none";
  };

  window.submitOrgModal = async function () {
    const orgId = $("org-modal-org-id").value;
    const name = $("om-name").value.trim();
    const slug = $("om-slug").value.trim();
    const whatsapp = $("om-whatsapp").value.trim();
    const waPhoneId = $("om-wa-phone-id").value.trim();
    const waToken = $("om-wa-token").value.trim();
    const plan = $("om-plan").value;
    const errEl = $("org-modal-error");

    if (!name) { errEl.textContent = "El nombre es requerido."; return; }

    const body = { name };
    if (slug) body.slug = slug;
    if (whatsapp) body.whatsapp_number = whatsapp;
    if (waPhoneId) body.wa_phone_id = waPhoneId;
    if (waToken) body.wa_access_token = waToken;
    if (plan) body.subscription_plan = plan;

    try {
      let r;
      if (orgId) {
        r = await hqFetch("/organizations/" + orgId, { method: "PATCH", body });
      } else {
        r = await hqFetch("/organizations", { method: "POST", body });
      }
      if (r.ok) {
        closeOrgModal();
        toast(orgId ? "Organización actualizada" : "Organización creada");
        await loadOrganizations();
      } else {
        const d = await r.json().catch(() => ({}));
        errEl.textContent = d.detail || d.error || "Error al guardar";
      }
    } catch (e) {
      errEl.textContent = "Error de conexión";
    }
  };

  // ── Toggle org status ────────────────────────────────────────────────────────
  async function toggleOrgStatus(orgId, newStatus) {
    const label = newStatus === "suspended" ? "suspender" : "activar";
    const ok = await confirm("¿Deseas " + label + " esta organización?");
    if (!ok) return;
    try {
      const r = await hqFetch("/organizations/" + orgId, {
        method: "PATCH",
        body: { subscription_status: newStatus },
      });
      if (r.ok) {
        toast("Estado actualizado");
        loadOrganizations();
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || "Error al actualizar", "error");
      }
    } catch (e) {
      toast("Error de conexión", "error");
    }
  }

  // ── Delete org ───────────────────────────────────────────────────────────────
  window.deleteOrg = async function (orgId) {
    const ok = await confirm(
      "¿Eliminar esta organización? (soft-delete: se suspende y todas las sedes se desactivan)"
    );
    if (!ok) return;
    try {
      const r = await hqFetch("/organizations/" + orgId, { method: "DELETE" });
      if (r.ok) {
        toast("Organización eliminada (soft-delete)");
        loadOrganizations();
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || d.error || "Error al eliminar", "error");
      }
    } catch (e) {
      toast("Error de conexión", "error");
    }
  };

  // ── Location Modal ───────────────────────────────────────────────────────────
  window.openLocationModal = async function (orgId, locationId) {
    const modal = $("loc-modal");
    if (!modal) return;
    $("loc-modal-title").textContent = locationId ? "Editar sede" : "Nueva sede";
    $("loc-modal-loc-id").value = locationId || "";
    $("loc-modal-org-id").value = orgId || "";
    $("loc-modal-error").textContent = "";

    // Reset fields
    ["lm-name", "lm-code", "lm-address", "lm-lat", "lm-lon", "lm-whatsapp", "lm-wa-phone-id", "lm-wa-token"].forEach(
      (id) => { const el = $(id); if (el) el.value = ""; }
    );
    const tzSel = $("lm-timezone");
    if (tzSel) tzSel.value = "America/Bogota";
    const activeCb = $("lm-active");
    if (activeCb) activeCb.checked = true;

    if (locationId) {
      // Pre-fill with existing data from local cache
      let loc = ALL_LOCATIONS.find((l) => l.id === locationId);
      if (!loc) {
        // Fetch if not in cache
        try {
          const r = await hqFetch("/organizations/" + orgId + "/locations");
          if (r.ok) {
            const d = await r.json();
            loc = ((d.data && d.data.locations) || []).find((l) => l.id === locationId);
          }
        } catch (e) {}
      }
      if (loc) {
        const nm = $("lm-name"); if (nm) nm.value = loc.name || "";
        const cd = $("lm-code"); if (cd) cd.value = loc.code || "";
        const ad = $("lm-address"); if (ad) ad.value = loc.address || "";
        const lt = $("lm-lat"); if (lt) lt.value = loc.latitude != null ? loc.latitude : "";
        const ln = $("lm-lon"); if (ln) ln.value = loc.longitude != null ? loc.longitude : "";
        const wn = $("lm-whatsapp"); if (wn) wn.value = loc.whatsapp_number || "";
        const wp = $("lm-wa-phone-id"); if (wp) wp.value = loc.wa_phone_id || "";
        const tz = $("lm-timezone"); if (tz) tz.value = loc.timezone || "America/Bogota";
        const ac = $("lm-active"); if (ac) ac.checked = !!loc.active;
      }
    }

    // Populate org selector in new-location form
    const orgSel = $("lm-org-select");
    if (orgSel) {
      orgSel.innerHTML =
        '<option value="">Selecciona org...</option>' +
        ALL_ORGS.map(
          (o) =>
            '<option value="' +
            o.id +
            '"' +
            (String(o.id) === String(orgId) ? " selected" : "") +
            ">" +
            escHtml(o.name) +
            "</option>"
        ).join("");
      orgSel.value = String(orgId || "");
    }

    modal.style.display = "flex";
  };

  window.closeLocModal = function () {
    const modal = $("loc-modal");
    if (modal) modal.style.display = "none";
  };

  window.submitLocModal = async function () {
    const locId = $("loc-modal-loc-id").value;
    const orgSel = $("lm-org-select");
    const orgId = orgSel ? orgSel.value : $("loc-modal-org-id").value;
    const name = $("lm-name").value.trim();
    const errEl = $("loc-modal-error");

    if (!name) { errEl.textContent = "El nombre es requerido."; return; }
    if (!orgId) { errEl.textContent = "Selecciona una organización."; return; }

    const body = { name };
    const code = $("lm-code").value.trim();
    const address = $("lm-address").value.trim();
    const lat = $("lm-lat").value.trim();
    const lon = $("lm-lon").value.trim();
    const wa = $("lm-whatsapp").value.trim();
    const waPhoneId = $("lm-wa-phone-id").value.trim();
    const waToken = $("lm-wa-token").value.trim();
    const tz = $("lm-timezone") ? $("lm-timezone").value : null;
    const active = $("lm-active") ? $("lm-active").checked : true;

    if (code) body.code = code;
    if (address) body.address = address;
    if (lat) body.latitude = parseFloat(lat);
    if (lon) body.longitude = parseFloat(lon);
    if (wa) body.whatsapp_number = wa;
    if (waPhoneId) body.wa_phone_id = waPhoneId;
    if (waToken) body.wa_access_token = waToken;
    if (tz) body.timezone = tz;
    body.active = active;

    try {
      let r;
      if (locId) {
        r = await hqFetch("/locations/" + locId, { method: "PATCH", body });
      } else {
        r = await hqFetch("/organizations/" + orgId + "/locations", { method: "POST", body });
      }
      if (r.ok) {
        closeLocModal();
        toast(locId ? "Sede actualizada" : "Sede creada");
        loadAllLocations(_locOrgFilter);
      } else {
        const d = await r.json().catch(() => ({}));
        errEl.textContent = d.detail || d.error || "Error al guardar";
      }
    } catch (e) {
      errEl.textContent = "Error de conexión";
    }
  };

  // ── Promote primary ───────────────────────────────────────────────────────────
  window.setPrimaryLocation = async function (locationId) {
    const ok = await confirm(
      "¿Marcar esta sede como principal? La sede principal actual perderá ese estado."
    );
    if (!ok) return;
    try {
      const r = await hqFetch("/locations/" + locationId, {
        method: "PATCH",
        body: { is_primary: true },
      });
      if (r.ok) {
        toast("Sede principal actualizada");
        loadAllLocations(_locOrgFilter);
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || d.error || "Error", "error");
      }
    } catch (e) {
      toast("Error de conexión", "error");
    }
  };

  // ── Toggle location active ────────────────────────────────────────────────────
  async function toggleLocationActive(locationId, newActive) {
    try {
      const r = await hqFetch("/locations/" + locationId, {
        method: "PATCH",
        body: { active: newActive },
      });
      if (r.ok) {
        toast(newActive ? "Sede activada" : "Sede desactivada");
        loadAllLocations(_locOrgFilter);
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || "Error", "error");
      }
    } catch (e) {
      toast("Error de conexión", "error");
    }
  }

  // ── Delete location ──────────────────────────────────────────────────────────
  window.deleteLocation = async function (locationId) {
    const ok = await confirm("¿Eliminar esta sede? (soft-delete: se desactivará)");
    if (!ok) return;
    try {
      const r = await hqFetch("/locations/" + locationId, { method: "DELETE" });
      if (r.ok) {
        toast("Sede eliminada (soft-delete)");
        loadAllLocations(_locOrgFilter);
      } else {
        const d = await r.json().catch(() => ({}));
        toast(d.detail || d.error || "Error al eliminar", "error");
      }
    } catch (e) {
      toast("Error de conexión", "error");
    }
  };

  // ── Filter handler ────────────────────────────────────────────────────────────
  window.onSedesOrgFilterChange = function () {
    const sel = $("sedes-org-filter");
    const orgId = sel ? (sel.value ? parseInt(sel.value) : null) : null;
    loadAllLocations(orgId);
  };

  // ── Init (called from superadmin.html after app is shown) ─────────────────────
  window.initOrgLocationTabs = function () {
    // Default: show Organizaciones tab
    switchOrgTab("orgs");
  };
})();
