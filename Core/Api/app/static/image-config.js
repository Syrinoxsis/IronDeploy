const elements = {
    form: document.querySelector("#image-form"),
    refreshButton: document.querySelector("#refresh-button"),
    saveButton: document.querySelector("#save-button"),
    dirtyState: document.querySelector("#dirty-state"),
    dirtyMessage: document.querySelector("#dirty-message"),
    message: document.querySelector("#message-area"),
    localAdminName: document.querySelector("#localAdminName"),
    localAdminPassword: document.querySelector("#localAdminPassword"),
    enableBuiltInAdministrator: document.querySelector("#enableBuiltInAdministrator"),
    enableSetupLocalAdmin: document.querySelector("#enableSetupLocalAdmin"),
    enableGuiImageApplyProgress: document.querySelector("#enableGuiImageApplyProgress"),
    imageApplyModes: [...document.querySelectorAll('input[name="imageApplyMode"]')],
    builtInAdministratorPassword: document.querySelector("#builtInAdministratorPassword"),
    timeZone: document.querySelector("#timeZone"),
    keyboardLayoutChoice: document.querySelector("#keyboardLayoutChoice"),
    addKeyboardLayout: document.querySelector("#addKeyboardLayout"),
    keyboardLayoutList: document.querySelector("#keyboardLayoutList"),
    systemLocale: document.querySelector("#systemLocale"),
    uiLanguage: document.querySelector("#uiLanguage"),
    userLocale: document.querySelector("#userLocale"),
    passwordStatus: document.querySelector("#password-status"),
    builtinPasswordStatus: document.querySelector("#builtin-password-status"),
    builtinPasswordWarning: document.querySelector("#builtin-password-warning"),
    filesNote: document.querySelector("#files-note"),
    buildWimButton: document.querySelector("#build-wim-button"),
    buildIsoButton: document.querySelector("#build-iso-button"),
    buildState: document.querySelector("#build-state"),
    buildLog: document.querySelector("#build-log"),
    lastWimBuild: document.querySelector("#last-wim-build"),
    lastIsoBuild: document.querySelector("#last-iso-build"),
    settingsLeaves: [...document.querySelectorAll("[data-settings-view]")],
    settingsViewPanels: [...document.querySelectorAll("[data-settings-view-panel]")],
    settingsGroupToggles: [
        ...document.querySelectorAll("[data-settings-group-toggle]"),
    ],
    buildConfirmation: document.querySelector("#build-confirmation"),
    buildConfirmationMessage: document.querySelector("#build-confirmation-message"),
    confirmBuildButton: document.querySelector("#confirm-build-button"),
    cancelBuildButton: document.querySelector("#cancel-build-button"),
};

let pendingBuildTarget = null;
let keyboardLayoutChoices = [];
let selectedKeyboardLayouts = [];
let isDirty = false;

function translated(text) {
    return window.IronI18n?.t(text) || text;
}

function localizedLocaleLabel(choice) {
    const language = window.IronI18n?.language;
    if (!language || language === "en" || typeof Intl.DisplayNames !== "function") {
        return translated(choice.label);
    }
    try {
        const label = new Intl.DisplayNames([language], { type: "language" }).of(choice.id);
        if (label) return label[0].toUpperCase() + label.slice(1);
    } catch {
        // Fall back to the label returned by the API.
    }
    return translated(choice.label);
}

function showSettingsView(view) {
    for (const leaf of elements.settingsLeaves) {
        const active = leaf.dataset.settingsView === view;
        leaf.classList.toggle("is-active", active);
        if (active) {
            leaf.setAttribute("aria-current", "page");
        } else {
            leaf.removeAttribute("aria-current");
        }
    }
    for (const panel of elements.settingsViewPanels) {
        panel.hidden = panel.dataset.settingsViewPanel !== view;
    }
}

function setDirty(dirty) {
    isDirty = dirty;
    elements.dirtyState.classList.toggle("is-dirty", dirty);
    elements.dirtyState.classList.toggle("is-clean", !dirty);
    elements.dirtyState.textContent = dirty ? "Unsaved changes" : "All changes saved";
    elements.dirtyMessage.textContent = dirty
        ? "Save before rebuilding the WinPE image."
        : "Settings match the saved configuration.";
    elements.saveButton.disabled = !dirty;
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
            const label = translated(zone.label);
            option.textContent = window.IronI18n?.language === "ru"
                ? `${zone.offset} — ${label} (${zone.id})`
                : `${zone.offset} - ${zone.id} (${label})`;
            return option;
        })
    );
    if (selected) elements.timeZone.value = selected;
}

function renderChoices(element, choices, selected) {
    element.replaceChildren(
        ...choices.map((choice) => {
            const option = document.createElement("option");
            option.value = choice.id;
            option.textContent = `${localizedLocaleLabel(choice)} (${choice.id})`;
            return option;
        })
    );
    if (selected) element.value = selected;
}

function keyboardLayoutLabel(id) {
    const choice = keyboardLayoutChoices.find((item) => item.id === id);
    return choice ? `${localizedLocaleLabel(choice)} (${choice.id})` : id;
}

function renderKeyboardLayouts() {
    const available = keyboardLayoutChoices.filter(
        (choice) => !selectedKeyboardLayouts.includes(choice.id)
    );
    renderChoices(elements.keyboardLayoutChoice, available);
    elements.keyboardLayoutChoice.disabled = available.length === 0;
    elements.addKeyboardLayout.disabled =
        available.length === 0 || selectedKeyboardLayouts.length >= 8;

    elements.keyboardLayoutList.replaceChildren(
        ...selectedKeyboardLayouts.map((layout, index) => {
            const row = document.createElement("div");
            row.className = "keyboard-layout-row";

            const name = document.createElement("span");
            name.className = "keyboard-layout-name";
            name.textContent = keyboardLayoutLabel(layout);
            row.append(name);

            if (index === 0) {
                const badge = document.createElement("span");
                badge.className = "keyboard-default-badge";
                badge.textContent = window.IronI18n?.t("Default") || "Default";
                row.append(badge);
            }

            const actions = document.createElement("div");
            actions.className = "keyboard-layout-actions";
            for (const [action, text, title, disabled] of [
                ["up", "↑", "Move up", index === 0],
                ["down", "↓", "Move down", index === selectedKeyboardLayouts.length - 1],
                ["remove", "×", "Remove", selectedKeyboardLayouts.length === 1],
            ]) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = `keyboard-action-button ${action}`;
                button.dataset.action = action;
                button.dataset.index = String(index);
                button.textContent = text;
                button.title = window.IronI18n?.t(title) || title;
                button.setAttribute("aria-label", button.title);
                button.disabled = disabled;
                actions.append(button);
            }
            row.append(actions);
            return row;
        })
    );
}

function setKeyboardLayouts(choices, value) {
    keyboardLayoutChoices = choices;
    selectedKeyboardLayouts = String(value || "")
        .split(";")
        .map((item) => item.trim())
        .filter((item) => keyboardLayoutChoices.some((choice) => choice.id === item));
    if (!selectedKeyboardLayouts.length && keyboardLayoutChoices.length) {
        selectedKeyboardLayouts = [keyboardLayoutChoices[0].id];
    }
    renderKeyboardLayouts();
}

function updateBuiltinWarning() {
    const needsPassword =
        elements.enableBuiltInAdministrator.checked &&
        !elements.builtInAdministratorPassword.dataset.hasPassword &&
        !elements.builtInAdministratorPassword.value;
    elements.builtinPasswordWarning.hidden = !needsPassword;
}

function renderConfig(config) {
    renderTimeZones(config.timeZones || [], config.timeZone);
    setKeyboardLayouts(config.keyboardLayouts || [], config.inputLocale);
    renderChoices(elements.systemLocale, config.locales || [], config.systemLocale);
    renderChoices(elements.uiLanguage, config.uiLanguages || [], config.uiLanguage);
    renderChoices(elements.userLocale, config.locales || [], config.userLocale);
    elements.localAdminName.value = config.localAdminName || "";
    elements.enableBuiltInAdministrator.checked = Boolean(
        config.enableBuiltInAdministrator
    );
    elements.enableSetupLocalAdmin.checked = Boolean(config.enableSetupLocalAdmin);
    elements.enableGuiImageApplyProgress.checked = Boolean(
        config.enableGuiImageApplyProgress
    );
    for (const choice of elements.imageApplyModes) {
        choice.checked = choice.value === (config.imageApplyMode || "direct");
    }
    elements.localAdminPassword.value = "";
    elements.passwordStatus.textContent = config.hasLocalAdminPassword
        ? "A password is saved. Leave blank to keep it or enter a new one."
        : "No password is saved. Enter at least 8 characters.";
    elements.builtInAdministratorPassword.value = "";
    elements.builtInAdministratorPassword.dataset.hasPassword = config
        .hasBuiltInAdministratorPassword
        ? "1"
        : "";
    elements.builtinPasswordStatus.textContent = config.hasBuiltInAdministratorPassword
        ? "A password is saved. Leave blank to keep it or enter a new one."
        : "No password is saved. Enter at least 8 characters.";
    updateBuiltinWarning();
    elements.filesNote.textContent = "";
    setDirty(false);
}

async function load() {
    try {
        const config = await apiFetch("/api/image-config");
        elements.form.querySelectorAll("input, select").forEach((field) => {
            field.disabled = false;
        });
        renderConfig(config);
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
            enableGuiImageApplyProgress: elements.enableGuiImageApplyProgress.checked,
            imageApplyMode:
                elements.imageApplyModes.find((choice) => choice.checked)?.value ||
                "direct",
            timeZone: elements.timeZone.value,
            inputLocale: selectedKeyboardLayouts.join(";"),
            systemLocale: elements.systemLocale.value,
            uiLanguage: elements.uiLanguage.value,
            userLocale: elements.userLocale.value,
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
        elements.saveButton.disabled = !isDirty;
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
    const locale = window.IronI18n?.language === "ru" ? "ru-RU" : "en-US";
    const formatStartedAt = (value) => {
        if (!value) return translated("Never");
        const date = new Date(value);
        return Number.isNaN(date.getTime())
            ? translated("Unknown")
            : date.toLocaleString(locale);
    };
    elements.lastWimBuild.textContent = formatStartedAt(state.lastStartedAt?.wim);
    elements.lastIsoBuild.textContent = formatStartedAt(state.lastStartedAt?.iso);
}

async function refreshBuildState() {
    try {
        renderBuildState(await apiFetch("/api/winpe-build"));
    } catch (error) {
        elements.buildState.className = "build-state failed";
        elements.buildState.textContent = error.message;
    }
}

async function refreshAll() {
    clearMessage();
    elements.refreshButton.disabled = true;
    elements.refreshButton.classList.add("is-loading");
    try {
        await Promise.all([load(), refreshBuildState()]);
    } finally {
        elements.refreshButton.disabled = false;
        elements.refreshButton.classList.remove("is-loading");
    }
}

async function startBuild(target) {
    elements.buildWimButton.disabled = true;
    elements.buildIsoButton.disabled = true;
    try {
        renderBuildState(
            await apiFetch(`/api/winpe-build/${target}`, { method: "POST" })
        );
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

function markFormDirty(event) {
    if (event.target !== elements.keyboardLayoutChoice) setDirty(true);
}

elements.form.addEventListener("input", markFormDirty);
elements.form.addEventListener("change", markFormDirty);
elements.enableBuiltInAdministrator.addEventListener("change", updateBuiltinWarning);
elements.builtInAdministratorPassword.addEventListener("input", updateBuiltinWarning);

elements.addKeyboardLayout.addEventListener("click", () => {
    const layout = elements.keyboardLayoutChoice.value;
    if (
        layout &&
        selectedKeyboardLayouts.length < 8 &&
        !selectedKeyboardLayouts.includes(layout)
    ) {
        selectedKeyboardLayouts.push(layout);
        renderKeyboardLayouts();
        setDirty(true);
    }
});

elements.keyboardLayoutList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const index = Number(button.dataset.index);
    if (!Number.isInteger(index) || !selectedKeyboardLayouts[index]) return;

    let changed = false;
    if (button.dataset.action === "remove" && selectedKeyboardLayouts.length > 1) {
        selectedKeyboardLayouts.splice(index, 1);
        changed = true;
    } else if (button.dataset.action === "up" && index > 0) {
        [selectedKeyboardLayouts[index - 1], selectedKeyboardLayouts[index]] =
            [selectedKeyboardLayouts[index], selectedKeyboardLayouts[index - 1]];
        changed = true;
    } else if (
        button.dataset.action === "down" &&
        index < selectedKeyboardLayouts.length - 1
    ) {
        [selectedKeyboardLayouts[index + 1], selectedKeyboardLayouts[index]] =
            [selectedKeyboardLayouts[index], selectedKeyboardLayouts[index + 1]];
        changed = true;
    }
    renderKeyboardLayouts();
    if (changed) setDirty(true);
});

for (const leaf of elements.settingsLeaves) {
    leaf.addEventListener("click", () => showSettingsView(leaf.dataset.settingsView));
}

for (const toggle of elements.settingsGroupToggles) {
    toggle.addEventListener("click", () => {
        const group = toggle.closest(".settings-nav-group");
        const collapsed = group.classList.toggle("is-collapsed");
        toggle.setAttribute("aria-expanded", String(!collapsed));
    });
}

elements.refreshButton.addEventListener("click", () => {
    if (isDirty && !window.confirm("Discard unsaved changes?")) return;
    refreshAll();
});

elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (isDirty) save();
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

showSettingsView("setup-account");
setDirty(false);
refreshAll();
window.setInterval(refreshBuildState, 3000);
