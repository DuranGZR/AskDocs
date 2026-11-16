(() => {
  "use strict";

  const TENANT_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;
  const DEFAULT_TENANT = "test-user";
  const STORAGE_TENANT_KEY = "corerag.tenantId";
  const STORAGE_TENANTS_LIST_KEY = "corerag.savedTenants";
  const STORAGE_API_KEY = "corerag.apiKey";
  const STORAGE_CHAT_PREFIX = "corerag.chat.";
  const POLL_INTERVAL_MS = 900;
  const MAX_CONCURRENT_UPLOADS = 2;
  const MAX_HISTORY_MESSAGES = 10;
  const MAX_SAVED_UI_TURNS = 24;
  const HEALTH_POLL_INTERVAL_MS = 30000;
  const HEALTH_MIN_REFRESH_GAP_MS = 5000;

  const initialTenant = loadInitialTenant();
  const state = {
    tenantId: initialTenant,
    savedTenants: loadSavedTenants(initialTenant),
    apiKey: loadInitialApiKey(),
    apiKeyVerified: false,
    documents: [],
    docNameById: new Map(),
    selectedDocumentIds: new Set(),
    uploads: new Map(),
    uploadQueue: [],
    activeUploadCount: 0,
    confirmDeleteId: null,
    confirmTimerId: null,
    isStreaming: false,
    streamController: null,
    hasMessages: false,
    chatHistory: [],
    uiTurns: [],
    promptHistory: [],
    promptCursor: -1,
    inspector: {
      groups: [],
      activeGroupIndex: 0,
      focusCitationIndex: null,
      queryText: "",
    },
    healthStatus: "ok",
    openCircuits: [],
    lastHealthCheckAt: 0,
    healthPollTimerId: null,
    dragCounter: 0,
  };

  const dom = {
    appShell: document.getElementById("app-shell"),
    sidebar: document.getElementById("sidebar"),
    sidebarBackdrop: document.getElementById("sidebar-backdrop"),
    mobileOpenBtn: document.getElementById("mobile-sidebar-open"),
    mobileCloseBtn: document.getElementById("mobile-sidebar-close"),
    dropzone: document.getElementById("dropzone"),
    fileInput: document.getElementById("file-input"),
    selectFilesBtn: document.getElementById("select-files-btn"),
    clearScopeBtn: document.getElementById("clear-scope-btn"),
    uploadSummaryBar: document.getElementById("upload-summary-bar"),
    docList: document.getElementById("doc-list"),
    docCountBadge: document.getElementById("doc-count-badge"),
    topbarDocSummary: document.getElementById("topbar-doc-summary"),
    sidebarFooter: document.getElementById("sidebar-footer"),
    workspaceMenu: document.getElementById("workspace-menu"),
    workspaceList: document.getElementById("workspace-list"),
    workspaceToggleBtn: document.getElementById("workspace-toggle-btn"),
    workspaceNameLabel: document.getElementById("workspace-name-label"),
    workspaceForm: document.getElementById("workspace-form"),
    workspaceInput: document.getElementById("workspace-input"),
    clearChatBtn: document.getElementById("clear-chat-btn"),
    apiKeyBtn: document.getElementById("api-key-btn"),
    apiKeyBtnLabel: document.getElementById("api-key-btn-label"),
    apiKeyModal: document.getElementById("api-key-modal"),
    apiKeyForm: document.getElementById("api-key-form"),
    apiKeyInput: document.getElementById("api-key-input"),
    apiKeyVisibilityBtn: document.getElementById("api-key-visibility-btn"),
    apiKeyError: document.getElementById("api-key-error"),
    apiKeyClearBtn: document.getElementById("api-key-clear-btn"),
    apiKeyCancelBtn: document.getElementById("api-key-cancel-btn"),
    apiKeySaveBtn: document.getElementById("api-key-save-btn"),
    statusDot: document.getElementById("status-dot"),
    statusText: document.getElementById("status-text"),
    chatScroll: document.getElementById("chat-scroll"),
    emptyState: document.getElementById("empty-state"),
    emptyDesc: document.getElementById("empty-desc"),
    starterPrompts: document.getElementById("starter-prompts"),
    messagesList: document.getElementById("messages-list"),
    scopeChipsBar: document.getElementById("scope-chips-bar"),
    queryForm: document.getElementById("query-form"),
    queryInput: document.getElementById("query-input"),
    sendBtn: document.getElementById("send-btn"),
    sendIcon: document.querySelector("#send-btn .icon-send"),
    stopIcon: document.querySelector("#send-btn .icon-stop"),
    toastStack: document.getElementById("toast-stack"),
    windowDropOverlay: document.getElementById("window-drop-overlay"),
    inspectorBackdrop: document.getElementById("inspector-backdrop"),
    inspectorDrawer: document.getElementById("inspector-drawer"),
    inspectorTitle: document.getElementById("inspector-title"),
    inspectorSubtitle: document.getElementById("inspector-subtitle"),
    inspectorPrevBtn: document.getElementById("inspector-prev-btn"),
    inspectorNextBtn: document.getElementById("inspector-next-btn"),
    inspectorCloseBtn: document.getElementById("inspector-close-btn"),
    inspectorBody: document.getElementById("inspector-body"),
  };

  function loadInitialApiKey() {
    try {
      const saved = window.localStorage.getItem(STORAGE_API_KEY);
      return saved ? saved.trim() : "";
    } catch {
      return "";
    }
  }

  function persistApiKey(value) {
    state.apiKey = value.trim();
    try {
      if (state.apiKey) {
        window.localStorage.setItem(STORAGE_API_KEY, state.apiKey);
      } else {
        window.localStorage.removeItem(STORAGE_API_KEY);
      }
    } catch {
      // Depolama erişimi kapalıysa bellek içinde devam et
    }
    updateApiKeyButtonState();
  }

  function updateApiKeyButtonState() {
    if (!dom.apiKeyBtn || !dom.apiKeyBtnLabel) {
      return;
    }
    dom.apiKeyBtn.classList.remove("is-missing", "is-verified");
    if (!state.apiKey) {
      dom.apiKeyBtn.classList.add("is-missing");
      dom.apiKeyBtnLabel.textContent = "API Anahtarı Gerekli";
    } else if (state.apiKeyVerified) {
      dom.apiKeyBtn.classList.add("is-verified");
      dom.apiKeyBtnLabel.textContent = "API Anahtarı Bağlı";
    } else {
      dom.apiKeyBtnLabel.textContent = "API Anahtarı";
    }
  }

  function openApiKeyModal(errorMessage = "") {
    if (!dom.apiKeyModal) {
      return;
    }
    dom.apiKeyInput.value = state.apiKey;
    dom.apiKeyInput.type = "password";
    dom.apiKeyVisibilityBtn.textContent = "Göster";
    dom.apiKeyClearBtn.classList.toggle("hidden", !state.apiKey);
    setApiKeyModalError(errorMessage);
    dom.apiKeyModal.classList.remove("hidden");
    window.setTimeout(() => dom.apiKeyInput.focus(), 40);
  }

  function closeApiKeyModal() {
    if (!dom.apiKeyModal) {
      return;
    }
    dom.apiKeyModal.classList.add("hidden");
    setApiKeyModalError("");
  }

  function setApiKeyModalError(message) {
    if (!dom.apiKeyError) {
      return;
    }
    if (message) {
      dom.apiKeyError.textContent = message;
      dom.apiKeyError.classList.remove("hidden");
    } else {
      dom.apiKeyError.textContent = "";
      dom.apiKeyError.classList.add("hidden");
    }
  }

  function buildAuthHeaders(extraHeaders = {}) {
    const headers = { ...extraHeaders };
    if (state.apiKey) {
      headers["X-API-Key"] = state.apiKey;
    }
    return headers;
  }

  async function apiFetch(url, options = {}) {
    const headers = buildAuthHeaders(options.headers || {});
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) {
      state.apiKeyVerified = false;
      updateApiKeyButtonState();
      openApiKeyModal("Geçerli bir API anahtarı girilmeden işlem yapılamaz.");
    } else if (response.status === 503) {
      void scheduleHealthRefresh();
    } else if (response.ok) {
      state.apiKeyVerified = true;
      updateApiKeyButtonState();
    }
    return response;
  }

  function loadInitialTenant() {
    try {
      const saved = window.localStorage.getItem(STORAGE_TENANT_KEY);
      if (saved && TENANT_PATTERN.test(saved.trim())) {
        return saved.trim();
      }
    } catch {
      // localStorage erişimi kapalıysa varsayılan tenant ile devam et
    }
    return DEFAULT_TENANT;
  }

  function loadSavedTenants(currentTenant) {
    const merged = new Set([DEFAULT_TENANT, currentTenant]);
    try {
      const raw = window.localStorage.getItem(STORAGE_TENANTS_LIST_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          for (const item of parsed) {
            if (typeof item === "string" && TENANT_PATTERN.test(item.trim())) {
              merged.add(item.trim());
            }
          }
        }
      }
    } catch {
      // Bozuk veya kapalı localStorage durumunda varsayılanla devam et
    }
    return Array.from(merged).sort((a, b) => a.localeCompare(b));
  }

  function saveTenant(value) {
    if (!state.savedTenants.includes(value)) {
      state.savedTenants = [...state.savedTenants, value].sort((a, b) => a.localeCompare(b));
    }
    try {
      window.localStorage.setItem(STORAGE_TENANT_KEY, value);
      window.localStorage.setItem(STORAGE_TENANTS_LIST_KEY, JSON.stringify(state.savedTenants));
    } catch {
      // Depolama hatasını yok say
    }
  }

  async function fetchKnownTenants() {
    if (!state.apiKey) {
      return;
    }
    try {
      const response = await apiFetch("/api/v1/documents/tenants");
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      if (Array.isArray(data.tenants)) {
        const merged = new Set([DEFAULT_TENANT, state.tenantId, ...state.savedTenants]);
        for (const t of data.tenants) {
          if (typeof t === "string" && TENANT_PATTERN.test(t.trim())) {
            merged.add(t.trim());
          }
        }
        state.savedTenants = Array.from(merged).sort((a, b) => a.localeCompare(b));
        renderWorkspaceList();
      }
    } catch {
      // Arka uç tenant keşfi fail-open çalışır; yerel liste korunur
    }
  }

  function renderWorkspaceList() {
    dom.workspaceList.replaceChildren();
    for (const tenant of state.savedTenants) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.role = "menuitemradio";
      btn.setAttribute("aria-checked", tenant === state.tenantId ? "true" : "false");
      btn.className = tenant === state.tenantId ? "workspace-option is-selected" : "workspace-option";

      const nameSpan = document.createElement("span");
      nameSpan.textContent = tenant;
      btn.appendChild(nameSpan);

      if (tenant === state.tenantId) {
        const check = document.createElement("span");
        check.className = "workspace-option-check";
        check.textContent = "✓";
        btn.appendChild(check);
      }

      btn.addEventListener("click", () => {
        switchWorkspace(tenant);
      });
      dom.workspaceList.appendChild(btn);
    }
  }

  function closeWorkspaceMenu() {
    dom.workspaceMenu.classList.add("hidden");
    dom.workspaceToggleBtn.setAttribute("aria-expanded", "false");
  }

  function toggleWorkspaceMenu() {
    const willOpen = dom.workspaceMenu.classList.contains("hidden");
    if (willOpen) {
      renderWorkspaceList();
      fetchKnownTenants();
      dom.workspaceMenu.classList.remove("hidden");
      dom.workspaceToggleBtn.setAttribute("aria-expanded", "true");
    } else {
      closeWorkspaceMenu();
    }
  }

  function switchWorkspace(nextTenant) {
    closeWorkspaceMenu();
    if (nextTenant === state.tenantId) {
      return;
    }
    state.tenantId = nextTenant;
    state.selectedDocumentIds.clear();
    saveTenant(nextTenant);
    renderWorkspaceList();
    renderSummaryTexts();
    renderScopeChips();
    resetChatView();
    restoreChatTranscript();
    fetchDocuments();
    showToast(`Çalışma alanı: ${nextTenant}`);
  }

  function showToast(message, isError = false) {
    const pill = document.createElement("div");
    pill.className = isError ? "toast-pill is-error" : "toast-pill";
    pill.textContent = message;
    dom.toastStack.appendChild(pill);
    window.setTimeout(() => {
      pill.remove();
    }, 3400);
  }

  function updateSystemStatus() {
    const activeUploads = Array.from(state.uploads.values()).filter(
      (item) =>
        item.state === "queued" || item.state === "uploading" || item.state === "indexing"
    );
    const statusPill = dom.statusText.parentElement;

    dom.statusDot.classList.remove("is-ready", "is-busy", "is-degraded");
    if (state.isStreaming) {
      dom.statusDot.classList.add("is-busy");
      dom.statusText.textContent = "Yanıt Yazılıyor";
      if (statusPill) {
        statusPill.title = "Yanıt akışı aktif";
      }
    } else if (activeUploads.length > 0) {
      dom.statusDot.classList.add("is-busy");
      dom.statusText.textContent = "Doküman İşleniyor";
      if (statusPill) {
        statusPill.title = "Aktif doküman indeksleme işlemi sürüyor";
      }
    } else if (state.openCircuits.length > 0) {
      dom.statusDot.classList.add("is-degraded");
      const circuitsLabel = state.openCircuits.join(", ");
      dom.statusText.textContent = `Devre Açık (${circuitsLabel})`;
      if (statusPill) {
        statusPill.title = `Açık devre kesiciler: ${circuitsLabel}`;
      }
    } else if (state.healthStatus !== "ok") {
      dom.statusDot.classList.add("is-degraded");
      dom.statusText.textContent = "Sistem Kısıtlı";
      if (statusPill) {
        statusPill.title = "Bağımlılık sağlık kontrolü kısıtlı";
      }
    } else {
      dom.statusDot.classList.add("is-ready");
      dom.statusText.textContent = "Sistem Hazır";
      if (statusPill) {
        statusPill.title = "Tüm servisler ve devre kesiciler sağlıklı";
      }
    }
  }

  async function checkHealth() {
    state.lastHealthCheckAt = Date.now();
    try {
      const response = await fetch("/api/v1/health");
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      const circuits =
        data && typeof data.circuits === "object" && data.circuits !== null ? data.circuits : {};
      state.openCircuits = Object.entries(circuits)
        .filter(([, value]) => value === "open")
        .map(([name]) => name);
      const hasHalfOpen = Object.values(circuits).some((value) => value === "half_open");
      state.healthStatus =
        response.ok && data?.status === "ok" && state.openCircuits.length === 0 && !hasHalfOpen
          ? "ok"
          : "degraded";
    } catch {
      state.healthStatus = "degraded";
      state.openCircuits = [];
    }
    updateSystemStatus();
  }

  async function scheduleHealthRefresh() {
    if (Date.now() - state.lastHealthCheckAt < HEALTH_MIN_REFRESH_GAP_MS) {
      return;
    }
    await checkHealth();
  }

  function startHealthPolling() {
    if (state.healthPollTimerId) {
      window.clearInterval(state.healthPollTimerId);
    }
    state.healthPollTimerId = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void checkHealth();
      }
    }, HEALTH_POLL_INTERVAL_MS);
  }

  async function fetchDocuments() {
    if (!state.apiKey) {
      state.documents = [];
      state.docNameById = new Map();
      state.selectedDocumentIds.clear();
      renderDocumentList();
      renderScopeChips();
      renderStarterPrompts();
      dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı bekleniyor`;
      return;
    }
    const url = `/api/v1/documents?tenant_id=${encodeURIComponent(state.tenantId)}`;
    try {
      const response = await apiFetch(url);
      if (response.status === 401) {
        dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı doğrulanamadı`;
        return;
      }
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }
      const data = await response.json();
      state.documents = Array.isArray(data.documents) ? data.documents : [];
      state.docNameById = new Map(
        state.documents.map((doc) => [doc.document_id, doc.filename])
      );
      for (const selectedId of Array.from(state.selectedDocumentIds)) {
        if (!state.docNameById.has(selectedId)) {
          state.selectedDocumentIds.delete(selectedId);
        }
      }
      renderDocumentList();
      renderSummaryTexts();
      renderScopeChips();
      renderStarterPrompts();
    } catch (err) {
      dom.topbarDocSummary.textContent = "Doküman listesi yüklenemedi";
      showToast(err instanceof Error ? err.message : "Doküman listesi alınamadı", true);
    }
  }

  function renderSummaryTexts() {
    const docCount = state.documents.length;
    const totalChunks = state.documents.reduce((acc, doc) => acc + (doc.chunk_count || 0), 0);

    dom.docCountBadge.textContent = String(docCount);
    dom.workspaceNameLabel.textContent = state.tenantId;

    if (docCount === 0) {
      dom.topbarDocSummary.textContent = `${state.tenantId} • Henüz doküman yok`;
      dom.emptyDesc.textContent =
        "Sol panelden bir veya birden fazla PDF yükleyin; yüklenen belgeler yalnızca kendi içeriklerine dayanarak canlı yanıtlanır.";
    } else {
      dom.topbarDocSummary.textContent = `${docCount} doküman (${totalChunks} parça) • ${state.tenantId}`;
      dom.emptyDesc.textContent =
        `Şu anda ${docCount} doküman arama için hazır. Sorularınız yalnızca yüklü kaynaklarınızdaki doğrulanmış metinlere dayanarak yanıtlanır.`;
    }
  }

  function createTrashIconSvg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      "M3 4.5H13M6.2 4.5V3.2C6.2 2.81 6.51 2.5 6.9 2.5H9.1C9.49 2.5 9.8 2.81 9.8 3.2V4.5M5 4.5L5.5 12.6C5.53 13.11 5.95 13.5 6.46 13.5H9.54C10.05 13.5 10.47 13.11 10.5 12.6L11 4.5"
    );
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.35");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  function toggleDocumentScope(documentId) {
    if (state.selectedDocumentIds.has(documentId)) {
      state.selectedDocumentIds.delete(documentId);
    } else {
      state.selectedDocumentIds.add(documentId);
    }
    renderDocumentList();
    renderScopeChips();
  }

  function clearDocumentScope() {
    if (state.selectedDocumentIds.size === 0) {
      return;
    }
    state.selectedDocumentIds.clear();
    renderDocumentList();
    renderScopeChips();
  }

  function renderScopeChips() {
    if (dom.clearScopeBtn) {
      dom.clearScopeBtn.classList.toggle("hidden", state.selectedDocumentIds.size === 0);
    }
    if (!dom.scopeChipsBar) {
      return;
    }
    dom.scopeChipsBar.replaceChildren();
    if (state.selectedDocumentIds.size === 0) {
      dom.scopeChipsBar.classList.add("hidden");
      return;
    }

    const prefix = document.createElement("span");
    prefix.className = "scope-bar-label";
    prefix.textContent = "Kapsam:";
    dom.scopeChipsBar.appendChild(prefix);

    for (const docId of state.selectedDocumentIds) {
      const name = state.docNameById.get(docId) || `${docId.slice(0, 8)}.pdf`;
      const chip = document.createElement("span");
      chip.className = "scope-chip";

      const label = document.createElement("span");
      label.className = "scope-chip-name";
      label.textContent = name;

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "scope-chip-remove";
      removeBtn.setAttribute("aria-label", `${name} belgesini kapsamdan çıkar`);
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", () => toggleDocumentScope(docId));

      chip.append(label, removeBtn);
      dom.scopeChipsBar.appendChild(chip);
    }

    const resetBtn = document.createElement("button");
    resetBtn.type = "button";
    resetBtn.className = "scope-reset-link";
    resetBtn.textContent = "Tümünde ara";
    resetBtn.addEventListener("click", clearDocumentScope);
    dom.scopeChipsBar.appendChild(resetBtn);
    dom.scopeChipsBar.classList.remove("hidden");
  }

  function renderStarterPrompts() {
    if (!dom.starterPrompts) {
      return;
    }
    const docs = state.documents;
    if (docs.length === 0) {
      return;
    }

    const demoPromptByName = {
      "sirket_el_kitabi_tr.pdf": {
        label: "Yıllık izin hakkı ve uzaktan çalışma kuralları nedir?",
        prompt: "Yıllık izin hakkı kaç gün ve uzaktan çalışma kuralları nelerdir?",
      },
      "bitcoin_whitepaper.pdf": {
        label: "Bitcoin'de double-spending sorunu nasıl çözülüyor?",
        prompt: "Bitcoin whitepaper'ında çift harcama (double-spending) sorunu nasıl çözülüyor?",
      },
      "rag_paper_2005_11401.pdf": {
        label: "RAG mimarisinde bellek mekanizması nasıl çalışır?",
        prompt: "RAG (Retrieval-Augmented Generation) yaklaşımında parametrik ve parametrik olmayan bellek nasıl birleşir?",
      },
    };

    const items = [];
    const customDocs = docs.filter((doc) => !demoPromptByName[doc.filename]);
    if (customDocs.length > 0) {
      for (const doc of docs.slice(0, 3)) {
        const preset = demoPromptByName[doc.filename];
        if (preset) {
          items.push(preset);
        } else {
          items.push({
            label: `${doc.filename} belgesindeki temel konuları özetle`,
            prompt: `${doc.filename} belgesinin temel konusunu ve önemli maddelerini özetler misin?`,
          });
        }
      }
      if (items.length < 3 && docs[0]) {
        items.push({
          label: `${docs[0].filename} içinde öne çıkan kurallar veya bulgular neler?`,
          prompt: `${docs[0].filename} belgesinde öne çıkan temel kurallar, tanımlar veya bulgular nelerdir?`,
        });
      }
    } else {
      for (const doc of docs) {
        if (demoPromptByName[doc.filename]) {
          items.push(demoPromptByName[doc.filename]);
        }
      }
    }

    if (items.length === 0) {
      return;
    }
    dom.starterPrompts.replaceChildren();
    for (const entry of items.slice(0, 3)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "starter-chip";
      btn.setAttribute("data-prompt", entry.prompt);
      btn.textContent = entry.label;
      dom.starterPrompts.appendChild(btn);
    }
  }

  function renderUploadSummaryBar() {
    if (!dom.uploadSummaryBar) {
      return;
    }
    const items = Array.from(state.uploads.values()).filter(
      (job) => job.tenantId === state.tenantId
    );
    if (items.length === 0) {
      dom.uploadSummaryBar.classList.add("hidden");
      dom.uploadSummaryBar.textContent = "";
      return;
    }
    const activeCount = items.filter(
      (job) => job.state === "queued" || job.state === "uploading" || job.state === "indexing"
    ).length;
    const errorCount = items.filter((job) => job.state === "error").length;

    const parts = [];
    if (activeCount > 0) {
      parts.push(`${activeCount} dosya işleniyor`);
    }
    if (errorCount > 0) {
      parts.push(`${errorCount} hatalı`);
    }
    dom.uploadSummaryBar.textContent = parts.join(" • ") || `${items.length} yükleme`;
    dom.uploadSummaryBar.classList.toggle("is-error", activeCount === 0 && errorCount > 0);
    dom.uploadSummaryBar.classList.remove("hidden");
  }

  function renderDocumentList() {
    renderUploadSummaryBar();
    dom.docList.replaceChildren();

    const uploadItems = Array.from(state.uploads.values()).filter(
      (job) => job.tenantId === state.tenantId
    );
    for (const job of uploadItems) {
      dom.docList.appendChild(buildUploadRow(job));
    }

    if (state.documents.length === 0 && uploadItems.length === 0) {
      const hint = document.createElement("div");
      hint.className = "doc-empty-hint";
      hint.textContent = "Bu çalışma alanında henüz doküman yok. Yukarıdan PDF veya TXT sürükleyip bırakın.";
      dom.docList.appendChild(hint);
      return;
    }

    for (const doc of state.documents) {
      dom.docList.appendChild(buildIndexedDocRow(doc));
    }
  }

  function buildUploadRow(job) {
    const row = document.createElement("div");
    row.className = "doc-item is-uploading";

    const main = document.createElement("div");
    main.className = "doc-item-main";

    const badge = document.createElement("span");
    const ext = job.fileType === "txt" ? "txt" : "pdf";
    badge.className = `file-badge is-${ext}`;
    badge.textContent = ext.toUpperCase();

    const meta = document.createElement("div");
    meta.className = "doc-meta";

    const title = document.createElement("p");
    title.className = "doc-title";
    title.textContent = job.filename;
    title.title = job.filename;

    const sub = document.createElement("p");
    sub.className = job.state === "error" ? "doc-sub is-error" : "doc-sub";
    sub.textContent = job.phaseText;

    meta.append(title, sub);
    main.append(badge, meta);

    if (job.state === "error") {
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "doc-confirm-cancel";
      dismissBtn.textContent = "Kapat";
      dismissBtn.addEventListener("click", () => {
        state.uploads.delete(job.localId);
        renderDocumentList();
        updateSystemStatus();
      });
      main.appendChild(dismissBtn);
    }

    row.appendChild(main);

    if (job.state !== "error") {
      const track = document.createElement("div");
      track.className = "doc-progress-track";
      const fill = document.createElement("div");
      fill.className = job.isIndeterminate
        ? "doc-progress-fill is-indeterminate"
        : "doc-progress-fill";
      fill.style.width = `${Math.max(4, Math.min(100, job.progress))}%`;
      track.appendChild(fill);
      row.appendChild(track);
    }

    return row;
  }

  function buildIndexedDocRow(doc) {
    const isSelected = state.selectedDocumentIds.has(doc.document_id);
    const row = document.createElement("div");
    row.className = isSelected ? "doc-item is-selected" : "doc-item";
    row.dataset.documentId = doc.document_id;

    const main = document.createElement("div");
    main.className = "doc-item-main";

    const selectBtn = document.createElement("button");
    selectBtn.type = "button";
    selectBtn.role = "checkbox";
    selectBtn.setAttribute("aria-checked", isSelected ? "true" : "false");
    selectBtn.className = isSelected ? "doc-select-btn is-selected" : "doc-select-btn";
    selectBtn.title = isSelected
      ? "Kapsam filtresinden çıkar"
      : "Aramayı bu belgeyle sınırla";
    selectBtn.setAttribute("aria-label", `${doc.filename} belgesini kapsam olarak seç`);
    selectBtn.textContent = isSelected ? "✓" : "";
    selectBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleDocumentScope(doc.document_id);
    });

    const ext = doc.file_type === "txt" ? "txt" : "pdf";
    const badge = document.createElement("span");
    badge.className = `file-badge is-${ext}`;
    badge.textContent = ext.toUpperCase();

    const meta = document.createElement("div");
    meta.className = "doc-meta";

    const title = document.createElement("p");
    title.className = "doc-title";
    title.textContent = doc.filename;
    title.title = doc.filename;

    const sub = document.createElement("p");
    sub.className = "doc-sub";
    const pageLabel = doc.page_count > 1 ? ` • ${doc.page_count} sayfa` : "";
    sub.textContent = `${doc.chunk_count} parça${pageLabel} • Hazır`;

    meta.append(title, sub);

    const actions = document.createElement("div");
    actions.className = "doc-actions";

    if (state.confirmDeleteId === doc.document_id) {
      const group = document.createElement("div");
      group.className = "doc-confirm-group";

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "doc-confirm-delete";
      confirmBtn.textContent = "Sil";
      confirmBtn.addEventListener("click", () => {
        deleteDocument(doc.document_id, doc.filename, row);
      });

      const cancelBtn = document.createElement("button");
      cancelBtn.type = "button";
      cancelBtn.className = "doc-confirm-cancel";
      cancelBtn.textContent = "İptal";
      cancelBtn.addEventListener("click", () => {
        clearDeleteConfirm();
      });

      group.append(confirmBtn, cancelBtn);
      actions.appendChild(group);
    } else {
      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "doc-delete-btn";
      deleteBtn.title = "Dokümanı sil";
      deleteBtn.setAttribute("aria-label", `${doc.filename} dokümanını sil`);
      deleteBtn.appendChild(createTrashIconSvg());
      deleteBtn.addEventListener("click", () => {
        requestDeleteConfirm(doc.document_id);
      });
      actions.appendChild(deleteBtn);
    }

    main.append(selectBtn, badge, meta, actions);
    row.appendChild(main);
    return row;
  }

  function requestDeleteConfirm(documentId) {
    if (state.confirmTimerId) {
      window.clearTimeout(state.confirmTimerId);
    }
    state.confirmDeleteId = documentId;
    renderDocumentList();
    state.confirmTimerId = window.setTimeout(() => {
      if (state.confirmDeleteId === documentId) {
        state.confirmDeleteId = null;
        renderDocumentList();
      }
    }, 4500);
  }

  function clearDeleteConfirm() {
    if (state.confirmTimerId) {
      window.clearTimeout(state.confirmTimerId);
      state.confirmTimerId = null;
    }
    state.confirmDeleteId = null;
    renderDocumentList();
  }

  async function deleteDocument(documentId, filename, rowElement) {
    clearDeleteConfirm();
    rowElement.classList.add("is-removing");
    try {
      const url = `/api/v1/documents/${encodeURIComponent(documentId)}?tenant_id=${encodeURIComponent(state.tenantId)}`;
      const response = await apiFetch(url, { method: "DELETE" });
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }
      state.documents = state.documents.filter((doc) => doc.document_id !== documentId);
      state.docNameById.delete(documentId);
      renderDocumentList();
      renderSummaryTexts();
      showToast(`"${filename}" silindi`);
    } catch (err) {
      rowElement.classList.remove("is-removing");
      showToast(err instanceof Error ? err.message : "Doküman silinemedi", true);
    }
  }

  async function extractErrorMessage(response) {
    if (response.status === 401) {
      return "Yetkisiz erişim: Lütfen geçerli bir API anahtarı girin.";
    }
    if (response.status === 429) {
      const retryAfter = response.headers.get("Retry-After");
      if (retryAfter) {
        return `Hız sınırı aşıldı. Lütfen ${retryAfter} saniye bekleyip tekrar deneyin.`;
      }
      return "Hız sınırı aşıldı. Lütfen kısa süre bekleyip tekrar deneyin.";
    }
    try {
      const body = await response.json();
      if (body?.error?.message) {
        return String(body.error.message);
      }
    } catch {
      // JSON olmayan hata gövdelerini standart mesajla karşıla
    }
    return `Sunucu hatası (${response.status})`;
  }

  function handleSelectedFiles(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0) {
      return;
    }
    if (!state.apiKey) {
      openApiKeyModal("Doküman yüklemek için önce API anahtarınızı girin.");
      return;
    }

    const rejected = [];
    const accepted = [];
    for (const file of files) {
      const lower = file.name.toLowerCase();
      if (lower.endsWith(".pdf") || lower.endsWith(".txt")) {
        accepted.push(file);
      } else {
        rejected.push(file.name);
      }
    }

    if (rejected.length > 0) {
      showToast(`Desteklenmeyen dosya türü atlandı: ${rejected.join(", ")}`, true);
    }

    for (const file of accepted) {
      enqueueFileUpload(file);
    }
    pumpUploadQueue();
  }

  function enqueueFileUpload(file) {
    const localId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    const ext = file.name.toLowerCase().endsWith(".txt") ? "txt" : "pdf";
    const job = {
      localId,
      tenantId: state.tenantId,
      file,
      filename: file.name,
      fileType: ext,
      progress: 4,
      phaseText: "Sırada bekliyor...",
      state: "queued",
      isIndeterminate: false,
      documentId: null,
      taskId: null,
    };
    state.uploads.set(localId, job);
    state.uploadQueue.push(localId);
    renderDocumentList();
    updateSystemStatus();
  }

  function pumpUploadQueue() {
    while (state.activeUploadCount < MAX_CONCURRENT_UPLOADS && state.uploadQueue.length > 0) {
      const nextId = state.uploadQueue.shift();
      if (!nextId) {
        continue;
      }
      const job = state.uploads.get(nextId);
      if (!job || job.state !== "queued") {
        continue;
      }
      executeFileUpload(job);
    }
  }

  function executeFileUpload(job) {
    const localId = job.localId;
    state.activeUploadCount += 1;
    job.state = "uploading";
    job.progress = 8;
    job.phaseText = "Yükleniyor • %8";
    renderDocumentList();
    updateSystemStatus();

    const formData = new FormData();
    formData.append("file", job.file);
    formData.append("tenant_id", job.tenantId);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/documents/upload", true);
    if (state.apiKey) {
      xhr.setRequestHeader("X-API-Key", state.apiKey);
    }

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        const uploadRatio = event.loaded / event.total;
        const pct = Math.max(8, Math.round(uploadRatio * 65));
        job.progress = pct;
        job.phaseText = `Yükleniyor • %${pct}`;
        renderDocumentList();
      }
    };

    const releaseSlot = () => {
      state.activeUploadCount = Math.max(0, state.activeUploadCount - 1);
      job.file = null;
      pumpUploadQueue();
    };

    xhr.onload = () => {
      let parsed = null;
      try {
        parsed = JSON.parse(xhr.responseText);
      } catch {
        parsed = null;
      }

      if (xhr.status >= 200 && xhr.status < 300 && parsed?.task_id) {
        state.apiKeyVerified = true;
        updateApiKeyButtonState();
        job.documentId = parsed.document_id;
        job.taskId = parsed.task_id;
        job.state = "indexing";
        job.progress = 74;
        job.phaseText = "Kuyruğa alındı...";
        job.isIndeterminate = true;
        renderDocumentList();
        updateSystemStatus();
        releaseSlot();
        pollTaskStatus(localId);
        return;
      }

      job.state = "error";
      job.isIndeterminate = false;
      if (xhr.status === 401) {
        state.apiKeyVerified = false;
        updateApiKeyButtonState();
        job.phaseText = "API anahtarı doğrulanamadı";
        openApiKeyModal("Doküman yüklemek için geçerli bir API anahtarı girin.");
      } else if (xhr.status === 429) {
        const retryAfter = xhr.getResponseHeader("Retry-After");
        job.phaseText = retryAfter
          ? `Hız sınırı aşıldı (${retryAfter} sn bekleyin)`
          : "Hız sınırı aşıldı";
        showToast(job.phaseText, true);
      } else {
        if (xhr.status === 503) {
          void scheduleHealthRefresh();
        }
        job.phaseText = parsed?.error?.message || `Yükleme başarısız (${xhr.status})`;
      }
      renderDocumentList();
      updateSystemStatus();
      releaseSlot();
    };

    xhr.onerror = () => {
      job.state = "error";
      job.isIndeterminate = false;
      job.phaseText = "Ağ bağlantısı hatası";
      renderDocumentList();
      updateSystemStatus();
      releaseSlot();
    };

    xhr.send(formData);
  }

  function computeAdaptivePollDelayMs() {
    const activeIndexing = Array.from(state.uploads.values()).filter(
      (item) => item.state === "indexing"
    ).length;
    return Math.min(3500, POLL_INTERVAL_MS * Math.max(1, activeIndexing));
  }

  async function pollTaskStatus(localId) {
    const job = state.uploads.get(localId);
    if (!job || !job.taskId || job.state !== "indexing") {
      return;
    }

    let nextDelayMs = computeAdaptivePollDelayMs();
    try {
      const response = await apiFetch(
        `/api/v1/documents/status/${encodeURIComponent(job.taskId)}`
      );
      if (response.status === 401) {
        job.state = "error";
        job.isIndeterminate = false;
        job.phaseText = "API anahtarı doğrulanamadı";
        renderDocumentList();
        updateSystemStatus();
        return;
      }
      if (response.status === 400 || response.status === 404 || response.status === 422) {
        job.state = "error";
        job.isIndeterminate = false;
        job.phaseText = "Görev takibi sonlandırıldı";
        renderDocumentList();
        updateSystemStatus();
        return;
      }
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get("Retry-After")) || 5;
        const waitSec = Math.max(2, retryAfter);
        job.phaseText = `Hız sınırı bekleniyor (${waitSec} sn)...`;
        renderDocumentList();
        window.setTimeout(() => pollTaskStatus(localId), waitSec * 1000);
        return;
      }
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const snapshot = await response.json();

      if (snapshot.status === "SUCCESS") {
        const resultStatus = snapshot.result?.status;
        const chunkCount = snapshot.result?.chunk_count;
        if (resultStatus === "missing_file") {
          job.state = "error";
          job.isIndeterminate = false;
          job.phaseText = "Geçici dosya bulunamadı";
          renderDocumentList();
          updateSystemStatus();
          return;
        }
        if (resultStatus === "no_text_extracted" || chunkCount === 0) {
          job.state = "error";
          job.isIndeterminate = false;
          job.phaseText = "Metin çıkarılamadı (taranmış veya boş belge)";
          renderDocumentList();
          updateSystemStatus();
          showToast(`"${job.filename}" içinde çıkarılabilir metin bulunamadı`, true);
          return;
        }

        job.progress = 100;
        job.isIndeterminate = false;
        job.phaseText = "Hazır • %100";
        renderDocumentList();

        if (job.tenantId === state.tenantId) {
          await fetchDocuments();
        }
        window.setTimeout(() => {
          state.uploads.delete(localId);
          renderDocumentList();
          updateSystemStatus();
        }, 420);
        showToast(`"${job.filename}" indekslendi`);
        return;
      }

      if (snapshot.status === "FAILURE") {
        job.state = "error";
        job.isIndeterminate = false;
        job.phaseText = snapshot.error || "Doküman işlenemedi";
        renderDocumentList();
        updateSystemStatus();
        return;
      }

      if (snapshot.status === "STARTED") {
        job.progress = 88;
        job.phaseText = "Parçalanıyor & Vektörleniyor...";
      } else if (snapshot.status === "RETRY") {
        job.progress = 80;
        job.phaseText = "Yeniden deneniyor...";
      } else {
        job.progress = 74;
        job.phaseText = "Sırada bekliyor...";
      }
      renderDocumentList();
    } catch {
      job.phaseText = "Durum bekleniyor...";
      nextDelayMs = Math.min(5000, Math.round(nextDelayMs * 1.5));
      renderDocumentList();
    }

    window.setTimeout(() => pollTaskStatus(localId), nextDelayMs);
  }

  /* ================= GÜVENLİ MARKDOWN & SOHBET ================= */

  function escapeHtml(raw) {
    return raw
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  const INLINE_CITATION_BLOCK_RE =
    /(\s*)\[((?:Source|Kaynak)\s*\d+(?:\s*,\s*(?:(?:Source|Kaynak)\s*)?\d+)*)\](\s*([.,;:!?]))?/gi;
  const INLINE_LATEX_SYMBOL_RE = /\$([A-Za-z][A-Za-z0-9_]{0,12})\$/g;

  function renderInlineCitationsHtml(blockContent) {
    const nums = blockContent.match(/\d+/g) || [];
    const seen = new Set();
    const badges = [];
    for (const rawNum of nums) {
      const num = Number.parseInt(rawNum, 10);
      if (!Number.isFinite(num) || num < 1 || seen.has(num)) {
        continue;
      }
      seen.add(num);
      badges.push(
        `<button type="button" class="inline-citation-badge" data-citation-index="${num - 1}" title="${num}. kaynağa git">${num}</button>`
      );
    }
    if (badges.length === 0) {
      return "";
    }
    return `<span class="inline-citation-group">${badges.join("")}</span>`;
  }

  function formatInlineMarkdown(escapedLine) {
    return escapedLine
      .replace(INLINE_LATEX_SYMBOL_RE, "$1")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>")
      .replace(INLINE_CITATION_BLOCK_RE, (fullMatch, _leadingWs, inner, _tWs, trailingPunct) => {
        const groupHtml = renderInlineCitationsHtml(inner);
        if (!groupHtml) {
          return fullMatch;
        }
        return trailingPunct ? `${trailingPunct}${groupHtml}` : groupHtml;
      });
  }

  function parseMarkdownTableRow(line) {
    const stripped = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    return stripped.split("|").map((cell) => cell.trim());
  }

  function isMarkdownTableSeparator(line) {
    const trimmed = line.trim();
    if (!trimmed.includes("|") || !trimmed.includes("-")) {
      return false;
    }
    const cells = parseMarkdownTableRow(trimmed);
    return cells.length > 0 && cells.every((cell) => /^:?-{2,}:?$/.test(cell));
  }

  function renderSafeMarkdown(text) {
    const lines = escapeHtml(text).replace(/\r\n/g, "\n").split("\n");
    const htmlParts = [];
    let listType = null;
    let inCodeBlock = false;
    let codeLang = "";
    let codeLines = [];

    const closeList = () => {
      if (listType === "ul") {
        htmlParts.push("</ul>");
      } else if (listType === "ol") {
        htmlParts.push("</ol>");
      }
      listType = null;
    };

    const flushCodeBlock = () => {
      const langLabel = codeLang || "metin";
      htmlParts.push(
        `<div class="md-code-block"><div class="md-code-head"><span class="md-code-lang">${langLabel}</span><button type="button" class="md-code-copy">Kopyala</button></div><pre><code>${codeLines.join("\n")}</code></pre></div>`
      );
      inCodeBlock = false;
      codeLang = "";
      codeLines = [];
    };

    for (let i = 0; i < lines.length; i += 1) {
      const rawLine = lines[i];
      const trimmed = rawLine.trim();

      const fenceMatch = trimmed.match(/^```([A-Za-z0-9_+-]*)\s*$/);
      if (fenceMatch) {
        closeList();
        if (inCodeBlock) {
          flushCodeBlock();
        } else {
          inCodeBlock = true;
          codeLang = fenceMatch[1] || "";
          codeLines = [];
        }
        continue;
      }

      if (inCodeBlock) {
        codeLines.push(rawLine);
        continue;
      }

      if (!trimmed) {
        closeList();
        continue;
      }

      if (
        trimmed.includes("|") &&
        i + 1 < lines.length &&
        isMarkdownTableSeparator(lines[i + 1])
      ) {
        closeList();
        const headers = parseMarkdownTableRow(trimmed);
        i += 2;
        const bodyRows = [];
        while (i < lines.length && lines[i].trim().includes("|")) {
          bodyRows.push(parseMarkdownTableRow(lines[i]));
          i += 1;
        }
        i -= 1;

        const thHtml = headers
          .map((cell) => `<th>${formatInlineMarkdown(cell)}</th>`)
          .join("");
        const trHtml = bodyRows
          .map(
            (row) =>
              `<tr>${headers
                .map((_, colIdx) => `<td>${formatInlineMarkdown(row[colIdx] || "")}</td>`)
                .join("")}</tr>`
          )
          .join("");
        htmlParts.push(
          `<div class="md-table-wrap"><table class="md-table"><thead><tr>${thHtml}</tr></thead><tbody>${trHtml}</tbody></table></div>`
        );
        continue;
      }

      const headingMatch = trimmed.match(/^(#{1,3})\s+(.+)$/);
      if (headingMatch) {
        closeList();
        const level = headingMatch[1].length;
        htmlParts.push(`<h${level}>${formatInlineMarkdown(headingMatch[2])}</h${level}>`);
        continue;
      }

      const ulMatch = trimmed.match(/^[-*•]\s+(.+)$/);
      if (ulMatch) {
        if (listType !== "ul") {
          closeList();
          htmlParts.push("<ul>");
          listType = "ul";
        }
        htmlParts.push(`<li>${formatInlineMarkdown(ulMatch[1])}</li>`);
        continue;
      }

      const olMatch = trimmed.match(/^\d+\.\s+(.+)$/);
      if (olMatch) {
        if (listType !== "ol") {
          closeList();
          htmlParts.push("<ol>");
          listType = "ol";
        }
        htmlParts.push(`<li>${formatInlineMarkdown(olMatch[1])}</li>`);
        continue;
      }

      closeList();
      htmlParts.push(`<p>${formatInlineMarkdown(trimmed)}</p>`);
    }

    if (inCodeBlock) {
      flushCodeBlock();
    }
    closeList();
    return htmlParts.join("");
  }

  function wireCodeCopyHandlers(bodyEl) {
    const copyBtns = bodyEl.querySelectorAll(".md-code-copy");
    copyBtns.forEach((btn) => {
      btn.addEventListener("click", async () => {
        const block = btn.closest(".md-code-block");
        const codeEl = block ? block.querySelector("code") : null;
        if (!codeEl) {
          return;
        }
        const ok = await copyTextToClipboard(codeEl.textContent || "");
        if (ok) {
          btn.textContent = "Kopyalandı ✓";
          window.setTimeout(() => {
            btn.textContent = "Kopyala";
          }, 1800);
        }
      });
    });
  }

  async function copyTextToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.select();
      const copied = document.execCommand("copy");
      helper.remove();
      return copied;
    } catch {
      return false;
    }
  }

  function hideEmptyState() {
    if (!state.hasMessages) {
      state.hasMessages = true;
      dom.emptyState.classList.add("hidden");
      dom.clearChatBtn.classList.remove("hidden");
    }
  }

  function getSavedChatKey(tenantId) {
    return `${STORAGE_CHAT_PREFIX}${tenantId}`;
  }

  function saveChatTranscript() {
    try {
      const compactTurns = state.uiTurns.slice(-MAX_SAVED_UI_TURNS).map((item) => ({
        query: item.query,
        answer: item.answer,
        citations: Array.isArray(item.citations) ? item.citations : [],
        stopped: Boolean(item.stopped),
      }));
      if (compactTurns.length === 0) {
        window.localStorage.removeItem(getSavedChatKey(state.tenantId));
      } else {
        window.localStorage.setItem(getSavedChatKey(state.tenantId), JSON.stringify(compactTurns));
      }
    } catch {
      // Depolama kotası doluysa bellek içinde devam et
    }
  }

  function removeSavedChat(tenantId) {
    try {
      window.localStorage.removeItem(getSavedChatKey(tenantId));
    } catch {
      // ignore
    }
  }

  function resetChatView() {
    if (state.isStreaming && state.streamController) {
      state.streamController.abort();
    }
    closeInspector();
    dom.messagesList.replaceChildren();
    state.hasMessages = false;
    state.chatHistory = [];
    state.uiTurns = [];
    state.promptHistory = [];
    state.promptCursor = -1;
    dom.emptyState.classList.remove("hidden");
    dom.clearChatBtn.classList.add("hidden");
  }

  function clearConversation() {
    resetChatView();
    removeSavedChat(state.tenantId);
    dom.queryInput.focus();
  }

  function scrollChatToBottomIfNearBottom(force = false) {
    const el = dom.chatScroll;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (force || distanceFromBottom < 140) {
      el.scrollTop = el.scrollHeight;
    }
  }

  function setStreamingUi(active) {
    state.isStreaming = active;
    dom.sendBtn.classList.toggle("is-streaming", active);
    dom.sendIcon.classList.toggle("hidden", active);
    dom.stopIcon.classList.toggle("hidden", !active);

    if (active) {
      dom.sendBtn.disabled = false;
      dom.sendBtn.title = "Yanıtı durdur";
      dom.sendBtn.setAttribute("aria-label", "Yanıtı durdur");
    } else {
      dom.sendBtn.title = "Gönder (Enter)";
      dom.sendBtn.setAttribute("aria-label", "Soruyu gönder");
      updateSendButtonDisabledState();
    }
    updateSystemStatus();
  }

  function updateSendButtonDisabledState() {
    if (state.isStreaming) {
      dom.sendBtn.disabled = false;
      return;
    }
    dom.sendBtn.disabled = dom.queryInput.value.trim().length === 0;
  }

  function appendUserBubble(queryText) {
    const row = document.createElement("div");
    row.className = "msg-row is-user";
    const bubble = document.createElement("div");
    bubble.className = "user-bubble";
    bubble.textContent = queryText;
    row.appendChild(bubble);
    dom.messagesList.appendChild(row);
    scrollChatToBottomIfNearBottom(true);
  }

  function createAssistantMessageTurn(initialStatusText = "Belgeler taranıyor…") {
    const row = document.createElement("div");
    row.className = "msg-row is-assistant";

    const body = document.createElement("div");
    body.className = "assistant-body";

    const thinkingRow = document.createElement("div");
    thinkingRow.className = "thinking-row";

    const thinking = document.createElement("div");
    thinking.className = "thinking-dots";
    for (let i = 0; i < 3; i += 1) {
      const dot = document.createElement("span");
      dot.className = "thinking-dot";
      thinking.appendChild(dot);
    }

    const thinkingLabel = document.createElement("span");
    thinkingLabel.className = "thinking-label";
    thinkingLabel.textContent = initialStatusText;

    thinkingRow.append(thinking, thinkingLabel);
    body.appendChild(thinkingRow);

    const actionsBar = document.createElement("div");
    actionsBar.className = "turn-actions-bar hidden";

    const citationsWrap = document.createElement("div");
    citationsWrap.className = "citations-wrap hidden";

    row.append(body, actionsBar, citationsWrap);
    dom.messagesList.appendChild(row);
    scrollChatToBottomIfNearBottom(true);

    return { row, body, thinkingLabel, actionsBar, citationsWrap };
  }

  function setTurnThinkingStatus(turn, statusText) {
    if (turn.thinkingLabel && turn.thinkingLabel.isConnected) {
      turn.thinkingLabel.textContent = statusText;
    }
  }

  function updateAssistantText(bodyEl, text, showCaret) {
    if (!text) {
      return;
    }
    bodyEl.innerHTML = renderSafeMarkdown(text);
    wireCodeCopyHandlers(bodyEl);
    if (showCaret) {
      const caret = document.createElement("span");
      caret.className = "stream-caret";
      caret.setAttribute("aria-hidden", "true");
      const lastBlock = bodyEl.lastElementChild;
      if (lastBlock) {
        lastBlock.appendChild(caret);
      } else {
        bodyEl.appendChild(caret);
      }
    }
    scrollChatToBottomIfNearBottom(false);
  }

  function resolveCitationFilename(citation) {
    if (citation.filename && citation.filename.trim()) {
      return citation.filename.trim();
    }
    const mapped = state.docNameById.get(citation.document_id);
    if (mapped) {
      return mapped;
    }
    const shortId = String(citation.document_id || "doc").slice(0, 8);
    const ext = citation.file_type || "pdf";
    return `${shortId}.${ext}`;
  }

  function createSmallDocSvg() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("fill", "none");
    svg.setAttribute("width", "13");
    svg.setAttribute("height", "13");
    svg.classList.add("citation-pill-icon");
    const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    rect.setAttribute("x", "3");
    rect.setAttribute("y", "2");
    rect.setAttribute("width", "10");
    rect.setAttribute("height", "12");
    rect.setAttribute("rx", "1.8");
    rect.setAttribute("stroke", "currentColor");
    rect.setAttribute("stroke-width", "1.3");
    const lines = document.createElementNS("http://www.w3.org/2000/svg", "path");
    lines.setAttribute("d", "M5.5 6H10.5M5.5 8.5H10.5M5.5 11H8.5");
    lines.setAttribute("stroke", "currentColor");
    lines.setAttribute("stroke-width", "1.2");
    lines.setAttribute("stroke-linecap", "round");
    svg.append(rect, lines);
    return svg;
  }

  function groupCitationsByPage(citations) {
    const groups = [];
    const groupByKey = new Map();

    citations.forEach((cit, idx) => {
      const filename = resolveCitationFilename(cit);
      const pageNum = Number(cit.page_number) || 1;
      const scorePct = Math.round((Number(cit.score) || 0) * 100);
      const sourceNum = idx + 1;
      const key = `${cit.document_id || filename}::${pageNum}`;

      let group = groupByKey.get(key);
      if (!group) {
        group = {
          key,
          filename,
          pageNum,
          bestScorePct: scorePct,
          entries: [],
        };
        groupByKey.set(key, group);
        groups.push(group);
      } else if (scorePct > group.bestScorePct) {
        group.bestScorePct = scorePct;
      }

      group.entries.push({
        index: idx,
        sourceNum,
        scorePct,
        text: cit.text || "",
      });
    });

    return groups;
  }

  const KEYWORD_STOPWORDS = new Set([
    "nedir",
    "nasil",
    "nasıl",
    "neler",
    "nelerdir",
    "hangi",
    "hakkinda",
    "hakkında",
    "icin",
    "için",
    "veya",
    "ile",
    "gibi",
    "kadar",
    "olan",
    "olarak",
    "biraz",
    "daha",
    "detayli",
    "detaylı",
    "anlat",
    "ozetle",
    "özetle",
    "what",
    "how",
    "does",
    "with",
    "about",
    "this",
    "that",
    "from",
  ]);

  function extractSearchKeywords(queryText) {
    const tokens = String(queryText || "")
      .toLowerCase()
      .split(/[^a-z0-9çğıöşüÇĞİÖŞÜ-]+/i)
      .map((item) => item.trim())
      .filter((item) => item.length >= 3 && !KEYWORD_STOPWORDS.has(item));
    return Array.from(new Set(tokens)).slice(0, 8);
  }

  function escapeRegExp(raw) {
    return raw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function appendHighlightedText(containerEl, rawText, keywords) {
    containerEl.replaceChildren();
    if (!rawText) {
      return;
    }
    if (!keywords || keywords.length === 0) {
      containerEl.textContent = rawText;
      return;
    }

    const pattern = new RegExp(`(${keywords.map(escapeRegExp).join("|")})`, "gi");
    let lastIdx = 0;
    let match = pattern.exec(rawText);
    while (match !== null) {
      if (match.index > lastIdx) {
        containerEl.appendChild(document.createTextNode(rawText.slice(lastIdx, match.index)));
      }
      const mark = document.createElement("mark");
      mark.className = "quote-highlight";
      mark.textContent = match[0];
      containerEl.appendChild(mark);
      lastIdx = pattern.lastIndex;
      match = pattern.exec(rawText);
    }
    if (lastIdx < rawText.length) {
      containerEl.appendChild(document.createTextNode(rawText.slice(lastIdx)));
    }
  }

  function closeInspector() {
    if (!dom.inspectorDrawer) {
      return;
    }
    dom.inspectorDrawer.classList.remove("is-open");
    dom.inspectorDrawer.classList.add("hidden");
    if (dom.inspectorBackdrop) {
      dom.inspectorBackdrop.classList.add("hidden");
    }
    document.querySelectorAll(".citation-pill.is-active").forEach((el) => {
      el.classList.remove("is-active");
    });
  }

  function renderInspectorView() {
    const { groups, activeGroupIndex, focusCitationIndex, queryText, pillsCol } = state.inspector;
    if (!dom.inspectorDrawer || !groups || groups.length === 0) {
      closeInspector();
      return;
    }

    const safeIdx = Math.max(0, Math.min(activeGroupIndex, groups.length - 1));
    state.inspector.activeGroupIndex = safeIdx;
    const group = groups[safeIdx];

    document.querySelectorAll(".citation-pill.is-active").forEach((el) => {
      el.classList.remove("is-active");
    });
    if (pillsCol) {
      const activePill = pillsCol.children[safeIdx];
      if (activePill) {
        activePill.classList.add("is-active");
      }
    }

    dom.inspectorTitle.textContent = group.filename;
    dom.inspectorSubtitle.textContent = `Sayfa ${group.pageNum} • ${group.entries.length} alıntı • %${group.bestScorePct} eşleşme`;
    dom.inspectorPrevBtn.disabled = safeIdx <= 0;
    dom.inspectorNextBtn.disabled = safeIdx >= groups.length - 1;

    const keywords = extractSearchKeywords(queryText);
    dom.inspectorBody.replaceChildren();

    let focusEl = null;
    group.entries.forEach((entry) => {
      const card = document.createElement("article");
      const isFocused =
        focusCitationIndex !== null && entry.index === focusCitationIndex;
      card.className = isFocused
        ? "inspector-passage-card is-focused"
        : "inspector-passage-card";

      const meta = document.createElement("div");
      meta.className = "inspector-passage-meta";
      const left = document.createElement("span");
      left.className = "inspector-passage-badge";
      left.textContent = `Kaynak [${entry.sourceNum}]`;
      const right = document.createElement("span");
      right.textContent = `Benzerlik %${entry.scorePct}`;
      meta.append(left, right);

      const body = document.createElement("p");
      body.className = "inspector-passage-text";
      appendHighlightedText(body, entry.text, keywords);

      card.append(meta, body);
      dom.inspectorBody.appendChild(card);
      if (isFocused) {
        focusEl = card;
      }
    });

    if (focusEl) {
      focusEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function openInspector(groups, groupIndex, focusCitationIndex, queryText, pillsCol) {
    if (!dom.inspectorDrawer || !groups || groups.length === 0) {
      return;
    }
    state.inspector = {
      groups,
      activeGroupIndex: groupIndex,
      focusCitationIndex,
      queryText: queryText || "",
      pillsCol: pillsCol || null,
    };

    dom.inspectorDrawer.classList.remove("hidden");
    if (dom.inspectorBackdrop) {
      dom.inspectorBackdrop.classList.remove("hidden");
    }
    window.requestAnimationFrame(() => {
      dom.inspectorDrawer.classList.add("is-open");
    });
    renderInspectorView();
  }

  function renderCitations(citationsWrap, citations, queryText = "") {
    citationsWrap.replaceChildren();
    citationsWrap.classList.remove("hidden");

    if (!citations || citations.length === 0) {
      const note = document.createElement("div");
      note.className = "no-citations-note";
      note.textContent = "Yüklü dokümanlarda eşik üzerinde eşleşen kaynak bulunamadı.";
      citationsWrap.appendChild(note);
      return null;
    }

    const groups = groupCitationsByPage(citations);
    const pillsCol = document.createElement("div");
    pillsCol.className = "citations-pills";

    const indexLocationMap = new Map();

    groups.forEach((group, groupIdx) => {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "citation-pill";
      pill.appendChild(createSmallDocSvg());

      const numBadge = document.createElement("span");
      numBadge.className = "citation-source-num";
      const numsLabel = group.entries.map((entry) => entry.sourceNum).join(", ");
      numBadge.textContent = `[${numsLabel}]`;
      pill.appendChild(numBadge);

      const label = document.createElement("span");
      label.className = "citation-pill-text";
      const countSuffix =
        group.entries.length > 1 ? ` • ${group.entries.length} alıntı` : "";
      label.textContent = `${group.filename} • Sayfa ${group.pageNum}${countSuffix} • %${group.bestScorePct}`;
      pill.appendChild(label);

      pill.addEventListener("click", () => {
        openInspector(groups, groupIdx, null, queryText, pillsCol);
      });
      group.entries.forEach((entry) => {
        indexLocationMap.set(entry.index, { groupIdx, entryIndex: entry.index });
      });
      pillsCol.appendChild(pill);
    });

    citationsWrap.appendChild(pillsCol);
    scrollChatToBottomIfNearBottom(false);

    return (citationIndex) => {
      const loc = indexLocationMap.get(citationIndex);
      if (loc) {
        openInspector(groups, loc.groupIdx, loc.entryIndex, queryText, pillsCol);
      }
    };
  }

  function stripCitationTagsForCopy(rawText) {
    return String(rawText || "")
      .replace(/\s*\[(?:Source|Kaynak)\s*\d+(?:\s*,\s*(?:(?:Source|Kaynak)\s*)?\d+)*\]/gi, "")
      .replace(/[ \t]+\n/g, "\n")
      .trim();
  }

  function clearFollowupChips() {
    document.querySelectorAll(".turn-followup-chip").forEach((el) => el.remove());
  }

  function buildFollowupChipPayload(answerText) {
    const cleanText = stripCitationTagsForCopy(answerText);
    const nonEmptyLines = cleanText
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    if (nonEmptyLines.length === 0) {
      return null;
    }
    const lastLine = nonEmptyLines[nonEmptyLines.length - 1];
    if (!lastLine.endsWith("?")) {
      return null;
    }

    const sentences = lastLine.split(/(?<=[.!])\s+/);
    const questionSentence = (sentences[sentences.length - 1] || lastLine).trim();
    if (!questionSentence.endsWith("?")) {
      return null;
    }

    let topic = questionSentence.slice(0, -1).trim();
    topic = topic
      .replace(
        /\s+(?:hakkında|konusunda|konularını|konusunu|üzerine|ile\s+ilgili)\s+(?:daha\s+(?:fazla|detaylı|yakından)\s+)?(?:(?:bilgi|detay(?:ları|lar)?)\s*)?(?:edinmek|paylaşmamı|vermemi|almak|incelememizi|incelemek|açıklamamı|öğrenmek|gözden\s+geçirmek|bakmamızı|keşfetmek)?\s*(?:ister\s+misiniz|istiyorsanız|merak\s+ediyor\s+musunuz)?$/i,
        ""
      )
      .replace(
        /\s+(?:konularını|konusunu)\s+(?:daha\s+(?:fazla|yakından|detaylı)\s+)?(?:incelememizi|incelemek|öğrenmek|keşfetmek)\s*(?:ister\s+misiniz)?$/i,
        ""
      )
      .replace(
        /\s+(?:hakkında|üzerine|ile\s+ilgili)?\s*(?:incelememizi|öğrenmemizi|açıklamamı|paylaşmamı|anlatmamı)\s+ister\s+misiniz$/i,
        ""
      )
      .replace(
        /^(?:would you like(?:\s+me)?\s+to|do you want(?:\s+me)?\s+to|shall\s+i)\s+(?:explore|explain|examine|compare|discuss|elaborate on|learn more about)\s+/i,
        ""
      )
      .replace(/\s+in more detail$/i, "")
      .replace(/^(?:bu\s+belgelerdeki|bu\s+konuda|isterseniz|ayrıca|peki)\s+/i, "")
      .trim();

    if (!topic || topic.length < 4) {
      topic = questionSentence.slice(0, -1).trim();
    }

    const cappedTopic =
      topic.length > 52
        ? `${topic.slice(0, 49).replace(/\s+\S*$/, "").trim()}...`
        : topic;
    const formattedTopic = cappedTopic.charAt(0).toUpperCase() + cappedTopic.slice(1);
    const isEnglish = /[?]/.test(questionSentence) && /would you|do you|shall i/i.test(questionSentence);
    const prompt = isEnglish
      ? `Please explain ${topic} in more detail.`
      : `${topic} hakkında daha detaylı bilgi verir misin?`;

    return {
      label: `${formattedTopic} →`,
      prompt,
    };
  }

  function renderTurnActions(actionsBar, answerText, citations, showFollowup = true) {
    if (!actionsBar) {
      return;
    }
    actionsBar.replaceChildren();
    if (!answerText) {
      actionsBar.classList.add("hidden");
      return;
    }

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "turn-action-copy";
    copyBtn.textContent = "Kopyala";
    copyBtn.title = "Yanıtı panoya kopyala";
    copyBtn.addEventListener("click", async () => {
      const ok = await copyTextToClipboard(stripCitationTagsForCopy(answerText));
      if (ok) {
        copyBtn.textContent = "Kopyalandı ✓";
        window.setTimeout(() => {
          copyBtn.textContent = "Kopyala";
        }, 1800);
      }
    });
    actionsBar.appendChild(copyBtn);

    if (showFollowup && citations && citations.length > 0) {
      const followup = buildFollowupChipPayload(answerText);
      if (followup) {
        clearFollowupChips();
        const acceptBtn = document.createElement("button");
        acceptBtn.type = "button";
        acceptBtn.className = "turn-action-chip is-accent turn-followup-chip";
        acceptBtn.textContent = followup.label;
        acceptBtn.title = followup.prompt;
        acceptBtn.addEventListener("click", () => {
          if (!state.isStreaming) {
            submitQuery(followup.prompt);
          }
        });
        actionsBar.appendChild(acceptBtn);
      }
    }

    actionsBar.classList.remove("hidden");
  }

  function renderStoppedBadge(bodyEl) {
    const badge = document.createElement("div");
    badge.className = "turn-stopped-note";
    badge.textContent = "Yanıt kullanıcı tarafından durduruldu";
    bodyEl.appendChild(badge);
  }

  function restoreChatTranscript() {
    let saved = [];
    try {
      const raw = window.localStorage.getItem(getSavedChatKey(state.tenantId));
      saved = raw ? JSON.parse(raw) : [];
    } catch {
      saved = [];
    }
    if (!Array.isArray(saved) || saved.length === 0) {
      return;
    }

    hideEmptyState();
    const restoredHistory = [];
    const restoredUiTurns = [];
    const restoredPrompts = [];
    const validItems = saved.filter(
      (item) => item && typeof item.query === "string" && typeof item.answer === "string"
    );

    validItems.forEach((item, idx) => {
      const isLastTurn = idx === validItems.length - 1;
      appendUserBubble(item.query);
      const turn = createAssistantMessageTurn("");
      updateAssistantText(turn.body, item.answer, false);
      if (item.stopped) {
        renderStoppedBadge(turn.body);
      }
      const citList = Array.isArray(item.citations) ? item.citations : [];
      renderTurnActions(turn.actionsBar, item.answer, citList, isLastTurn);
      const openCitationByIndex = renderCitations(turn.citationsWrap, citList, item.query);
      wireInlineCitationBadges(turn.body, citList, openCitationByIndex);

      restoredUiTurns.push({
        query: item.query,
        answer: item.answer,
        citations: citList,
        stopped: Boolean(item.stopped),
      });
      restoredPrompts.push(item.query);
      if (!item.stopped && item.answer) {
        restoredHistory.push({ role: "user", content: item.query });
        restoredHistory.push({ role: "assistant", content: item.answer });
      }
    });

    state.uiTurns = restoredUiTurns.slice(-MAX_SAVED_UI_TURNS);
    state.chatHistory = restoredHistory.slice(-MAX_HISTORY_MESSAGES);
    state.promptHistory = restoredPrompts.slice(-30);
    scrollChatToBottomIfNearBottom(true);
  }

  function wireInlineCitationBadges(bodyEl, citations, openCitationByIndex) {
    const badges = bodyEl.querySelectorAll(".inline-citation-badge");
    badges.forEach((badge) => {
      const idx = Number.parseInt(badge.getAttribute("data-citation-index") || "", 10);
      if (!openCitationByIndex || !Number.isFinite(idx) || idx < 0 || idx >= citations.length) {
        badge.disabled = true;
        return;
      }
      badge.addEventListener("click", () => openCitationByIndex(idx));
    });
  }

  async function submitQuery(rawQuery) {
    const query = rawQuery.trim();
    if (!query || state.isStreaming) {
      return;
    }
    if (!state.apiKey) {
      openApiKeyModal("Soru sormak için önce API anahtarınızı girin.");
      return;
    }

    hideEmptyState();
    clearFollowupChips();
    appendUserBubble(query);
    state.promptHistory = [...state.promptHistory, query].slice(-30);
    state.promptCursor = -1;

    dom.queryInput.value = "";
    autoResizeTextarea();

    const scopedIds = Array.from(state.selectedDocumentIds);
    const initialSearchMsg =
      scopedIds.length > 0
        ? `Seçili ${scopedIds.length} belgede aranıyor…`
        : "Çalışma alanındaki belgeler taranıyor…";
    const turn = createAssistantMessageTurn(initialSearchMsg);
    const controller = new AbortController();
    state.streamController = controller;
    setStreamingUi(true);

    let answerText = "";
    let citations = [];
    let streamErrorMessage = "";
    const historySnapshot = state.chatHistory.slice(-MAX_HISTORY_MESSAGES);

    try {
      const response = await apiFetch("/api/v1/query/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          tenant_id: state.tenantId,
          history: historySnapshot,
          document_ids: scopedIds,
          stream: true,
        }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(await extractErrorMessage(response));
      }

      const preCountRaw = response.headers.get("X-Retrieved-Citations");
      const preCount = preCountRaw !== null ? Number.parseInt(preCountRaw, 10) : NaN;
      if (Number.isFinite(preCount) && preCount > 0) {
        setTurnThinkingStatus(turn, `${preCount} ilgili pasaj bulundu, yanıt hazırlanıyor…`);
      } else {
        setTurnThinkingStatus(turn, "Yanıt hazırlanıyor…");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      const processSseFrame = (frameText) => {
        if (!frameText.trim()) {
          return;
        }
        const parsed = parseSseFrame(frameText);
        if (!parsed) {
          return;
        }
        if (parsed.event === "token") {
          const payload = JSON.parse(parsed.data);
          if (payload.delta) {
            answerText += payload.delta;
            updateAssistantText(turn.body, answerText, true);
          }
        } else if (parsed.event === "citations") {
          const payload = JSON.parse(parsed.data);
          citations = Array.isArray(payload.citations) ? payload.citations : [];
        } else if (parsed.event === "error") {
          const payload = JSON.parse(parsed.data);
          streamErrorMessage = payload.message || "Yanıt akışı sırasında hata oluştu.";
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          processSseFrame(frame);
        }
      }

      if (buffer.trim()) {
        processSseFrame(buffer);
      }

      if (streamErrorMessage) {
        if (answerText) {
          updateAssistantText(turn.body, answerText, false);
        } else {
          turn.body.replaceChildren();
        }
        const errBox = document.createElement("div");
        errBox.className = "assistant-error-box";
        errBox.textContent = streamErrorMessage;
        turn.body.appendChild(errBox);
      } else {
        const finalAnswer =
          answerText || "Yüklü dokümanlarda bu soruya ilişkin bir yanıt üretilemedi.";
        updateAssistantText(turn.body, finalAnswer, false);
        renderTurnActions(turn.actionsBar, finalAnswer, citations, true);
        const openCitationByIndex = renderCitations(turn.citationsWrap, citations, query);
        wireInlineCitationBadges(turn.body, citations, openCitationByIndex);
        if (answerText) {
          state.chatHistory = [
            ...state.chatHistory,
            { role: "user", content: query },
            { role: "assistant", content: answerText },
          ].slice(-MAX_HISTORY_MESSAGES);
        }
        state.uiTurns = [
          ...state.uiTurns,
          { query, answer: finalAnswer, citations, stopped: false },
        ].slice(-MAX_SAVED_UI_TURNS);
        saveChatTranscript();
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        const partial = answerText || "";
        if (partial) {
          updateAssistantText(turn.body, partial, false);
        } else {
          turn.body.replaceChildren();
        }
        renderStoppedBadge(turn.body);
        if (partial) {
          renderTurnActions(turn.actionsBar, partial, [], false);
          state.uiTurns = [
            ...state.uiTurns,
            { query, answer: partial, citations: [], stopped: true },
          ].slice(-MAX_SAVED_UI_TURNS);
          saveChatTranscript();
        }
      } else {
        turn.body.replaceChildren();
        const errBox = document.createElement("div");
        errBox.className = "assistant-error-box";
        errBox.textContent = err instanceof Error ? err.message : "Sorgu sırasında hata oluştu.";
        turn.body.appendChild(errBox);
      }
    } finally {
      state.streamController = null;
      setStreamingUi(false);
      scrollChatToBottomIfNearBottom(false);
    }
  }

  function parseSseFrame(frameText) {
    let eventName = "message";
    const dataLines = [];
    for (const line of frameText.split(/\r?\n/)) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length === 0) {
      return null;
    }
    return { event: eventName, data: dataLines.join("\n") };
  }

  /* ================= OLAY BAĞLANTILARI ================= */

  function autoResizeTextarea() {
    const el = dom.queryInput;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 150)}px`;
    updateSendButtonDisabledState();
  }

  function openMobileSidebar() {
    dom.sidebar.classList.add("is-open");
    dom.sidebarBackdrop.classList.remove("hidden");
  }

  function closeMobileSidebar() {
    dom.sidebar.classList.remove("is-open");
    dom.sidebarBackdrop.classList.add("hidden");
  }

  function bindEvents() {
    dom.selectFilesBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      dom.fileInput.click();
    });

    dom.dropzone.addEventListener("click", () => {
      dom.fileInput.click();
    });

    dom.dropzone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        dom.fileInput.click();
      }
    });

    dom.fileInput.addEventListener("change", () => {
      handleSelectedFiles(dom.fileInput.files);
      dom.fileInput.value = "";
    });

    dom.dropzone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dom.dropzone.classList.add("is-dragover");
    });

    dom.dropzone.addEventListener("dragleave", () => {
      dom.dropzone.classList.remove("is-dragover");
    });

    dom.dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      dom.dropzone.classList.remove("is-dragover");
      dom.windowDropOverlay.classList.add("hidden");
      state.dragCounter = 0;
      handleSelectedFiles(event.dataTransfer?.files);
    });

    window.addEventListener("dragenter", (event) => {
      if (event.dataTransfer?.types?.includes("Files")) {
        state.dragCounter += 1;
        dom.windowDropOverlay.classList.remove("hidden");
      }
    });

    window.addEventListener("dragleave", (event) => {
      if (event.dataTransfer?.types?.includes("Files")) {
        state.dragCounter = Math.max(0, state.dragCounter - 1);
        if (state.dragCounter === 0) {
          dom.windowDropOverlay.classList.add("hidden");
        }
      }
    });

    window.addEventListener("dragover", (event) => {
      if (event.dataTransfer?.types?.includes("Files")) {
        event.preventDefault();
      }
    });

    window.addEventListener("drop", (event) => {
      if (event.dataTransfer?.types?.includes("Files")) {
        event.preventDefault();
        state.dragCounter = 0;
        dom.windowDropOverlay.classList.add("hidden");
        handleSelectedFiles(event.dataTransfer.files);
      }
    });

    dom.workspaceToggleBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleWorkspaceMenu();
    });

    dom.workspaceForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const nextTenant = dom.workspaceInput.value.trim();
      if (!TENANT_PATTERN.test(nextTenant)) {
        showToast("Çalışma alanı yalnızca harf, rakam, '_' veya '-' içerebilir", true);
        return;
      }
      dom.workspaceInput.value = "";
      saveTenant(nextTenant);
      switchWorkspace(nextTenant);
    });

    document.addEventListener("click", (event) => {
      if (
        !dom.workspaceMenu.classList.contains("hidden") &&
        dom.sidebarFooter &&
        !dom.sidebarFooter.contains(event.target)
      ) {
        closeWorkspaceMenu();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (!dom.workspaceMenu.classList.contains("hidden")) {
          closeWorkspaceMenu();
        }
        if (dom.apiKeyModal && !dom.apiKeyModal.classList.contains("hidden")) {
          closeApiKeyModal();
        }
      }
    });

    if (dom.apiKeyBtn) {
      dom.apiKeyBtn.addEventListener("click", () => openApiKeyModal());
    }
    if (dom.apiKeyCancelBtn) {
      dom.apiKeyCancelBtn.addEventListener("click", closeApiKeyModal);
    }
    if (dom.apiKeyModal) {
      dom.apiKeyModal.addEventListener("click", (event) => {
        if (event.target === dom.apiKeyModal) {
          closeApiKeyModal();
        }
      });
    }
    if (dom.apiKeyVisibilityBtn) {
      dom.apiKeyVisibilityBtn.addEventListener("click", () => {
        const isPassword = dom.apiKeyInput.type === "password";
        dom.apiKeyInput.type = isPassword ? "text" : "password";
        dom.apiKeyVisibilityBtn.textContent = isPassword ? "Gizle" : "Göster";
      });
    }
    if (dom.apiKeyClearBtn) {
      dom.apiKeyClearBtn.addEventListener("click", () => {
        persistApiKey("");
        state.apiKeyVerified = false;
        state.documents = [];
        state.docNameById = new Map();
        renderDocumentList();
        dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı bekleniyor`;
        updateApiKeyButtonState();
        closeApiKeyModal();
        showToast("API anahtarı tarayıcıdan temizlendi");
      });
    }
    if (dom.apiKeyForm) {
      dom.apiKeyForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const candidate = dom.apiKeyInput.value.trim();
        if (candidate.length < 8) {
          setApiKeyModalError("API anahtarı en az 8 karakter olmalıdır.");
          return;
        }
        setApiKeyModalError("");
        dom.apiKeySaveBtn.disabled = true;
        try {
          const probe = await fetch("/api/v1/documents/tenants", {
            headers: { "X-API-Key": candidate },
          });
          if (probe.status === 401) {
            setApiKeyModalError("Geçersiz API anahtarı. Lütfen .env dosyanızdaki değeri kontrol edin.");
            return;
          }
          if (!probe.ok) {
            setApiKeyModalError(await extractErrorMessage(probe));
            return;
          }
          persistApiKey(candidate);
          state.apiKeyVerified = true;
          updateApiKeyButtonState();
          closeApiKeyModal();
          showToast("API anahtarı doğrulandı");
          fetchKnownTenants();
          fetchDocuments();
        } catch {
          setApiKeyModalError("Sunucuya ulaşılamadı. Bağlantınızı kontrol edin.");
        } finally {
          dom.apiKeySaveBtn.disabled = false;
        }
      });
    }

    dom.starterPrompts.addEventListener("click", (event) => {
      const btn = event.target.closest(".starter-chip");
      if (!btn) {
        return;
      }
      const prompt = btn.getAttribute("data-prompt");
      if (prompt) {
        submitQuery(prompt);
      }
    });

    dom.clearChatBtn.addEventListener("click", clearConversation);
    if (dom.clearScopeBtn) {
      dom.clearScopeBtn.addEventListener("click", clearDocumentScope);
    }

    if (dom.inspectorCloseBtn) {
      dom.inspectorCloseBtn.addEventListener("click", closeInspector);
    }
    if (dom.inspectorBackdrop) {
      dom.inspectorBackdrop.addEventListener("click", closeInspector);
    }
    if (dom.inspectorPrevBtn) {
      dom.inspectorPrevBtn.addEventListener("click", () => {
        if (state.inspector.activeGroupIndex > 0) {
          state.inspector.activeGroupIndex -= 1;
          state.inspector.focusCitationIndex = null;
          renderInspectorView();
        }
      });
    }
    if (dom.inspectorNextBtn) {
      dom.inspectorNextBtn.addEventListener("click", () => {
        if (state.inspector.activeGroupIndex < state.inspector.groups.length - 1) {
          state.inspector.activeGroupIndex += 1;
          state.inspector.focusCitationIndex = null;
          renderInspectorView();
        }
      });
    }

    dom.queryInput.addEventListener("input", autoResizeTextarea);

    dom.queryInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (!state.isStreaming) {
          submitQuery(dom.queryInput.value);
        }
        return;
      }
      if (
        event.key === "ArrowUp" &&
        !dom.queryInput.value.trim() &&
        state.promptHistory.length > 0
      ) {
        event.preventDefault();
        if (state.promptCursor < 0) {
          state.promptCursor = state.promptHistory.length - 1;
        } else if (state.promptCursor > 0) {
          state.promptCursor -= 1;
        }
        dom.queryInput.value = state.promptHistory[state.promptCursor] || "";
        autoResizeTextarea();
        window.requestAnimationFrame(() => {
          const len = dom.queryInput.value.length;
          dom.queryInput.setSelectionRange(len, len);
        });
      }
    });

    document.addEventListener("keydown", (event) => {
      const activeTag = document.activeElement?.tagName?.toLowerCase();
      const isTyping = activeTag === "input" || activeTag === "textarea";
      if (event.key === "/" && !isTyping) {
        event.preventDefault();
        dom.queryInput.focus();
        return;
      }
      if (event.key === "Escape") {
        if (dom.inspectorDrawer && !dom.inspectorDrawer.classList.contains("hidden")) {
          closeInspector();
        } else if (dom.workspaceMenu && !dom.workspaceMenu.classList.contains("hidden")) {
          closeWorkspaceMenu();
        } else if (dom.apiKeyModal && !dom.apiKeyModal.classList.contains("hidden") && state.apiKey) {
          closeApiKeyModal();
        }
      }
    });

    dom.queryForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (state.isStreaming && state.streamController) {
        state.streamController.abort();
        return;
      }
      submitQuery(dom.queryInput.value);
    });

    dom.mobileOpenBtn.addEventListener("click", openMobileSidebar);
    dom.mobileCloseBtn.addEventListener("click", closeMobileSidebar);
    dom.sidebarBackdrop.addEventListener("click", closeMobileSidebar);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        void scheduleHealthRefresh();
      }
    });
  }

  function init() {
    bindEvents();
    updateApiKeyButtonState();
    renderWorkspaceList();
    renderSummaryTexts();
    renderScopeChips();
    restoreChatTranscript();
    checkHealth();
    startHealthPolling();
    if (state.apiKey) {
      fetchKnownTenants();
      fetchDocuments();
    } else {
      dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı bekleniyor`;
      openApiKeyModal();
    }
    updateSystemStatus();
  }

  init();
})();
