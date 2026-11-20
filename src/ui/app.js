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
  const DEFAULT_MAX_UPLOAD_PER_WINDOW = 5;
  const DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60;
  const MAX_HISTORY_MESSAGES = 10;
  const MAX_HISTORY_CONTENT_CHARS = 12000;
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
    maxUploadPerWindow: DEFAULT_MAX_UPLOAD_PER_WINDOW,
    uploadRateWindowSeconds: DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    uploadWindowTimestamps: [],
    batchByTenant: new Map(),
    countdownTimerId: null,
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
    dropzoneSubtitle: document.getElementById("dropzone-subtitle"),
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
    indexingStageCard: document.getElementById("indexing-stage-card"),
    indexingStageTitle: document.getElementById("indexing-stage-title"),
    indexingStageBadge: document.getElementById("indexing-stage-badge"),
    indexingStageDesc: document.getElementById("indexing-stage-desc"),
    indexingMasterFill: document.getElementById("indexing-master-fill"),
    indexingStepUpload: document.getElementById("indexing-step-upload"),
    indexingStepChunk: document.getElementById("indexing-step-chunk"),
    indexingStepVector: document.getElementById("indexing-step-vector"),
    indexingRateBanner: document.getElementById("indexing-rate-banner"),
    indexingFileList: document.getElementById("indexing-file-list"),
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
    clearDeleteConfirm();
    state.tenantId = nextTenant;
    state.documents = [];
    state.docNameById = new Map();
    state.selectedDocumentIds.clear();
    saveTenant(nextTenant);
    renderWorkspaceList();
    renderDocumentList();
    renderSummaryTexts();
    renderScopeChips();
    renderStarterPrompts();
    resetChatView();
    restoreChatTranscript();
    renderIndexingStageCard();
    updateComposerLockState();
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

  function getUploadQuotaInfo(nowMs = Date.now()) {
    const limit = state.maxUploadPerWindow;
    const windowSeconds = state.uploadRateWindowSeconds;
    const recentTimestamps = pruneUploadWindowTimestamps(nowMs);
    const activeGlobalCount = Array.from(state.uploads.values()).filter(isUploadJobActive).length;
    const usedSlots = Math.max(activeGlobalCount, recentTimestamps.length);
    const remainingSlots = limit > 0 ? Math.max(0, limit - usedSlots) : Number.POSITIVE_INFINITY;
    const windowMs = Math.max(1, windowSeconds) * 1000;
    const oldestTs = recentTimestamps.length > 0 ? recentTimestamps[0] : null;
    const waitSeconds = oldestTs
      ? Math.max(1, Math.ceil((windowMs - (nowMs - oldestTs)) / 1000))
      : windowSeconds;
    return {
      limit,
      windowSeconds,
      recentTimestamps,
      remainingSlots,
      waitSeconds,
    };
  }

  function renderDropzoneSubtitle() {
    if (!dom.dropzoneSubtitle) {
      return;
    }
    if (state.maxUploadPerWindow <= 0) {
      dom.dropzoneSubtitle.textContent = ".pdf veya .txt (çoklu destek)";
      return;
    }
    const quota = getUploadQuotaInfo();
    if (quota.remainingSlots === 0 && quota.recentTimestamps.length > 0) {
      dom.dropzoneSubtitle.textContent = `Yükleme kotası doldu (0/${quota.limit}) • ${quota.waitSeconds} sn sonra yenilenir`;
    } else if (quota.remainingSlots < quota.limit) {
      dom.dropzoneSubtitle.textContent = `.pdf veya .txt (kalan yükleme hakkı: ${quota.remainingSlots}/${quota.limit})`;
    } else {
      dom.dropzoneSubtitle.textContent = `.pdf veya .txt (tek seferde en fazla ${quota.limit} dosya)`;
    }
  }

  async function checkHealth() {
    state.lastHealthCheckAt = Date.now();
    try {
      const response = await fetch("/api/v1/health");
      const rawUploadLimit = response.headers.get("X-RateLimit-Upload-Limit");
      const rawWindowSec = response.headers.get("X-RateLimit-Window-Seconds");
      if (rawUploadLimit !== null) {
        const parsedLimit = Number.parseInt(rawUploadLimit, 10);
        if (Number.isFinite(parsedLimit) && parsedLimit >= 0) {
          state.maxUploadPerWindow = parsedLimit;
        }
      }
      if (rawWindowSec !== null) {
        const parsedWindow = Number.parseInt(rawWindowSec, 10);
        if (Number.isFinite(parsedWindow) && parsedWindow >= 1) {
          state.uploadRateWindowSeconds = parsedWindow;
        }
      }
      renderDropzoneSubtitle();

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
      renderSummaryTexts();
      renderScopeChips();
      renderStarterPrompts();
      dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı bekleniyor`;
      return;
    }
    const requestedTenant = state.tenantId;
    const url = `/api/v1/documents?tenant_id=${encodeURIComponent(requestedTenant)}`;
    try {
      const response = await apiFetch(url);
      if (state.tenantId !== requestedTenant) {
        return;
      }
      if (response.status === 401) {
        state.documents = [];
        state.docNameById = new Map();
        state.selectedDocumentIds.clear();
        renderDocumentList();
        renderSummaryTexts();
        renderScopeChips();
        renderStarterPrompts();
        dom.topbarDocSummary.textContent = `${state.tenantId} • API anahtarı doğrulanamadı`;
        return;
      }
      if (!response.ok) {
        const errorMessage = await extractErrorMessage(response);
        if (state.tenantId !== requestedTenant) {
          return;
        }
        throw new Error(errorMessage);
      }
      const data = await response.json();
      if (state.tenantId !== requestedTenant) {
        return;
      }
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
      if (state.tenantId !== requestedTenant) {
        return;
      }
      state.documents = [];
      state.docNameById = new Map();
      state.selectedDocumentIds.clear();
      renderDocumentList();
      renderSummaryTexts();
      renderScopeChips();
      renderStarterPrompts();
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
    dom.starterPrompts.replaceChildren();
    const docs = state.documents;
    if (docs.length === 0) {
      dom.starterPrompts.classList.add("hidden");
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
      dom.starterPrompts.classList.add("hidden");
      return;
    }
    for (const entry of items.slice(0, 3)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "starter-chip";
      btn.setAttribute("data-prompt", entry.prompt);
      btn.textContent = entry.label;
      dom.starterPrompts.appendChild(btn);
    }
    dom.starterPrompts.classList.remove("hidden");
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
    sub.textContent = formatJobPhaseText(job);

    meta.append(title, sub);
    main.append(badge, meta);

    if (job.state === "error") {
      const dismissBtn = document.createElement("button");
      dismissBtn.type = "button";
      dismissBtn.className = "doc-confirm-cancel";
      dismissBtn.textContent = "Kapat";
      dismissBtn.addEventListener("click", () => {
        state.uploads.delete(job.localId);
        syncUploadUi();
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
      state.selectedDocumentIds.delete(documentId);
      renderDocumentList();
      renderSummaryTexts();
      renderScopeChips();
      renderStarterPrompts();
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

  function isUploadJobActive(job) {
    return job.state === "queued" || job.state === "uploading" || job.state === "indexing";
  }

  function getActiveWorkspaceUploadJobs(tenantId = state.tenantId) {
    return Array.from(state.uploads.values()).filter(
      (job) => job.tenantId === tenantId && isUploadJobActive(job)
    );
  }

  function hasActiveWorkspaceUploads(tenantId = state.tenantId) {
    return getActiveWorkspaceUploadJobs(tenantId).length > 0;
  }

  function ensureActiveBatch(tenantId) {
    let batch = state.batchByTenant.get(tenantId);
    if (!batch || !hasActiveWorkspaceUploads(tenantId)) {
      batch = {
        completedIds: new Set(),
        jobMeta: new Map(),
        maxPct: 6,
        lastTotal: 0,
      };
      state.batchByTenant.set(tenantId, batch);
    }
    return batch;
  }

  function markBatchJobCompleted(tenantId, localId, finalState, phaseText) {
    const batch = state.batchByTenant.get(tenantId);
    if (!batch) {
      return;
    }
    batch.completedIds.add(localId);
    const meta = batch.jobMeta.get(localId);
    if (meta) {
      meta.finalState = finalState;
      meta.phaseText = phaseText;
    }
  }

  function setStepVisualState(stepEl, stepState) {
    if (!stepEl) {
      return;
    }
    stepEl.classList.remove("is-active", "is-done");
    if (stepState === "is-active" || stepState === "is-done") {
      stepEl.classList.add(stepState);
    }
  }

  function formatJobPhaseText(job, nowMs = Date.now()) {
    if (job.isRateLimited) {
      const remainingSec = job.rateLimitResumeAt
        ? Math.max(1, Math.ceil((job.rateLimitResumeAt - nowMs) / 1000))
        : 1;
      const provider = job.rateLimitProvider || "AI API";
      const chunkInfo = job.chunksTotal > 0 ? `${job.chunksDone} / ${job.chunksTotal} parça • ` : "";
      return `⏳ ${provider} kotası: ${chunkInfo}${remainingSec} sn`;
    }
    return job.phaseText;
  }

  function ensureCountdownTicker() {
    const hasActiveOrRateLimited = Array.from(state.uploads.values()).some(isUploadJobActive);
    const recentQuota =
      state.maxUploadPerWindow > 0 && pruneUploadWindowTimestamps().length > 0;
    if (!hasActiveOrRateLimited && !recentQuota) {
      if (state.countdownTimerId) {
        window.clearInterval(state.countdownTimerId);
        state.countdownTimerId = null;
      }
      renderDropzoneSubtitle();
      return;
    }
    if (state.countdownTimerId) {
      return;
    }
    state.countdownTimerId = window.setInterval(() => {
      const stillActive = Array.from(state.uploads.values()).some(isUploadJobActive);
      const stillRecentQuota =
        state.maxUploadPerWindow > 0 && pruneUploadWindowTimestamps().length > 0;
      renderDropzoneSubtitle();
      if (stillActive) {
        renderDocumentList();
        renderIndexingStageCard();
      }
      if (!stillActive && !stillRecentQuota) {
        window.clearInterval(state.countdownTimerId);
        state.countdownTimerId = null;
      }
    }, 1000);
  }

  function renderIndexingStageCard() {
    if (!dom.indexingStageCard) {
      return;
    }
    const nowMs = Date.now();
    const activeJobs = getActiveWorkspaceUploadJobs();
    if (activeJobs.length === 0) {
      dom.indexingStageCard.classList.add("hidden");
      dom.indexingStageCard.classList.remove("is-hero");
      if (dom.indexingRateBanner) {
        dom.indexingRateBanner.classList.add("hidden");
        dom.indexingRateBanner.replaceChildren();
      }
      state.batchByTenant.delete(state.tenantId);
      if (!state.hasMessages && dom.emptyState) {
        dom.emptyState.classList.remove("hidden");
      }
      return;
    }

    if (dom.emptyState) {
      dom.emptyState.classList.add("hidden");
    }
    dom.indexingStageCard.classList.remove("hidden");
    dom.indexingStageCard.classList.toggle("is-hero", !state.hasMessages);

    const batch = state.batchByTenant.get(state.tenantId);
    const metaList = batch ? Array.from(batch.jobMeta.values()) : [];
    const totalCount = Math.max(activeJobs.length, metaList.length);
    const doneCount = batch
      ? Array.from(batch.completedIds).filter((id) => batch.jobMeta.has(id)).length
      : 0;

    let progressSum = 0;
    if (metaList.length > 0) {
      for (const meta of metaList) {
        const liveJob = state.uploads.get(meta.localId);
        if (batch.completedIds.has(meta.localId) || meta.finalState) {
          progressSum += 100;
        } else if (liveJob) {
          progressSum += Math.max(4, Math.min(99, liveJob.progress || 4));
        } else {
          progressSum += 4;
        }
      }
    } else {
      for (const job of activeJobs) {
        progressSum += Math.max(4, Math.min(99, job.progress || 4));
      }
    }

    const rawPct = totalCount > 0 ? Math.round(progressSum / totalCount) : 6;
    let overallPct = rawPct;
    if (batch) {
      if (batch.lastTotal === totalCount) {
        overallPct = Math.max(batch.maxPct || 6, rawPct);
      }
      batch.maxPct = overallPct;
      batch.lastTotal = totalCount;
    }
    overallPct = Math.max(6, Math.min(100, overallPct));

    if (dom.indexingStageTitle) {
      dom.indexingStageTitle.textContent =
        totalCount === 1 ? "Dokümanınız İndeksleniyor" : "Dokümanlarınız İndeksleniyor";
    }
    if (dom.indexingStageBadge) {
      dom.indexingStageBadge.textContent = `${doneCount} / ${totalCount} tamamlandı • %${overallPct}`;
    }
    if (dom.indexingMasterFill) {
      dom.indexingMasterFill.style.width = `${overallPct}%`;
    }

    const hasUploadingOrQueued = activeJobs.some(
      (job) => job.state === "queued" || job.state === "uploading"
    );
    const hasRunningWorker = activeJobs.some(
      (job) => job.state === "indexing" && ((job.progress || 0) >= 88 || job.chunksTotal > 0)
    );
    const hasQueuedWorker = activeJobs.some(
      (job) => job.state === "indexing" && (job.progress || 0) < 88 && !(job.chunksTotal > 0)
    );

    setStepVisualState(dom.indexingStepUpload, hasUploadingOrQueued ? "is-active" : "is-done");
    setStepVisualState(
      dom.indexingStepChunk,
      hasRunningWorker ? "is-done" : hasQueuedWorker ? "is-active" : ""
    );
    setStepVisualState(dom.indexingStepVector, hasRunningWorker ? "is-active" : "");

    if (dom.indexingRateBanner) {
      const rateLimitedJobs = activeJobs.filter((job) => job.isRateLimited);
      if (rateLimitedJobs.length > 0) {
        let primary = rateLimitedJobs[0];
        for (const candidate of rateLimitedJobs) {
          if ((candidate.rateLimitResumeAt || 0) > (primary.rateLimitResumeAt || 0)) {
            primary = candidate;
          }
        }
        const waitSec = primary.rateLimitResumeAt
          ? Math.max(1, Math.ceil((primary.rateLimitResumeAt - nowMs) / 1000))
          : 1;
        const provider = primary.rateLimitProvider || "AI API";
        let bannerText = `${provider} hız sınırı bekleniyor — devam etmesine ${waitSec} sn kaldı`;
        if (rateLimitedJobs.length === 1 && primary.chunksTotal > 0) {
          bannerText = `${provider} hız sınırı bekleniyor — yeni kota penceresine ${waitSec} sn kaldı (${primary.chunksDone} / ${primary.chunksTotal} parça hazır)`;
        } else if (rateLimitedJobs.length > 1) {
          bannerText = `${provider} hız sınırı bekleniyor (${rateLimitedJobs.length} dosya) — devam etmesine ${waitSec} sn kaldı`;
        }
        const dotEl = document.createElement("span");
        dotEl.className = "indexing-rate-dot";
        const msgEl = document.createElement("span");
        msgEl.textContent = bannerText;
        dom.indexingRateBanner.replaceChildren(dotEl, msgEl);
        dom.indexingRateBanner.classList.remove("hidden");
      } else {
        dom.indexingRateBanner.classList.add("hidden");
        dom.indexingRateBanner.replaceChildren();
      }
    }

    if (dom.indexingFileList) {
      dom.indexingFileList.replaceChildren();
      const rows =
        metaList.length > 0
          ? metaList
          : activeJobs.map((job) => ({
              localId: job.localId,
              filename: job.filename,
              finalState: null,
              phaseText: job.phaseText,
            }));

      for (const item of rows) {
        const liveJob = state.uploads.get(item.localId);
        const row = document.createElement("div");

        let rowClass = "indexing-file-row";
        let phaseLabel = item.phaseText || "Hazırlanıyor...";
        let pctLabel = "%4";

        if (item.finalState === "error" || liveJob?.state === "error") {
          rowClass = "indexing-file-row is-error";
          phaseLabel = item.phaseText || liveJob?.phaseText || "Hata";
          pctLabel = "Hata";
        } else if (
          item.finalState === "done" ||
          liveJob?.state === "done" ||
          batch?.completedIds.has(item.localId)
        ) {
          rowClass = "indexing-file-row is-done";
          phaseLabel = "İndekslendi ✓";
          pctLabel = "%100";
        } else if (liveJob) {
          if (liveJob.isRateLimited) {
            rowClass = "indexing-file-row is-rate-limited";
          }
          phaseLabel = formatJobPhaseText(liveJob, nowMs);
          pctLabel = `%${Math.round(liveJob.progress || 4)}`;
        }

        row.className = rowClass;

        const nameEl = document.createElement("span");
        nameEl.className = "indexing-file-name";
        nameEl.textContent = item.filename;
        nameEl.title = item.filename;

        const metaEl = document.createElement("div");
        metaEl.className = "indexing-file-meta";

        const phaseEl = document.createElement("span");
        phaseEl.className = "indexing-file-phase";
        phaseEl.textContent = phaseLabel;

        const pctEl = document.createElement("span");
        pctEl.className = "indexing-file-pct";
        pctEl.textContent = pctLabel;

        metaEl.append(phaseEl, pctEl);
        row.append(nameEl, metaEl);
        dom.indexingFileList.appendChild(row);
      }
    }
  }

  function updateComposerLockState() {
    if (!dom.queryInput || !dom.queryForm) {
      return;
    }
    const activeCount = getActiveWorkspaceUploadJobs().length;
    const wasLocked = dom.queryInput.disabled;
    const shouldLock = activeCount > 0 && !state.isStreaming;

    if (shouldLock) {
      dom.queryInput.disabled = true;
      dom.queryForm.classList.add("is-locked");
      dom.queryInput.placeholder = `Dokümanlar işleniyor (${activeCount} dosya kaldı)... Tamamlandığında soru sorabilirsiniz.`;
      dom.sendBtn.disabled = true;
    } else {
      dom.queryInput.disabled = false;
      dom.queryForm.classList.remove("is-locked");
      dom.queryInput.placeholder = "Dokümanlarınıza bir soru sorun...";
      updateSendButtonDisabledState();
      if (
        wasLocked &&
        activeCount === 0 &&
        (!dom.apiKeyModal || dom.apiKeyModal.classList.contains("hidden"))
      ) {
        dom.queryInput.focus();
      }
    }
  }

  function syncUploadUi() {
    renderDropzoneSubtitle();
    renderDocumentList();
    renderIndexingStageCard();
    updateComposerLockState();
    updateSystemStatus();
    ensureCountdownTicker();
  }

  function failUploadJob(job, phaseText) {
    job.state = "error";
    job.isIndeterminate = false;
    job.isRateLimited = false;
    job.rateLimitResumeAt = null;
    job.phaseText = phaseText;
    markBatchJobCompleted(job.tenantId, job.localId, "error", phaseText);
    if (!hasActiveWorkspaceUploads(job.tenantId)) {
      state.batchByTenant.delete(job.tenantId);
    }
    syncUploadUi();
  }

  function pruneUploadWindowTimestamps(nowMs = Date.now()) {
    const windowMs = Math.max(1, state.uploadRateWindowSeconds) * 1000;
    state.uploadWindowTimestamps = state.uploadWindowTimestamps.filter(
      (ts) => nowMs - ts < windowMs
    );
    return state.uploadWindowTimestamps;
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
    if (accepted.length === 0) {
      return;
    }

    if (state.maxUploadPerWindow > 0) {
      if (accepted.length > state.maxUploadPerWindow) {
        showToast(
          `Tek seferde en fazla ${state.maxUploadPerWindow} dosya yükleyebilirsiniz (${accepted.length} dosya seçildi). Lütfen en fazla ${state.maxUploadPerWindow} dosya seçin.`,
          true
        );
        return;
      }

      const quota = getUploadQuotaInfo();
      if (accepted.length > quota.remainingSlots) {
        if (quota.remainingSlots === 0) {
          showToast(
            `Yükleme hız sınırı dolu (${quota.windowSeconds} sn içinde en fazla ${quota.limit} dosya). Lütfen ${quota.waitSeconds} sn bekleyin.`,
            true
          );
        } else {
          showToast(
            `Hız sınırı nedeniyle şu an en fazla ${quota.remainingSlots} dosya yükleyebilirsiniz (${accepted.length} dosya seçildi).`,
            true
          );
        }
        return;
      }
    }

    const acceptedAt = Date.now();
    for (let i = 0; i < accepted.length; i += 1) {
      state.uploadWindowTimestamps.push(acceptedAt);
    }

    for (const file of accepted) {
      enqueueFileUpload(file);
    }
    pumpUploadQueue();
  }

  function enqueueFileUpload(file) {
    const localId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    const ext = file.name.toLowerCase().endsWith(".txt") ? "txt" : "pdf";
    const batch = ensureActiveBatch(state.tenantId);
    batch.jobMeta.set(localId, {
      localId,
      filename: file.name,
      fileType: ext,
      finalState: null,
      phaseText: "Sırada bekliyor...",
    });
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
      isRateLimited: false,
      rateLimitResumeAt: null,
      rateLimitProvider: "",
      chunksDone: 0,
      chunksTotal: 0,
      pageCount: 0,
      documentId: null,
      taskId: null,
    };
    state.uploads.set(localId, job);
    state.uploadQueue.push(localId);
    syncUploadUi();
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
    syncUploadUi();

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
        renderIndexingStageCard();
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
        syncUploadUi();
        releaseSlot();
        pollTaskStatus(localId);
        return;
      }

      if (xhr.status === 401) {
        state.apiKeyVerified = false;
        updateApiKeyButtonState();
        failUploadJob(job, "API anahtarı doğrulanamadı");
        openApiKeyModal("Doküman yüklemek için geçerli bir API anahtarı girin.");
      } else if (xhr.status === 429) {
        const retryAfter = xhr.getResponseHeader("Retry-After");
        const rateLimitMsg = retryAfter
          ? `Hız sınırı aşıldı (${retryAfter} sn bekleyin)`
          : "Hız sınırı aşıldı";
        failUploadJob(job, rateLimitMsg);
        showToast(rateLimitMsg, true);
      } else {
        if (xhr.status === 503) {
          void scheduleHealthRefresh();
        }
        failUploadJob(job, parsed?.error?.message || `Yükleme başarısız (${xhr.status})`);
      }
      releaseSlot();
    };

    xhr.onerror = () => {
      failUploadJob(job, "Ağ bağlantısı hatası");
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
        failUploadJob(job, "API anahtarı doğrulanamadı");
        return;
      }
      if (response.status === 400 || response.status === 404 || response.status === 422) {
        failUploadJob(job, "Görev takibi sonlandırıldı");
        return;
      }
      if (response.status === 429) {
        const retryAfter = Number(response.headers.get("Retry-After")) || 5;
        const waitSec = Math.max(2, retryAfter);
        job.phaseText = `Hız sınırı bekleniyor (${waitSec} sn)...`;
        renderDocumentList();
        renderIndexingStageCard();
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
          failUploadJob(job, "Geçici dosya bulunamadı");
          return;
        }
        if (resultStatus === "no_text_extracted" || chunkCount === 0) {
          failUploadJob(job, "Metin çıkarılamadı (taranmış veya boş belge)");
          showToast(`"${job.filename}" içinde çıkarılabilir metin bulunamadı`, true);
          return;
        }
        if (resultStatus === "quota_exhausted") {
          const quotaMsg =
            typeof snapshot.result?.error === "string" && snapshot.result.error
              ? snapshot.result.error
              : "❌ AI API günlük ücretsiz kotası doldu.";
          failUploadJob(job, quotaMsg);
          while (state.uploadQueue.length > 0) {
            const queuedId = state.uploadQueue.shift();
            const queuedJob = queuedId ? state.uploads.get(queuedId) : null;
            if (queuedJob && queuedJob.state === "queued") {
              failUploadJob(queuedJob, quotaMsg);
            }
          }
          showToast(quotaMsg, true);
          return;
        }

        job.state = "done";
        job.progress = 100;
        job.isIndeterminate = false;
        job.isRateLimited = false;
        job.rateLimitResumeAt = null;
        job.phaseText = "Hazır • %100";
        markBatchJobCompleted(job.tenantId, localId, "done", "İndekslendi ✓");
        renderDocumentList();
        updateSystemStatus();

        if (job.tenantId === state.tenantId) {
          if (hasActiveWorkspaceUploads(job.tenantId)) {
            renderIndexingStageCard();
            updateComposerLockState();
          }
          await fetchDocuments();
        }
        if (!hasActiveWorkspaceUploads(job.tenantId)) {
          state.batchByTenant.delete(job.tenantId);
        }
        renderIndexingStageCard();
        updateComposerLockState();
        ensureCountdownTicker();
        window.setTimeout(() => {
          state.uploads.delete(localId);
          syncUploadUi();
        }, 420);
        showToast(`"${job.filename}" indekslendi`);
        return;
      }

      if (snapshot.status === "FAILURE") {
        failUploadJob(job, snapshot.error || "Doküman işlenemedi");
        return;
      }

      if (snapshot.status === "STARTED") {
        const meta =
          snapshot.result && typeof snapshot.result === "object" ? snapshot.result : null;
        if (meta && (meta.phase === "embedding" || meta.phase === "rate_limited")) {
          const done = Number(meta.chunks_done) || 0;
          const total = Number(meta.chunks_total) || 0;
          const pages = Number(meta.page_count) || 0;
          const ratio = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0;
          job.chunksDone = done;
          job.chunksTotal = total;
          job.pageCount = pages;
          if (meta.phase === "rate_limited") {
            job.isRateLimited = true;
            job.rateLimitProvider =
              typeof meta.provider === "string" && meta.provider ? meta.provider : "AI API";
            const resumeAtMs = Number(meta.resume_at_epoch_ms);
            const fallbackWaitSec = Math.max(1, Number(meta.wait_seconds) || 15);
            job.rateLimitResumeAt =
              Number.isFinite(resumeAtMs) && resumeAtMs > Date.now()
                ? resumeAtMs
                : Date.now() + fallbackWaitSec * 1000;
            job.progress = Math.max(job.progress || 76, Math.round(76 + ratio * 21));
            job.phaseText = formatJobPhaseText(job);
          } else {
            job.isRateLimited = false;
            job.rateLimitResumeAt = null;
            job.progress = Math.max(job.progress || 76, Math.round(76 + ratio * 22));
            job.phaseText =
              total > 0
                ? `Vektörleniyor • ${done} / ${total} parça`
                : "Parçalanıyor & Vektörleniyor...";
          }
        } else {
          job.isRateLimited = false;
          job.rateLimitResumeAt = null;
          job.progress = Math.max(job.progress || 74, 88);
          job.phaseText = "Parçalanıyor & Vektörleniyor...";
        }
      } else if (snapshot.status === "RETRY") {
        job.isRateLimited = false;
        job.rateLimitResumeAt = null;
        job.progress = 80;
        job.phaseText = "Yeniden deneniyor...";
      } else {
        job.isRateLimited = false;
        job.rateLimitResumeAt = null;
        job.progress = 74;
        job.phaseText = "Sırada bekliyor...";
      }
      renderDocumentList();
      renderIndexingStageCard();
      ensureCountdownTicker();
    } catch {
      job.phaseText = "Durum bekleniyor...";
      nextDelayMs = Math.min(5000, Math.round(nextDelayMs * 1.5));
      renderDocumentList();
      renderIndexingStageCard();
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
    renderIndexingStageCard();
    updateComposerLockState();
    if (!hasActiveWorkspaceUploads()) {
      dom.queryInput.focus();
    }
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
      updateComposerLockState();
    }
    updateSystemStatus();
  }

  function updateSendButtonDisabledState() {
    if (state.isStreaming) {
      dom.sendBtn.disabled = false;
      return;
    }
    if (hasActiveWorkspaceUploads()) {
      dom.sendBtn.disabled = true;
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

  function isTrivialGreetingText(queryText) {
    const cleaned = String(queryText || "")
      .replace(/\s+/g, " ")
      .trim();
    if (!cleaned) return false;
    if (
      /^(?:merhaba|selam|slm|hi|hello|hey|günaydın|iyi akşamlar|teşekkürler|sağ ol)[!.\s]*$/i.test(
        cleaned
      )
    ) {
      return true;
    }
    const withoutGreeting = cleaned
      .replace(/^(?:merhaba|selam|slm|hi|hello|hey|günaydın|iyi\s+akşamlar)[,!.\s]+/i, "")
      .trim();
    const identityRe =
      /^(?:(?:sen\s+)?kimsin(?:\s+(?:ve|ya\s+da)\s+(?:ne\s+(?:işe|ise)\s+yar[ıi]yorsun|neler\s+yapabilirsin|nas[ıi]l\s+yard[ıi]mc[ıi]\s+olabilirsin))?|ne\s+(?:işe|ise)\s+yar[ıi]yorsun|(?:neler|ne)\s+yapabilirsin|(?:bana\s+)?nas[ıi]l\s+yard[ıi]m(?:c[ıi])?\s+(?:olabilirsin|edebilirsin)|kendini\s+tan[ıi]t(?:[ıi]r\s+m[ıi]s[ıi]n)?|(?:sen\s+)?nas[ıi]ls[ıi]n|who\s+are\s+you(?:\s+and\s+(?:what\s+(?:do|can)\s+you\s+do|how\s+can\s+you\s+help(?:\s+me)?))?|what\s+(?:do|can)\s+you\s+do|how\s+can\s+you\s+help(?:\s+me)?|introduce\s+yourself)[?!.\s]*$/i;
    return (
      Boolean(withoutGreeting && identityRe.test(withoutGreeting)) ||
      identityRe.test(cleaned)
    );
  }

  function renderCitations(citationsWrap, citations, queryText = "") {
    citationsWrap.replaceChildren();

    if (!citations || citations.length === 0) {
      if (isTrivialGreetingText(queryText)) {
        citationsWrap.classList.add("hidden");
        return null;
      }
      citationsWrap.classList.remove("hidden");
      const note = document.createElement("div");
      note.className = "no-citations-note";
      note.textContent = "Yüklü dokümanlarda eşik üzerinde eşleşen kaynak bulunamadı.";
      citationsWrap.appendChild(note);
      return null;
    }

    citationsWrap.classList.remove("hidden");

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

    if (
      /başka\s+(?:bir\s+)?(?:soru|yardım|konu)|merak\s+ettiğiniz\s+başka|\bvar\s+m[ıi]\s*\?*\s*$|yardımcı\s+olabilir\s+miyim|anything\s+else/i.test(
        questionSentence
      )
    ) {
      return null;
    }
    if (
      /^(?:hangi|hangisi\w*|ne\s+tür|nas[ıi]l\s+bir|which|what\s+kind)\b/i.test(
        questionSentence.trim()
      ) ||
      /\b(?:hangi\s+\S+|hangisini\w*|nerede|neden)\b.*\b(?:istersiniz|tercih\s+edersiniz)\?\s*$/i.test(
        questionSentence
      )
    ) {
      return null;
    }

    const rules = [
      { re: /öğrenmek\s+ister\s+misiniz/i, text: "Evet, öğrenmek isterim" },
      { re: /incelememizi|incelemek\s+ister\s+misiniz/i, text: "Evet, inceleyelim" },
      { re: /karşılaştırmamızı|kıyaslamamızı/i, text: "Evet, karşılaştıralım" },
      { re: /özetlememi|özet\s+geçmemi/i, text: "Evet, özetle" },
      {
        re: /detaylandırmamı|detaylı\s+(?:anlatmamı|bakmamızı|görmek)/i,
        text: "Evet, detaylandır",
      },
      { re: /paylaşmamı|görmek\s+ister\s+misiniz/i, text: "Evet, paylaşır mısın" },
      { re: /açıklamamı|anlatmamı/i, text: "Evet, açıklar mısın" },
      { re: /ister\s+misiniz|istiyorsanız|merak\s+ediyor\s+musunuz/i, text: "Evet, isterim" },
      {
        re: /devam\s+edelim\s+mi|örnek\s+vereyim\s+mi|göstereyim\s+mi|anlatayım\s+m[ıi]|ekleyeyim\s+mi/i,
        text: "Evet, yapalım",
      },
      { re: /would\s+you\s+like.*compare/i, text: "Yes, let's compare" },
      { re: /would\s+you\s+like.*summarize/i, text: "Yes, please summarize" },
      { re: /would\s+you\s+like|shall\s+i|do\s+you\s+want/i, text: "Yes, let's do that" },
    ];

    for (const rule of rules) {
      if (rule.re.test(questionSentence)) {
        return { label: `${rule.text} →`, prompt: rule.text };
      }
    }

    return null;
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

  function formatStreamErrorMessage(code, rawMessage) {
    if (code === "content_filtered") {
      return "Yanıt, yapay zeka içerik/tekrar filtresi nedeniyle tamamlanamadan durduruldu. Lütfen soruyu farklı şekilde sormayı deneyin.";
    }
    if (code === "stream_truncated") {
      return "Yanıt maksimum uzunluk sınırına ulaştığı için tamamlanamadan kesildi.";
    }
    if (
      code === "service_unavailable" &&
      typeof rawMessage === "string" &&
      (rawMessage.includes("closed before completion") ||
        rawMessage.includes("terminated unexpectedly") ||
        rawMessage.includes("reported an error during streaming"))
    ) {
      return "Yanıt akışı beklenmedik şekilde yarıda kesildi. Lütfen soruyu yeniden gönderin.";
    }
    return rawMessage || "Yanıt akışı sırasında hata oluştu.";
  }

  async function submitQuery(rawQuery) {
    if (hasActiveWorkspaceUploads()) {
      showToast("Doküman indeksleme işlemi tamamlanana kadar soru gönderilemez.", true);
      return;
    }
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
    let sawDoneEvent = false;
    const historySnapshot = state.chatHistory
      .slice(-MAX_HISTORY_MESSAGES)
      .map((item) => ({
        role: item.role,
        content: String(item.content || "").trim().slice(0, MAX_HISTORY_CONTENT_CHARS).trim(),
      }))
      .filter((item) => item.content.length > 0);

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
          const errorCode = typeof payload.code === "string" ? payload.code : "";
          streamErrorMessage = formatStreamErrorMessage(errorCode, payload.message);
        } else if (parsed.event === "done") {
          sawDoneEvent = true;
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

      buffer += decoder.decode();
      if (buffer.trim()) {
        processSseFrame(buffer);
      }

      if (!streamErrorMessage && !sawDoneEvent) {
        streamErrorMessage =
          "Yanıt akışı tamamlanmadan sunucu bağlantısı kesildi. Lütfen soruyu yeniden deneyin.";
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
        if (answerText) {
          updateAssistantText(turn.body, answerText, false);
        } else {
          turn.body.replaceChildren();
        }
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
        state.selectedDocumentIds.clear();
        renderDocumentList();
        renderSummaryTexts();
        renderScopeChips();
        renderStarterPrompts();
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
      if (event.key === "/" && !isTyping && !dom.queryInput.disabled) {
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
    renderDropzoneSubtitle();
    updateApiKeyButtonState();
    renderWorkspaceList();
    renderSummaryTexts();
    renderScopeChips();
    renderStarterPrompts();
    restoreChatTranscript();
    renderIndexingStageCard();
    updateComposerLockState();
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
