(() => {
  "use strict";

  let csrfToken = "";
  let lastConfig = null;
  let currentSection = "overview";
  const completedSections = new Set();
  const ruTranslations = window.SetupWebTranslations || {};
  const originalTextNodes = new WeakMap();
  const originalAttributes = new WeakMap();

  let currentLanguage = (() => {
    try {
      const saved = localStorage.getItem("setupweb-language");
      if (saved === "ru" || saved === "en") return saved;
    } catch {
      // The current page can still switch language without persistence.
    }
    return navigator.language.toLowerCase().startsWith("ru") ? "ru" : "en";
  })();

  const messageArea = document.querySelector("#messageArea");

  function t(value) {
    return currentLanguage === "ru" ? (ruTranslations[value] ?? value) : value;
  }

  function applyLanguage() {
    document.documentElement.lang = currentLanguage;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!originalTextNodes.has(node)) originalTextNodes.set(node, node.nodeValue);
      const source = originalTextNodes.get(node);
      const trimmed = source.trim();
      if (!trimmed) continue;
      const leading = source.match(/^\s*/)[0];
      const trailing = source.match(/\s*$/)[0];
      node.nodeValue = `${leading}${t(trimmed)}${trailing}`;
    }

    document.querySelectorAll("[title], [placeholder], [aria-label]").forEach((element) => {
      if (!originalAttributes.has(element)) {
        originalAttributes.set(element, {
          title: element.getAttribute("title"),
          placeholder: element.getAttribute("placeholder"),
          ariaLabel: element.getAttribute("aria-label"),
        });
      }
      const source = originalAttributes.get(element);
      if (source.title !== null) element.setAttribute("title", t(source.title));
      if (source.placeholder !== null) element.setAttribute("placeholder", t(source.placeholder));
      if (source.ariaLabel !== null) element.setAttribute("aria-label", t(source.ariaLabel));
    });

    document.querySelectorAll("[data-language]").forEach((button) => {
      const active = button.dataset.language === currentLanguage;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
  }

  function setLanguage(language, persist = true) {
    currentLanguage = language === "ru" ? "ru" : "en";
    if (persist) {
      try {
        localStorage.setItem("setupweb-language", currentLanguage);
      } catch {
        // The selected language still applies to this page.
      }
    }
    applyLanguage();
    if (lastConfig) renderConfig(lastConfig);
  }

  function showMessage(text, type = "success") {
    messageArea.hidden = false;
    messageArea.className = `notice ${type}`;
    messageArea.textContent = t(text);
  }

  function clearMessage() {
    messageArea.hidden = true;
    messageArea.textContent = "";
  }

  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("content-type")) {
      headers.set("content-type", "application/json");
    }
    if (options.method && options.method !== "GET") {
      headers.set("x-csrf-token", csrfToken);
    }
    const response = await fetch(url, {
      ...options,
      headers,
      credentials: "same-origin",
    });
    if (!response.ok) {
      const responseText = await response.text();
      let detail = responseText;
      try {
        detail = JSON.parse(responseText).detail || responseText;
      } catch {
        detail = responseText;
      }
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return response.json();
  }

  function setField(selector, values) {
    document.querySelectorAll(selector).forEach((input) => {
      const key = input.dataset.api || input.dataset.winpe || input.dataset.unattend || input.dataset.auth;
      if (!(key in values)) return;
      if (input.type === "checkbox") {
        input.checked = String(values[key]).toLowerCase() === "true";
      } else {
        input.value = values[key] ?? "";
      }
    });
  }

  function collectFields(container, selector, datasetName) {
    const values = {};
    container.querySelectorAll(selector).forEach((input) => {
      values[input.dataset[datasetName]] =
        input.type === "checkbox" ? input.checked : input.value;
    });
    return values;
  }

  function populateTimeZones(config) {
    const select = document.querySelector("#timeZoneSelect");
    if (!select) return;
    const selected = select.value;
    select.replaceChildren();
    for (const zone of config.timeZones || []) {
      const option = document.createElement("option");
      option.value = zone.id;
      option.textContent = `(${zone.offset}) ${zone.label}`;
      select.append(option);
    }
    if (selected) select.value = selected;
  }

  function renderComputerNamePreview() {
    const prefix = document.querySelector("[data-api='IRONAPI_NAME_PREFIX']")?.value.trim() || "pc";
    const parsedWidth = Number.parseInt(
      document.querySelector("[data-api='IRONAPI_NAME_WIDTH']")?.value,
      10
    );
    const width = Number.isInteger(parsedWidth) && parsedWidth > 0
      ? Math.min(parsedWidth, 20)
      : 5;
    const parsedStart = Number.parseInt(
      document.querySelector("[data-api='IRONAPI_NAME_START']")?.value,
      10
    );
    const start = Number.isInteger(parsedStart) && parsedStart >= 0 ? parsedStart : 1;
    document.querySelector("#computerNamePreview").textContent =
      `${prefix}${String(start).padStart(width, "0")}`;
  }

  function renderStoragePaths() {
    const drive = document.querySelector("[data-winpe='ShareDrive']")?.value.trim() || "Z:";
    document.querySelector("[data-winpe='ImagesPath']").value = `${drive}\\Images`;
    document.querySelector("[data-winpe='DriversPath']").value = `${drive}\\Drivers`;
  }

  function smbPartsFromUnc(value) {
    const match = String(value || "").trim().match(/^\\\\([^\\]+)\\([^\\]+)$/);
    return match ? { serverAddress: match[1], shareName: match[2] } : null;
  }

  function isValidSmbServerAddress(value) {
    if (!value || value.length > 253 || !/^[A-Za-z0-9._-]+$/.test(value)) return false;
    if (/^[0-9.]+$/.test(value) && value.includes(".")) {
      const octets = value.split(".");
      return octets.length === 4 && octets.every((octet) => {
        if (!/^\d{1,3}$/.test(octet)) return false;
        return Number(octet) >= 0 && Number(octet) <= 255;
      });
    }
    return value.split(".").every((label) => (
      label.length >= 1 &&
      label.length <= 63 &&
      !label.startsWith("-") &&
      !label.endsWith("-")
    ));
  }

  function syncSmbPath() {
    const serverName = document.querySelector("#smbServerName").value.trim();
    const shareName = document.querySelector("#smbShareName").value.trim();
    const uncPath = serverName && shareName ? `\\\\${serverName}\\${shareName}` : "";
    document.querySelector("#smbSharePath").value = uncPath;
    document.querySelector("#smbUncPreview").textContent =
      uncPath || t("Enter an SMB address and share name");
    return uncPath;
  }

  function renderSmbSettings(config) {
    const existingPath = String(config.winpe?.SharePath || "");
    const existingParts = smbPartsFromUnc(existingPath);
    const serverName = existingParts?.serverAddress ||
      String(config.localSmb?.serverName || "localhost");
    const localPath = String(config.localSmb?.localPath || `${config.root}\\Share`);
    document.querySelector("#smbServerName").value = serverName;
    document.querySelector("#smbShareName").value = existingParts?.shareName || "IronDeploy";
    document.querySelector("#smbLocalPath").textContent = localPath;
    document.querySelector("#smbPasswordStatus").textContent = t(
      config.secrets?.hasWinpeSharePassword
        ? "Saved password will be used when this field is empty"
        : "Password is required"
    );
    document.querySelector("#smbActionStatus").textContent = "";
    document.querySelector("#smbActionStatus").classList.remove("is-error");
    syncSmbPath();
  }

  function renderOdjSettings(config) {
    document.querySelector("#odjProcessAccount").value =
      String(config.localOdj?.processAccount || "");
    document.querySelector("#odjFolderPath").textContent =
      String(config.localOdj?.path || `${config.root}\\ODJ`);
    const status = document.querySelector("#odjAclStatus");
    status.textContent = "";
    status.classList.remove("is-error");
  }

  async function secureOdjFolder() {
    clearMessage();
    const accountInput = document.querySelector("#odjProcessAccount");
    const account = accountInput.value.trim();
    const accountParts = account.match(/^([^\\/@\r\n]+)\\([^\\/@\r\n]+)$/);
    const accountValid = Boolean(
      accountParts &&
      account.length <= 256 &&
      accountParts[1] === accountParts[1].trim() &&
      accountParts[2] === accountParts[2].trim()
    );
    accountInput.setCustomValidity(accountValid ? "" : t(
      "Use DOMAIN\\user or COMPUTER\\user format."
    ));
    if (!accountValid) {
      accountInput.reportValidity();
      return;
    }

    if (!window.confirm(t(
      "Replace ODJ permissions with SYSTEM, Administrators, and this IronAPI account?"
    ))) {
      return;
    }

    const button = document.querySelector("#secureOdjFolderButton");
    const statusElement = document.querySelector("#odjAclStatus");
    button.disabled = true;
    statusElement.classList.remove("is-error");
    statusElement.textContent = t("Securing ODJ folder...");
    try {
      const result = await apiFetch("/api/odj/secure-folder", {
        method: "POST",
        body: JSON.stringify({ account }),
      });
      const aclAccount = String(result.aclAccount || account);
      accountInput.value = aclAccount;
      statusElement.textContent = `${t("ODJ folder secured for")} ${aclAccount}.`;
    } catch (error) {
      statusElement.textContent = error.message;
      statusElement.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  }

  function validateSmbSettings() {
    const serverAddress = document.querySelector("#smbServerName");
    const shareName = document.querySelector("#smbShareName");
    const account = document.querySelector("[data-winpe='ShareUser']");
    const normalizedServerAddress = serverAddress.value.trim();
    const normalizedShareName = shareName.value.trim();
    const normalizedAccount = account.value.trim();
    const shareNameValid = /^[A-Za-z0-9._-]{1,80}$/.test(normalizedShareName);
    const accountParts = normalizedAccount.match(/^([^\\/@\r\n]+)\\([^\\/@\r\n]+)$/);
    const accountValid = Boolean(
      accountParts &&
      accountParts[1] === accountParts[1].trim() &&
      accountParts[2] === accountParts[2].trim()
    );
    const serverAddressValid = isValidSmbServerAddress(normalizedServerAddress);
    serverAddress.setCustomValidity(serverAddressValid ? "" : t(
      "Use a hostname, FQDN, or IPv4 address."
    ));
    shareName.setCustomValidity(shareNameValid ? "" : t(
      "Use 1-80 letters, digits, dots, underscores, or hyphens."
    ));
    account.setCustomValidity(accountValid ? "" : t(
      "Use SERVER\\user or DOMAIN\\user format."
    ));
    if (!serverAddressValid) {
      serverAddress.reportValidity();
      throw new Error("The SMB connection address is invalid.");
    }
    if (!shareNameValid) {
      shareName.reportValidity();
      throw new Error("The SMB share name is invalid.");
    }
    if (!accountValid) {
      account.reportValidity();
      throw new Error("The SMB account must use SERVER\\user or DOMAIN\\user format.");
    }
    serverAddress.value = normalizedServerAddress;
    shareName.value = normalizedShareName;
    account.value = normalizedAccount;
    syncSmbPath();
    return {
      serverAddress: normalizedServerAddress,
      shareName: normalizedShareName,
      account: normalizedAccount,
    };
  }

  function setSmbActionStatus(message, error = false) {
    const status = document.querySelector("#smbActionStatus");
    status.textContent = t(message);
    status.classList.toggle("is-error", error);
  }

  async function runSmbAction(kind) {
    clearMessage();
    let values;
    try {
      values = validateSmbSettings();
    } catch (error) {
      setSmbActionStatus(error.message, true);
      return;
    }

    const testMode = kind === "test";
    const button = document.querySelector(testMode
      ? "#testSmbAccessButton"
      : "#configureSmbShareButton");
    button.disabled = true;
    setSmbActionStatus(testMode ? "Checking SMB access..." : "Configuring local SMB share...");
    try {
      const payload = {
        serverAddress: values.serverAddress,
        shareName: values.shareName,
        account: values.account,
      };
      if (testMode) {
        payload.password = document.querySelector("[data-winpe='SharePassword']").value;
      }
      const result = await apiFetch(
        testMode ? "/api/smb/test-access" : "/api/smb/configure-local-share",
        { method: "POST", body: JSON.stringify(payload) }
      );
      if (testMode) {
        const missing = Array.isArray(result.missingFolders) && result.missingFolders.length
          ? ` ${t("Missing folders")}: ${result.missingFolders.join(", ")}.`
          : "";
        setSmbActionStatus(`${t("SMB access verified.")}${missing}`);
      } else {
        const normalizedAccount = result.aclAccount && result.aclAccount !== values.account
          ? ` ${t("Permissions assigned to")}: ${result.aclAccount}.`
          : "";
        const actionMessage = result.status === "created"
          ? "Local SMB share created."
          : "Local SMB share permissions updated.";
        setSmbActionStatus(`${t(actionMessage)}${normalizedAccount}`);
      }
    } catch (error) {
      setSmbActionStatus(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function cleanExternalAddress(value) {
    let result = String(value || "").trim();
    if (!result) return "";
    if (/^https?:\/\//i.test(result)) {
      try {
        result = new URL(result).host;
      } catch {
        result = result.replace(/^https?:\/\//i, "");
      }
    }
    return result.split("/")[0].trim();
  }

  function hostForUrl(value) {
    const host = String(value || "").trim();
    if (!host || host.startsWith("[") || (host.match(/:/g) || []).length < 2) {
      return host;
    }
    return `[${host}]`;
  }

  function currentAccessMode() {
    return document.querySelector("#accessModeValue").value || "http_direct";
  }

  function syncDerivedEndpoint() {
    const mode = currentAccessMode();
    const bindHost = document.querySelector("[data-api='IRONAPI_BIND_HOST']").value.trim();
    const port = document.querySelector("[data-api='IRONAPI_PORT']").value.trim();
    const external = cleanExternalAddress(document.querySelector("#externalHost").value);
    const endpoint = mode === "https_proxy"
      ? `https://${external}`
      : `http://${hostForUrl(bindHost)}:${port}`;
    document.querySelector("#apiBaseUrl").value = endpoint;
    document.querySelector("#publicEndpoint").textContent = endpoint;
    document.querySelector("#internalEndpoint").textContent = `${bindHost}:${port}`;
    return endpoint;
  }

  function applyCertificateState() {
    const proxyMode = currentAccessMode() === "https_proxy";
    const validation = document.querySelector("#validateApiServerCertificate");
    const certificateType = document.querySelector("#apiServerCertificateType");
    const certificateFile = document.querySelector("#apiServerCertificateFile");
    const checkButton = document.querySelector("#checkCertificateButton");
    const fileName = document.querySelector("#certificateFileName");
    const hasSavedCertificate = Boolean(lastConfig?.secrets?.hasApiServerCertificate);
    const enabled = proxyMode && validation.checked;

    certificateType.disabled = !enabled;
    certificateFile.disabled = !enabled;
    certificateFile.required = enabled && !hasSavedCertificate;
    checkButton.disabled = !enabled;
    document.querySelector("#apiServerCertificateStatus").textContent = hasSavedCertificate
      ? t("Saved; choose a file to replace it")
      : t(enabled ? "Required when validation is enabled" : "Certificate validation is disabled");
    if (!certificateFile.files?.length) {
      fileName.textContent = t(hasSavedCertificate ? "Saved certificate" : "No file chosen");
    }
    document.querySelector("#certificateGateValue").textContent = t(validation.checked ? "YES" : "NO");
  }

  function renderTopology() {
    const proxyMode = currentAccessMode() === "https_proxy";
    const certificateGate = document.querySelector("#certificateGate");
    const certificateBlock = document.querySelector("#certificateBlock");
    const proxyLabel = document.querySelector("#proxyNode span");
    const regularRouteProtocol = document.querySelector("#regularRouteProtocol");
    const proxyIcon = document.querySelector("#proxyNode .proxy-icon");
    const directIcon = document.querySelector("#proxyNode .direct-icon");
    const topologyNote = document.querySelector("#topologyNote");

    certificateGate.hidden = !proxyMode;
    certificateBlock.hidden = !proxyMode;
    proxyIcon.toggleAttribute("hidden", !proxyMode);
    directIcon.toggleAttribute("hidden", proxyMode);
    regularRouteProtocol.textContent = proxyMode ? "HTTPS" : "HTTP";
    proxyLabel.textContent = t(proxyMode ? "HTTPS reverse proxy" : "Direct HTTP access");
    topologyNote.textContent = t(proxyMode
      ? "Certificate validation applies to WinPE and post-install."
      : "Clients connect directly over HTTP; certificate settings are hidden.");
    syncDerivedEndpoint();
  }

  function applyAccessMode({ fromUser = false } = {}) {
    const hiddenMode = document.querySelector("#accessModeValue");
    const bindHost = document.querySelector("[data-api='IRONAPI_BIND_HOST']");
    const externalField = document.querySelector("#externalHostField");
    const externalHost = document.querySelector("#externalHost");
    const validation = document.querySelector("#validateApiServerCertificate");
    const proxyMode = hiddenMode.value === "https_proxy";

    document.querySelectorAll("input[name='accessMode']").forEach((radio) => {
      radio.checked = radio.value === hiddenMode.value;
    });

    if (proxyMode) {
      if (!bindHost.disabled && bindHost.value !== "127.0.0.1") {
        bindHost.dataset.directValue = bindHost.value;
      }
      bindHost.value = "127.0.0.1";
      bindHost.disabled = true;
      externalField.hidden = false;
      externalHost.required = true;
      if (validation.dataset.stashed === "true") {
        validation.checked = validation.dataset.proxyValue === "true";
        delete validation.dataset.stashed;
      }
    } else {
      if (bindHost.disabled) {
        bindHost.value = bindHost.dataset.directValue || "0.0.0.0";
      }
      bindHost.disabled = false;
      externalField.hidden = true;
      externalHost.required = false;
      if (fromUser) {
        validation.dataset.proxyValue = String(validation.checked);
        validation.dataset.stashed = "true";
      }
      validation.checked = false;
    }

    applyCertificateState();
    renderTopology();
  }

  async function readCertificateBase64() {
    const file = document.querySelector("#apiServerCertificateFile")?.files?.[0];
    if (!file) return "";
    if (file.size > 64 * 1024) {
      throw new Error(t("The certificate file must not exceed 64 KiB."));
    }
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 8192) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
    }
    return btoa(binary);
  }

  function firstInvalidVisibleField() {
    return [...document.querySelectorAll("input, select")].find((field) => {
      const hiddenParent = field.closest("[hidden]");
      return !hiddenParent && !field.disabled && !field.checkValidity();
    });
  }

  function validateNetworkSettings() {
    const port = Number.parseInt(
      document.querySelector("[data-api='IRONAPI_PORT']").value,
      10
    );
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error("Internal TCP port must be from 1 to 65535.");
    }
    if (currentAccessMode() === "https_proxy") {
      const externalHost = document.querySelector("#externalHost");
      externalHost.value = cleanExternalAddress(externalHost.value);
      if (!externalHost.value) {
        externalHost.reportValidity();
        throw new Error("External address is required for HTTPS reverse proxy.");
      }
    }
    const endpoint = syncDerivedEndpoint();
    try {
      new URL(endpoint);
    } catch {
      throw new Error("The derived client endpoint is invalid.");
    }
    const invalid = firstInvalidVisibleField();
    if (invalid) {
      invalid.reportValidity();
      throw new Error("Complete the required fields before continuing.");
    }
    return endpoint;
  }

  async function checkCertificate() {
    clearMessage();
    const status = document.querySelector("#certificateCheckStatus");
    status.textContent = t("Checking certificate...");
    try {
      const certificateBase64 = await readCertificateBase64();
      const result = await apiFetch("/api/certificate/validate", {
        method: "POST",
        body: JSON.stringify({
          certificateBase64,
          certificateType: document.querySelector("#apiServerCertificateType").value,
        }),
      });
      const expiry = new Intl.DateTimeFormat(currentLanguage === "ru" ? "ru-RU" : "en-US", {
        dateStyle: "medium",
      }).format(new Date(result.notAfter));
      status.textContent = `${t("Certificate is valid until")} ${expiry.replace(/\.$/, "")}.`;
    } catch (error) {
      status.textContent = "";
      showMessage(error.message, "error");
    }
  }

  function checkConnectionSettings() {
    clearMessage();
    const status = document.querySelector("#networkCheckStatus");
    try {
      validateNetworkSettings();
      status.textContent = t("Connection settings are consistent.");
    } catch (error) {
      status.textContent = "";
      showMessage(error.message, "error");
    }
  }

  function setStatusText(id, value) {
    const element = document.querySelector(id);
    if (element) element.textContent = t(value);
  }

  function renderConfig(config) {
    lastConfig = config;
    populateTimeZones(config);
    document.querySelector("#rootPath").textContent = config.root;

    const apiStatus = config.files.apiEnvCreated
      ? "Created from example"
      : config.files.apiEnvExists ? "Configured" : "Missing";
    const winpeStatus = config.files.winpeConfigCreated
      ? "Created from example"
      : config.files.winpeConfigExists ? "Configured" : "Missing";
    setStatusText("#apiEnvStatus", apiStatus);
    setStatusText("#winpeStatus", winpeStatus);
    setStatusText("#passwordStatus", config.secrets.hasWinpeSharePassword
      ? "Saved, not displayed"
      : "Required");
    setStatusText("#superadminPasswordStatus", config.secrets.hasSuperadminPassword
      ? "Configured; leave blank to keep it"
      : "Required; at least 12 characters");
    setStatusText("#validationApiStatus", apiStatus);
    setStatusText("#validationWinpeStatus", winpeStatus);
    document.querySelector("#apiEnvPath").textContent = config.files.apiEnv;
    document.querySelector("#winpePath").textContent = config.files.winpeConfig;

    setField("[data-api]", config.api);
    setField("[data-winpe]", config.winpe);
    setField("[data-unattend]", config.unattend);
    setField("[data-auth]", config.auth);

    const apiUrl = String(config.winpe.ApiBaseUrl || "");
    if (apiUrl.toLowerCase().startsWith("https://")) {
      document.querySelector("#externalHost").value = cleanExternalAddress(apiUrl);
    }
    document.querySelector("#accessModeValue").value =
      config.api.IRONAPI_ACCESS_MODE || "http_direct";
    renderSmbSettings(config);
    renderOdjSettings(config);
    applyAccessMode();
    applyCertificateState();
    renderStoragePaths();
    renderComputerNamePreview();
  }

  async function load() {
    const session = await apiFetch("/api/session");
    csrfToken = session.csrfToken;
    document.querySelector("#rootPath").textContent = session.root;
    renderConfig(await apiFetch("/api/config"));
  }

  function markSectionComplete(section) {
    completedSections.add(section);
    document.querySelector(`[data-section='${section}']`)?.classList.add("is-complete");
  }

  async function saveConfiguration(sectionName = currentSection) {
    clearMessage();
    const saveAll = sectionName === "all";
    const section = saveAll ? document : document.querySelector(`#${sectionName}`);
    if (!section) throw new Error("The current settings page was not found.");

    if (saveAll || sectionName === "network") validateNetworkSettings();
    if (saveAll || sectionName === "winpe") validateSmbSettings();
    if (saveAll || sectionName === "additional") renderStoragePaths();

    const payload = { section: sectionName };
    for (const [group, selector] of Object.entries({
      api: "[data-api]",
      winpe: "[data-winpe]",
      unattend: "[data-unattend]",
      auth: "[data-auth]",
    })) {
      const values = collectFields(section, selector, group);
      if (
        !saveAll &&
        group === "auth" &&
        !String(values.username || "").trim() &&
        !String(values.password || "")
      ) continue;
      if (Object.keys(values).length) payload[group] = values;
    }

    if (saveAll || sectionName === "network") {
      const certificateBase64 = await readCertificateBase64();
      if (
        payload.winpe?.ValidateApiServerCertificate === true &&
        !certificateBase64 &&
        !lastConfig?.secrets?.hasApiServerCertificate
      ) {
        throw new Error(t("Select a certificate file before saving."));
      }
      payload.winpe.ApiServerCertificateBase64 = certificateBase64;
    }
    const result = await apiFetch("/api/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    renderConfig(result.config);
    document.querySelector("#apiServerCertificateFile").value = "";
    markSectionComplete(currentSection);
    const backupText = result.backups?.length
      ? ` ${t("Backups")}: ${result.backups.length}.`
      : "";
    showMessage(`${t("Configuration saved.")}${backupText}`);
    return result;
  }

  function activateSection(section) {
    if (!document.querySelector(`#${section}`)) return;
    currentSection = section;
    document.querySelectorAll(".step-item").forEach((item) => {
      const active = item.dataset.section === section;
      item.classList.toggle("is-active", active);
      if (active) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
    document.querySelectorAll(".view").forEach((view) => {
      view.classList.toggle("is-visible", view.id === section);
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function validateRepository() {
    clearMessage();
    const output = document.querySelector("#validationOutput");
    output.textContent = t("Running validation...");
    try {
      const result = await apiFetch("/api/validate", { method: "POST" });
      output.textContent = [
        `${t("Exit code")}: ${result.exitCode}`,
        "",
        result.stdout || "",
        result.stderr ? `\nSTDERR:\n${result.stderr}` : "",
      ].join("\n");
      showMessage(
        result.exitCode === 0 ? "Validation completed." : "Validation found issues.",
        result.exitCode === 0 ? "success" : "error"
      );
      if (result.exitCode === 0) markSectionComplete("validation");
    } catch (error) {
      output.textContent = error.message;
      showMessage(error.message, "error");
    }
  }

  async function finish() {
    clearMessage();
    await apiFetch("/api/finish", { method: "POST" });
    showMessage("SetupWeb session closed. You can close this tab.");
  }

  document.querySelectorAll(".step-item").forEach((button) => {
    button.addEventListener("click", () => activateSection(button.dataset.section));
  });

  document.querySelectorAll("[data-next]").forEach((button) => {
    button.addEventListener("click", () => activateSection(button.dataset.next));
  });

  document.querySelectorAll("[data-save-next]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await saveConfiguration();
        activateSection(button.dataset.saveNext);
      } catch (error) {
        showMessage(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-language]").forEach((button) => {
    button.addEventListener("click", () => setLanguage(button.dataset.language));
  });

  document.querySelectorAll("input[name='accessMode']").forEach((radio) => {
    radio.addEventListener("change", () => {
      document.querySelector("#accessModeValue").value = radio.value;
      applyAccessMode({ fromUser: true });
      document.querySelector("#networkCheckStatus").textContent = "";
    });
  });

  document.querySelectorAll(
    "[data-api='IRONAPI_BIND_HOST'], [data-api='IRONAPI_PORT'], #externalHost"
  ).forEach((input) => input.addEventListener("input", () => {
    syncDerivedEndpoint();
    document.querySelector("#networkCheckStatus").textContent = "";
  }));

  document.querySelector("#validateApiServerCertificate").addEventListener("change", () => {
    applyCertificateState();
    document.querySelector("#certificateCheckStatus").textContent = "";
  });
  document.querySelector("#apiServerCertificateFile").addEventListener("change", () => {
    const file = document.querySelector("#apiServerCertificateFile").files?.[0];
    document.querySelector("#certificateFileName").textContent = file?.name || t("No file chosen");
    document.querySelector("#certificateCheckStatus").textContent = "";
  });
  document.querySelector("#checkCertificateButton").addEventListener("click", checkCertificate);
  document.querySelector("#checkConnectionButton").addEventListener("click", checkConnectionSettings);
  document.querySelector("#testSmbAccessButton").addEventListener("click", () => runSmbAction("test"));
  document.querySelector("#configureSmbShareButton").addEventListener("click", () => runSmbAction("configure"));
  document.querySelector("#secureOdjFolderButton").addEventListener("click", secureOdjFolder);
  document.querySelector("#odjProcessAccount").addEventListener("input", (event) => {
    event.currentTarget.setCustomValidity("");
  });

  document.querySelector("#smbShareName").addEventListener("input", () => {
    document.querySelector("#smbShareName").setCustomValidity("");
    setSmbActionStatus("");
    syncSmbPath();
  });
  document.querySelector("#smbServerName").addEventListener("input", (event) => {
    event.currentTarget.setCustomValidity("");
    setSmbActionStatus("");
    syncSmbPath();
  });
  document.querySelector("[data-winpe='ShareUser']").addEventListener("input", (event) => {
    event.currentTarget.setCustomValidity("");
    setSmbActionStatus("");
  });
  document.querySelector("[data-winpe='SharePassword']").addEventListener("input", () => {
    setSmbActionStatus("");
  });

  document.querySelectorAll(
    "[data-api='IRONAPI_NAME_PREFIX'], [data-api='IRONAPI_NAME_WIDTH'], [data-api='IRONAPI_NAME_START']"
  ).forEach((input) => input.addEventListener("input", renderComputerNamePreview));
  document.querySelector("[data-winpe='ShareDrive']").addEventListener("input", renderStoragePaths);

  document.querySelector("#saveOnlyButton").addEventListener("click", () => {
    saveConfiguration("all").catch((error) => showMessage(error.message, "error"));
  });
  document.querySelector("#saveAdditionalButton").addEventListener("click", () => {
    saveConfiguration().catch((error) => showMessage(error.message, "error"));
  });
  document.querySelector("#validateButton").addEventListener("click", validateRepository);
  document.querySelector("#finishButton").addEventListener("click", () => {
    finish().catch((error) => showMessage(error.message, "error"));
  });
  document.querySelector("#finishValidationButton").addEventListener("click", async () => {
    try {
      await saveConfiguration("all");
      await finish();
    } catch (error) {
      showMessage(error.message, "error");
    }
  });

  applyLanguage();
  load().catch((error) => showMessage(error.message, "error"));
})();
