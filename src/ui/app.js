(() => {
  "use strict";

  const TENANT_PATTERN = /^[a-zA-Z0-9_-]{1,64}$/;
  const DEFAULT_TENANT = "test-user";
  const STORAGE_TENANT_KEY = "corerag.tenantId";
  const STORAGE_TENANTS_LIST_KEY = "corerag.savedTenants";
  const STORAGE_API_KEY = "corerag.apiKey";
  const POLL_INTERVAL_MS = 900;

  const initialTenant = loadInitialTenant();
  const state = {
    tenantId: initialTenant,
    savedTenants: loadSavedTenants(initialTenant),
    apiKey: loadInitialApiKey(),
    apiKeyVerified: false,
    documents: [],
    docNameById: new Map(),
    uploads: new Map(),
    confirmDeleteId: null,
    confirmTimerId: null,
    isStreaming: false,
    streamController: null,
    hasMessages: false,
    healthStatus: "ok",
    dragCounter: 0,
  };

  const dom = {
    sidebar: document.getElementById("sidebar"),
    sidebarBackdrop: document.getElementById("sidebar-backdrop"),
    mobileOpenBtn: document.getElementById("mobile-sidebar-open"),
    mobileCloseBtn: document.getElementById("mobile-sidebar-close"),
    dropzone: document.getElementById("dropzone"),
    fileInput: document.getElementById("file-input"),
    selectFilesBtn: document.getElementById("select-files-btn"),
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
    queryForm: document.getElementById("query-form"),
    queryInput: document.getElementById("query-input"),
    sendBtn: document.getElementById("send-btn"),
    sendIcon: document.querySelector("#send-btn .icon-send"),
    stopIcon: document.querySelector("#send-btn .icon-stop"),
    toastStack: document.getElementById("toast-stack"),
    windowDropOverlay: document.getElementById("window-drop-overlay"),
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
    saveTenant(nextTenant);
    renderWorkspaceList();
    renderSummaryTexts();
    clearConversation();
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
      (item) => item.state === "uploading" || item.state === "indexing"
    );

    dom.statusDot.classList.remove("is-ready", "is-busy", "is-degraded");
    if (state.isStreaming) {
      dom.statusDot.classList.add("is-busy");
      dom.statusText.textContent = "Yanıt Yazılıyor";
    } else if (activeUploads.length > 0) {
      dom.statusDot.classList.add("is-busy");
      dom.statusText.textContent = "Doküman İşleniyor";
    } else if (state.healthStatus !== "ok") {
      dom.statusDot.classList.add("is-degraded");
      dom.statusText.textContent = "Sistem Kısıtlı";
    } else {
      dom.statusDot.classList.add("is-ready");
      dom.statusText.textContent = "Sistem Hazır";
    }
  }

  async function checkHealth() {
    try {
      const response = await fetch("/api/v1/health");
      if (!response.ok) {
        state.healthStatus = "degraded";
      } else {
        const data = await response.json();
        state.healthStatus = data.status === "ok" ? "ok" : "degraded";
      }
    } catch {
      state.healthStatus = "degraded";
    }
    updateSystemStatus();
  }

  async function fetchDocuments() {
    if (!state.apiKey) {
      state.documents = [];
      state.docNameById = new Map();
      renderDocumentList();
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
      renderDocumentList();
      renderSummaryTexts();
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

  function renderDocumentList() {
    dom.docList.replaceChildren();

    const uploadItems = Array.from(state.uploads.values());
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
    const row = document.createElement("div");
    row.className = "doc-item";
    row.dataset.documentId = doc.document_id;

    const main = document.createElement("div");
    main.className = "doc-item-main";

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

    main.append(badge, meta, actions);
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
      startFileUpload(file);
    }
  }

  function startFileUpload(file) {
    const localId = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
    const ext = file.name.toLowerCase().endsWith(".txt") ? "txt" : "pdf";
    const job = {
      localId,
      filename: file.name,
      fileType: ext,
      progress: 8,
      phaseText: "Yükleniyor • %8",
      state: "uploading",
      isIndeterminate: false,
      documentId: null,
      taskId: null,
    };
    state.uploads.set(localId, job);
    renderDocumentList();
    updateSystemStatus();

    const formData = new FormData();
    formData.append("file", file);
    formData.append("tenant_id", state.tenantId);

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
        job.phaseText = parsed?.error?.message || `Yükleme başarısız (${xhr.status})`;
      }
      renderDocumentList();
      updateSystemStatus();
    };

    xhr.onerror = () => {
      job.state = "error";
      job.isIndeterminate = false;
      job.phaseText = "Ağ bağlantısı hatası";
      renderDocumentList();
      updateSystemStatus();
    };

    xhr.send(formData);
  }

  async function pollTaskStatus(localId) {
    const job = state.uploads.get(localId);
    if (!job || !job.taskId || job.state !== "indexing") {
      return;
    }

    try {
      const response = await apiFetch(
        `/api/v1/documents/status/${encodeURIComponent(job.taskId)}`
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const snapshot = await response.json();

      if (snapshot.status === "SUCCESS") {
        const resultStatus = snapshot.result?.status;
        if (resultStatus === "missing_file") {
          job.state = "error";
          job.isIndeterminate = false;
          job.phaseText = "Geçici dosya bulunamadı";
          renderDocumentList();
          updateSystemStatus();
          return;
        }

        job.progress = 100;
        job.isIndeterminate = false;
        job.phaseText = "Hazır • %100";
        renderDocumentList();

        await fetchDocuments();
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
      renderDocumentList();
    }

    window.setTimeout(() => pollTaskStatus(localId), POLL_INTERVAL_MS);
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

  function formatInlineMarkdown(escapedLine) {
    return escapedLine
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  }

  function renderSafeMarkdown(text) {
    const lines = escapeHtml(text).replace(/\r\n/g, "\n").split("\n");
    const htmlParts = [];
    let listType = null;

    const closeList = () => {
      if (listType === "ul") {
        htmlParts.push("</ul>");
      } else if (listType === "ol") {
        htmlParts.push("</ol>");
      }
      listType = null;
    };

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) {
        closeList();
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

    closeList();
    return htmlParts.join("");
  }

  function hideEmptyState() {
    if (!state.hasMessages) {
      state.hasMessages = true;
      dom.emptyState.classList.add("hidden");
      dom.clearChatBtn.classList.remove("hidden");
    }
  }

  function clearConversation() {
    if (state.isStreaming && state.streamController) {
      state.streamController.abort();
    }
    dom.messagesList.replaceChildren();
    state.hasMessages = false;
    dom.emptyState.classList.remove("hidden");
    dom.clearChatBtn.classList.add("hidden");
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

  function createAssistantMessageTurn() {
    const row = document.createElement("div");
    row.className = "msg-row is-assistant";

    const body = document.createElement("div");
    body.className = "assistant-body";

    const thinking = document.createElement("div");
    thinking.className = "thinking-dots";
    for (let i = 0; i < 3; i += 1) {
      const dot = document.createElement("span");
      dot.className = "thinking-dot";
      thinking.appendChild(dot);
    }
    body.appendChild(thinking);

    const citationsWrap = document.createElement("div");
    citationsWrap.className = "citations-wrap hidden";

    row.append(body, citationsWrap);
    dom.messagesList.appendChild(row);
    scrollChatToBottomIfNearBottom(true);

    return { row, body, citationsWrap };
  }

  function updateAssistantText(bodyEl, text, showCaret) {
    if (!text) {
      return;
    }
    bodyEl.innerHTML = renderSafeMarkdown(text);
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

  function renderCitations(citationsWrap, citations) {
    citationsWrap.replaceChildren();
    citationsWrap.classList.remove("hidden");

    if (!citations || citations.length === 0) {
      const note = document.createElement("div");
      note.className = "no-citations-note";
      note.textContent = "Yüklü dokümanlarda eşik üzerinde eşleşen kaynak bulunamadı.";
      citationsWrap.appendChild(note);
      return;
    }

    const pillsCol = document.createElement("div");
    pillsCol.className = "citations-pills";

    const quoteBox = document.createElement("div");
    quoteBox.className = "citation-quote-card hidden";

    let activeIndex = -1;

    citations.forEach((cit, idx) => {
      const sourceNum = idx + 1;
      const filename = resolveCitationFilename(cit);
      const scorePct = Math.round((Number(cit.score) || 0) * 100);
      const pageNum = Number(cit.page_number) || 1;

      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "citation-pill";
      pill.appendChild(createSmallDocSvg());

      const numBadge = document.createElement("span");
      numBadge.className = "citation-source-num";
      numBadge.textContent = `[${sourceNum}]`;
      pill.appendChild(numBadge);

      const label = document.createElement("span");
      label.className = "citation-pill-text";
      label.textContent = `${filename} • Sayfa ${pageNum} • %${scorePct} eşleşme`;
      pill.appendChild(label);

      pill.addEventListener("click", () => {
        const allPills = pillsCol.querySelectorAll(".citation-pill");
        if (activeIndex === idx) {
          activeIndex = -1;
          pill.classList.remove("is-active");
          quoteBox.classList.add("hidden");
          return;
        }

        allPills.forEach((btn) => btn.classList.remove("is-active"));
        pill.classList.add("is-active");
        activeIndex = idx;

        quoteBox.replaceChildren();
        const meta = document.createElement("div");
        meta.className = "citation-quote-meta";

        const leftMeta = document.createElement("span");
        leftMeta.textContent = `[Kaynak ${sourceNum}] ${filename} — Sayfa ${pageNum}`;
        const rightMeta = document.createElement("span");
        rightMeta.textContent = `Benzerlik: %${scorePct}`;
        meta.append(leftMeta, rightMeta);

        const body = document.createElement("p");
        body.className = "citation-quote-body";
        body.textContent = cit.text || "";

        quoteBox.append(meta, body);
        quoteBox.classList.remove("hidden");
        quoteBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });

      pillsCol.appendChild(pill);
    });

    citationsWrap.append(pillsCol, quoteBox);
    scrollChatToBottomIfNearBottom(false);
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
    appendUserBubble(query);

    dom.queryInput.value = "";
    autoResizeTextarea();

    const turn = createAssistantMessageTurn();
    const controller = new AbortController();
    state.streamController = controller;
    setStreamingUi(true);

    let answerText = "";
    let citations = [];
    let streamErrorMessage = "";

    try {
      const response = await apiFetch("/api/v1/query/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query,
          tenant_id: state.tenantId,
          stream: true,
        }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(await extractErrorMessage(response));
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });

        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() ?? "";

        for (const frame of frames) {
          if (!frame.trim()) {
            continue;
          }
          const parsed = parseSseFrame(frame);
          if (!parsed) {
            continue;
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
        }
      }

      if (buffer.trim()) {
        const trailing = parseSseFrame(buffer);
        if (trailing?.event === "citations") {
          const payload = JSON.parse(trailing.data);
          citations = Array.isArray(payload.citations) ? payload.citations : [];
        }
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
        updateAssistantText(
          turn.body,
          answerText || "Yüklü dokümanlarda bu soruya ilişkin bir yanıt üretilemedi.",
          false
        );
        renderCitations(turn.citationsWrap, citations);
      }
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        updateAssistantText(turn.body, answerText || "Yanıt oluşturma durduruldu.", false);
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

    dom.queryInput.addEventListener("input", autoResizeTextarea);

    dom.queryInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (!state.isStreaming) {
          submitQuery(dom.queryInput.value);
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
  }

  function init() {
    bindEvents();
    updateApiKeyButtonState();
    renderWorkspaceList();
    renderSummaryTexts();
    checkHealth();
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
