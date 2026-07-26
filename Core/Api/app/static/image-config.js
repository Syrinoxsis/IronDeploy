const elements = {
    form: document.querySelector("#image-form"),
    saveButton: document.querySelector("#save-button"),
    message: document.querySelector("#message-area"),
    localAdminName: document.querySelector("#localAdminName"),
    localAdminPassword: document.querySelector("#localAdminPassword"),
    enableBuiltInAdministrator: document.querySelector("#enableBuiltInAdministrator"),
    enableSetupLocalAdmin: document.querySelector("#enableSetupLocalAdmin"),
    enableGuiImageApplyProgress: document.querySelector(
        "#enableGuiImageApplyProgress"
    ),
    builtInAdministratorPassword: document.querySelector("#builtInAdministratorPassword"),
    timeZone: document.querySelector("#timeZone"),
    passwordStatus: document.querySelector("#password-status"),
    builtinPasswordStatus: document.querySelector("#builtin-password-status"),
    builtinPasswordWarning: document.querySelector("#builtin-password-warning"),
    filesNote: document.querySelector("#files-note"),
    buildWimButton: document.querySelector("#build-wim-button"),
    buildIsoButton: document.querySelector("#build-iso-button"),
    buildState: document.querySelector("#build-state"),
    buildLog: document.querySelector("#build-log"),
    settingsCategory: document.querySelector("#settings-category"),
    settingsPanels: [...document.querySelectorAll("[data-settings-panel]")],
    buildConfirmation: document.querySelector("#build-confirmation"),
    buildConfirmationMessage: document.querySelector("#build-confirmation-message"),
    confirmBuildButton: document.querySelector("#confirm-build-button"),
    cancelBuildButton: document.querySelector("#cancel-build-button"),
};

let pendingBuildTarget = null;

function showSettingsCategory(category) {
    for (const panel of elements.settingsPanels) {
        panel.hidden = panel.dataset.settingsPanel !== category;
    }
}

function showMessage(text, type = "success") {
    elements.message.hidden = false;
    elements.message.className = `notice ${type}`;
    elements.message.textContent = text;
}

function clearMessage() {
    elements.message.hidden = true;
    elements.message.textContent = "";
}

async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body && !headers.has("content-type")) {
        headers.set("content-type", "application/json");
    }
    if (options.method && options.method !== "GET") {
        // Custom header the server requires on writes; a cross-site form cannot
        // set it, so this blocks CSRF against the settings endpoint.
        headers.set("x-requested-with", "IronDeploy");
    }
    const response = await fetch(url, { ...options, headers });
    const text = await response.text();
    if (!response.ok) {
        let detail = text;
        try {
            detail = JSON.parse(text).detail || text;
        } catch {
            detail = text;
        }
        throw new Error(detail || `HTTP ${response.status}`);
    }
    return text ? JSON.parse(text) : {};
}

function renderTimeZones(timeZones, selected) {
    elements.timeZone.replaceChildren(
        ...timeZones.map((zone) => {
            const option = document.createElement("option");
            option.value = zone.id;
            option.textContent = `${zone.offset} - ${zone.id} (${zone.label})`;
            return option;
        })
    );
    if (selected) {
        elements.timeZone.value = selected;
    }
}

function updateBuiltinWarning() {
    // Warn (non-blocking) when the built-in admin is being enabled without a
    // password on record and none entered this session.
    const needsPassword =
        elements.enableBuiltInAdministrator.checked &&
        !elements.builtInAdministratorPassword.dataset.hasPassword &&
        !elements.builtInAdministratorPassword.value;
    elements.builtinPasswordWarning.hidden = !needsPassword;
}

function renderConfig(config) {
    renderTimeZones(config.timeZones || [], config.timeZone);
    elements.localAdminName.value = config.localAdminName || "";
    elements.enableBuiltInAdministrator.checked = Boolean(
        config.enableBuiltInAdministrator
    );
    elements.enableSetupLocalAdmin.checked = Boolean(config.enableSetupLocalAdmin);
    elements.enableGuiImageApplyProgress.checked = Boolean(
        config.enableGuiImageApplyProgress
    );
    elements.localAdminPassword.value = "";
    elements.passwordStatus.textContent = config.hasLocalAdminPassword
        ? "A password is saved. Leave blank to keep it, or enter a new one (min 8 characters)."
        : "No custom password set yet. Enter one (min 8 characters); stored as plain text in the unattend template.";
    elements.builtInAdministratorPassword.value = "";
    elements.builtInAdministratorPassword.dataset.hasPassword = config
        .hasBuiltInAdministratorPassword
        ? "1"
        : "";
    elements.builtinPasswordStatus.textContent = config.hasBuiltInAdministratorPassword
        ? "A password is saved. Leave blank to keep it, or enter a new one (min 8 characters)."
        : "No password set yet. Applied by Windows Setup when the built-in Administrator is enabled (min 8 characters).";
    updateBuiltinWarning();
    elements.filesNote.textContent = [
        `${config.files.winpeConfig}${config.files.winpeConfigExists ? "" : " (created on save)"}`,
        `${config.files.unattend}${config.files.unattendExists ? "" : " (created on save)"}`,
    ].join("  •  ");
}

async function load() {
    try {
        renderConfig(await apiFetch("/api/image-config"));
    } catch (error) {
        elements.saveButton.disabled = true;
        elements.form.querySelectorAll("input, select").forEach((field) => {
            field.disabled = true;
        });
        showMessage(error.message, "error");
    }
}

async function save() {
    clearMessage();
    elements.saveButton.disabled = true;
    try {
        const payload = {
            localAdminName: elements.localAdminName.value,
            localAdminPassword: elements.localAdminPassword.value,
            builtInAdministratorPassword: elements.builtInAdministratorPassword.value,
            enableBuiltInAdministrator: elements.enableBuiltInAdministrator.checked,
            enableSetupLocalAdmin: elements.enableSetupLocalAdmin.checked,
            enableGuiImageApplyProgress:
                elements.enableGuiImageApplyProgress.checked,
            timeZone: elements.timeZone.value,
        };
        const result = await apiFetch("/api/image-config", {
            method: "POST",
            body: JSON.stringify(payload),
        });
        renderConfig(result.config);
        const backupText = result.backups.length
            ? ` ${result.backups.length} backup${result.backups.length === 1 ? "" : "s"} written.`
            : "";
        showMessage(`Image settings saved.${backupText}`);
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        elements.saveButton.disabled = false;
    }
}

function renderBuildState(state) {
    const running = state.status === "running";
    elements.buildWimButton.disabled = running;
    elements.buildIsoButton.disabled = running;
    elements.buildState.className = `build-state ${state.status || "idle"}`;
    elements.buildState.textContent = state.message || "Build status unavailable.";
    elements.buildLog.hidden = !state.logPath;
    elements.buildLog.textContent = state.logPath ? `Log: ${state.logPath}` : "";
}

async function refreshBuildState() {
    try {
        renderBuildState(await apiFetch("/api/winpe-build"));
    } catch (error) {
        elements.buildState.className = "build-state failed";
        elements.buildState.textContent = error.message;
    }
}

async function startBuild(target) {
    elements.buildWimButton.disabled = true;
    elements.buildIsoButton.disabled = true;
    try {
        renderBuildState(await apiFetch(`/api/winpe-build/${target}`, {
            method: "POST",
        }));
    } catch (error) {
        elements.buildState.className = "build-state failed";
        elements.buildState.textContent = error.message;
        elements.buildWimButton.disabled = false;
        elements.buildIsoButton.disabled = false;
    }
}

function closeBuildConfirmation() {
    elements.buildConfirmation.hidden = true;
    pendingBuildTarget = null;
}

function requestBuild(target) {
    pendingBuildTarget = target;
    elements.buildConfirmationMessage.textContent =
        `The ${target.toUpperCase()} rebuild runs as an elevated background job.`;
    elements.buildConfirmation.hidden = false;
    elements.confirmBuildButton.focus();
}

elements.enableBuiltInAdministrator.addEventListener("change", updateBuiltinWarning);
elements.builtInAdministratorPassword.addEventListener("input", updateBuiltinWarning);
elements.settingsCategory.addEventListener("change", () => {
    showSettingsCategory(elements.settingsCategory.value);
});

elements.saveButton.addEventListener("click", () => {
    save();
});

elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    save();
});

elements.buildWimButton.addEventListener("click", () => requestBuild("wim"));
elements.buildIsoButton.addEventListener("click", () => requestBuild("iso"));
elements.confirmBuildButton.addEventListener("click", () => {
    const target = pendingBuildTarget;
    closeBuildConfirmation();
    if (target) startBuild(target);
});
elements.cancelBuildButton.addEventListener("click", closeBuildConfirmation);
elements.buildConfirmation.addEventListener("click", (event) => {
    if (event.target === elements.buildConfirmation) closeBuildConfirmation();
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.buildConfirmation.hidden) {
        closeBuildConfirmation();
    }
});

load();
showSettingsCategory(elements.settingsCategory.value);
refreshBuildState();
window.setInterval(refreshBuildState, 3000);
