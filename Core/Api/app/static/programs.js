const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    fileInput: document.querySelector("#program-file"),
    programNameInput: document.querySelector("#program-name"),
    argumentsInput: document.querySelector("#program-arguments"),
    uploadButton: document.querySelector("#upload-button"),
    progressRow: document.querySelector("#upload-progress-row"),
    progress: document.querySelector("#upload-progress"),
    progressText: document.querySelector("#upload-progress-text"),
    cancelUploadButton: document.querySelector("#cancel-upload-button"),
    programList: document.querySelector("#program-list"),
    emptyState: document.querySelector("#empty-state"),
    programCount: document.querySelector("#program-count"),
    programsDirectory: document.querySelector("#programs-directory"),
};

let activeUploadRequest = null;
let messageTimer = null;

function showMessage(text, type = "success") {
    if (messageTimer) window.clearTimeout(messageTimer);
    elements.message.hidden = false;
    elements.message.className = `notice ${type}`;
    elements.message.textContent = text;
    if (type === "success") {
        messageTimer = window.setTimeout(clearMessage, 4500);
    }
}

function clearMessage() {
    if (messageTimer) window.clearTimeout(messageTimer);
    messageTimer = null;
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
            // Use the response body as-is.
        }
        throw new Error(detail || `HTTP ${response.status}`);
    }
    return text ? JSON.parse(text) : {};
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** unit)).toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function validateArguments(value) {
    if (/[\0\r\n]/.test(value)) {
        throw new Error("Launch arguments must not contain NUL, CR, or LF characters.");
    }
    const normalized = value.replace(/^ +| +$/g, "");
    if (!normalized) return "";
    if (normalized.length > 500) {
        throw new Error("Launch arguments are limited to 500 characters.");
    }
    return normalized;
}

function normalizeProgramName(value, sourceName, useSourceWhenBlank = false) {
    let normalized = value;
    if (!normalized && useSourceWhenBlank) normalized = sourceName;
    if (
        !normalized ||
        normalized !== normalized.trim() ||
        normalized === "." ||
        normalized === ".." ||
        normalized.endsWith(".") ||
        /[<>:"/\\|?*\x00-\x1f]/.test(normalized)
    ) {
        throw new Error("Enter a valid program file name.");
    }

    const baseName = normalized.split(".", 1)[0].replace(/[ .]+$/, "").toUpperCase();
    if (
        ["CON", "PRN", "AUX", "NUL"].includes(baseName) ||
        /^(COM|LPT)[1-9]$/.test(baseName)
    ) {
        throw new Error("This is a reserved Windows file name.");
    }

    const sourceExtension = sourceName.match(/\.(exe|msi)$/i)?.[0].toLowerCase();
    if (!sourceExtension) {
        throw new Error("The source program must be an .exe or .msi file.");
    }
    const requestedExtension = normalized.match(/\.([^.]+)$/)?.[0].toLowerCase();
    if (!requestedExtension) {
        throw new Error("Program names must include the .exe or .msi extension.");
    }
    if (!/\.(exe|msi)$/i.test(requestedExtension)) {
        throw new Error("Program names must end with .exe or .msi.");
    }
    if (!normalized.toLowerCase().endsWith(sourceExtension)) {
        throw new Error(`The file extension must remain ${sourceExtension}.`);
    }
    return normalized;
}

function createProgramCard(program) {
    const card = document.createElement("article");
    card.className = `image-card availability-card${program.enabled ? "" : " is-disabled"}`;

    const top = document.createElement("div");
    top.className = "image-card-top";
    const identity = document.createElement("div");
    const titleRow = document.createElement("div");
    titleRow.className = "image-title-row";
    const title = document.createElement("h3");
    title.textContent = program.name;
    const format = document.createElement("span");
    format.className = `format-badge ${program.type.toLowerCase()}`;
    format.textContent = program.type;
    titleRow.append(title, format);
    const details = document.createElement("p");
    details.textContent = `${formatBytes(program.size)} · Updated ${new Date(program.modifiedAt).toLocaleString()}`;
    identity.append(titleRow, details);
    top.append(identity);

    const actions = document.createElement("div");
    actions.className = "image-actions";
    const availability = document.createElement("label");
    availability.className = "availability-toggle";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.role = "switch";
    enabled.checked = Boolean(program.enabled);
    enabled.setAttribute("aria-label", `Offer ${program.name} in WinPE`);
    const track = document.createElement("span");
    track.className = "availability-track";
    track.setAttribute("aria-hidden", "true");
    const state = document.createElement("span");
    state.className = "availability-state";
    function updateAvailability() {
        state.textContent = program.enabled ? "Enabled" : "Disabled";
        card.classList.toggle("is-disabled", !program.enabled);
    }
    enabled.addEventListener("change", async () => {
        const nextEnabled = enabled.checked;
        enabled.disabled = true;
        try {
            const result = await apiFetch(
                `/api/programs/${encodeURIComponent(program.name)}/enabled`,
                {
                    method: "POST",
                    body: JSON.stringify({ enabled: nextEnabled }),
                }
            );
            program.enabled = Boolean(result.program.enabled);
            enabled.checked = program.enabled;
            updateAvailability();
            showMessage(`${program.name} ${program.enabled ? "enabled" : "disabled"} for WinPE.`);
        } catch (error) {
            enabled.checked = Boolean(program.enabled);
            showMessage(error.message, "error");
        } finally {
            enabled.disabled = false;
        }
    });
    availability.append(enabled, track, state);
    updateAvailability();
    const rename = document.createElement("button");
    rename.className = "secondary-button compact-button";
    rename.type = "button";
    rename.textContent = "Rename";
    const remove = document.createElement("button");
    remove.className = "danger-button";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => deleteProgram(program.name, remove));
    actions.append(availability, rename, remove);
    top.append(actions);
    card.append(top);

    rename.addEventListener("click", () => {
        if (actions.dataset.renaming === "true") return;
        actions.dataset.renaming = "true";
        const originalActions = Array.from(actions.children);
        const input = document.createElement("input");
        input.type = "text";
        input.className = "program-name-edit-input";
        input.maxLength = 255;
        input.spellcheck = false;
        input.value = program.name;
        const saveName = document.createElement("button");
        saveName.className = "primary-button compact-button";
        saveName.type = "button";
        saveName.textContent = "Save";
        const cancelRename = document.createElement("button");
        cancelRename.className = "quiet-button compact-button";
        cancelRename.type = "button";
        cancelRename.textContent = "Cancel";

        function restoreRename() {
            input.replaceWith(title);
            actions.replaceChildren(...originalActions);
            delete actions.dataset.renaming;
        }

        async function submitRename() {
            let newName;
            try {
                newName = normalizeProgramName(input.value, program.name);
            } catch (error) {
                showMessage(error.message, "error");
                input.focus();
                return;
            }
            if (newName === program.name) {
                restoreRename();
                return;
            }

            clearMessage();
            input.disabled = true;
            saveName.disabled = true;
            cancelRename.disabled = true;
            try {
                const result = await apiFetch(
                    `/api/programs/${encodeURIComponent(program.name)}/rename`,
                    {
                        method: "POST",
                        body: JSON.stringify({ newName }),
                    }
                );
                showMessage(`${result.oldName} renamed to ${result.name}.`);
                await refreshPrograms();
            } catch (error) {
                showMessage(error.message, "error");
                input.disabled = false;
                saveName.disabled = false;
                cancelRename.disabled = false;
                input.focus();
                input.select();
            }
        }

        title.replaceWith(input);
        actions.replaceChildren(saveName, cancelRename);
        saveName.addEventListener("click", submitRename);
        cancelRename.addEventListener("click", restoreRename);
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") submitRename();
            if (event.key === "Escape") restoreRename();
        });
        input.focus();
        input.select();
    });

    const argumentsSection = document.createElement("div");
    argumentsSection.className = "program-arguments";

    const summary = document.createElement("div");
    summary.className = "program-arguments-summary";
    const summaryCopy = document.createElement("div");
    summaryCopy.className = "program-arguments-copy";
    const summaryLabel = document.createElement("span");
    summaryLabel.className = "program-arguments-label";
    summaryLabel.textContent = "Launch arguments";
    const summaryValue = document.createElement("code");
    summaryValue.className = "program-arguments-value";
    summaryCopy.append(summaryLabel, summaryValue);

    const summaryActions = document.createElement("div");
    summaryActions.className = "program-arguments-actions";
    const edit = document.createElement("button");
    edit.className = "argument-action-button";
    edit.type = "button";
    summaryActions.append(edit);
    summary.append(summaryCopy, summaryActions);

    const editor = document.createElement("div");
    editor.className = "program-argument-editor";
    editor.hidden = true;
    const inputs = document.createElement("div");
    inputs.className = "program-argument-inputs";
    const inputRow = document.createElement("div");
    inputRow.className = "program-argument-input-row";
    const argumentInput = document.createElement("input");
    argumentInput.type = "text";
    argumentInput.className = "arguments-input";
    argumentInput.maxLength = 500;
    argumentInput.spellcheck = false;
    argumentInput.placeholder = "e.g. /qn /norestart ALLUSERS=1";
    inputRow.append(argumentInput);
    inputs.append(inputRow);
    const editorActions = document.createElement("div");
    editorActions.className = "program-argument-editor-actions";
    const save = document.createElement("button");
    save.className = "secondary-button compact-button";
    save.type = "button";
    save.textContent = "Save";
    const cancel = document.createElement("button");
    cancel.className = "quiet-button compact-button";
    cancel.type = "button";
    cancel.textContent = "Cancel";
    editorActions.append(save, cancel);
    editor.append(inputs, editorActions);

    function updateArgumentSummary() {
        const value = program.arguments || "";
        summaryValue.textContent = value || "No arguments";
        summaryValue.classList.toggle("is-empty", !value);
        edit.textContent = value ? "Edit" : "+ Add arguments";
    }

    function openArgumentEditor() {
        summary.hidden = true;
        editor.hidden = false;
        argumentInput.value = program.arguments || "";
        argumentInput.focus();
        argumentInput.select();
    }

    argumentInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            save.click();
        }
        if (event.key === "Escape") cancel.click();
    });
    edit.addEventListener("click", openArgumentEditor);
    cancel.addEventListener("click", () => {
        editor.hidden = true;
        summary.hidden = false;
    });
    save.addEventListener("click", async () => {
        clearMessage();
        let normalized;
        try {
            normalized = validateArguments(argumentInput.value);
        } catch (error) {
            showMessage(error.message, "error");
            argumentInput.focus();
            return;
        }
        argumentInput.disabled = true;
        cancel.disabled = true;
        save.disabled = true;
        try {
            const result = await apiFetch(
                `/api/programs/${encodeURIComponent(program.name)}/arguments`,
                {
                    method: "POST",
                    body: JSON.stringify({ arguments: normalized }),
                }
            );
            program.arguments = result.arguments || "";
            updateArgumentSummary();
            editor.hidden = true;
            summary.hidden = false;
            showMessage(`Launch arguments for ${program.name} saved.`);
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            argumentInput.disabled = false;
            cancel.disabled = false;
            save.disabled = false;
        }
    });

    updateArgumentSummary();
    argumentsSection.append(summary, editor);
    card.append(argumentsSection);
    return card;
}

async function deleteProgram(name, button) {
    clearMessage();
    if (!window.confirm(`Delete ${name} from Share\\Programs?`)) return;
    button.disabled = true;
    try {
        await apiFetch(`/api/programs/${encodeURIComponent(name)}`, {
            method: "DELETE",
        });
        showMessage(`${name} deleted.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, "error");
        button.disabled = false;
    }
}

function renderPrograms(payload) {
    elements.programsDirectory.textContent = payload.directory || "";
    const programs = payload.programs || [];
    elements.programCount.textContent = `${programs.length} program${programs.length === 1 ? "" : "s"}`;
    elements.emptyState.hidden = programs.length !== 0;
    elements.programList.replaceChildren(...programs.map(createProgramCard));
}

async function refreshPrograms() {
    elements.refreshButton.disabled = true;
    try {
        renderPrograms(await apiFetch("/api/programs"));
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        elements.refreshButton.disabled = false;
    }
}

function uploadFile(file, programName, argumentsValue) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeUploadRequest = request;
        const finish = () => {
            if (activeUploadRequest === request) activeUploadRequest = null;
        };
        request.open("POST", "/api/programs/upload");
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader("x-irondeploy-filename", encodeURIComponent(programName));
        request.setRequestHeader("x-irondeploy-arguments", encodeURIComponent(argumentsValue));
        request.setRequestHeader("content-type", "application/octet-stream");
        request.upload.addEventListener("progress", (event) => {
            if (!event.lengthComputable) return;
            const percent = Math.round((event.loaded / event.total) * 100);
            elements.progress.value = percent;
            elements.progressText.textContent = `${percent}% · ${formatBytes(event.loaded)} of ${formatBytes(event.total)}`;
        });
        request.addEventListener("load", () => {
            let payload = {};
            try { payload = JSON.parse(request.responseText || "{}"); } catch { /* ignored */ }
            finish();
            if (request.status >= 200 && request.status < 300) resolve(payload);
            else reject(new Error(payload.detail || request.responseText || `HTTP ${request.status}`));
        });
        request.addEventListener("error", () => {
            finish();
            reject(new Error("Upload connection failed."));
        });
        request.addEventListener("abort", () => {
            finish();
            reject(new Error("Upload cancelled."));
        });
        request.send(file);
    });
}

async function beginUpload() {
    clearMessage();
    const file = elements.fileInput.files[0];
    if (!file) {
        showMessage("Choose an .exe or .msi installer first.", "error");
        return;
    }
    if (!/\.(exe|msi)$/i.test(file.name)) {
        showMessage("Only .exe and .msi files are accepted.", "error");
        return;
    }
    let programName;
    try {
        programName = normalizeProgramName(
            elements.programNameInput.value,
            file.name,
            true
        );
    } catch (error) {
        showMessage(error.message, "error");
        elements.programNameInput.focus();
        return;
    }
    let argumentsValue;
    try {
        argumentsValue = validateArguments(elements.argumentsInput.value);
    } catch (error) {
        showMessage(error.message, "error");
        elements.argumentsInput.focus();
        return;
    }

    elements.uploadButton.disabled = true;
    elements.fileInput.disabled = true;
    elements.programNameInput.disabled = true;
    elements.argumentsInput.disabled = true;
    elements.progressRow.hidden = false;
    elements.cancelUploadButton.hidden = false;
    elements.progress.value = 0;
    elements.progressText.textContent = "Starting upload...";
    try {
        const result = await uploadFile(file, programName, argumentsValue);
        elements.progress.value = 100;
        elements.progressText.textContent = `Uploaded ${formatBytes(result.size)}.`;
        elements.fileInput.value = "";
        elements.programNameInput.value = "";
        elements.argumentsInput.value = "";
        showMessage(`${result.name} uploaded successfully.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, error.message === "Upload cancelled." ? "success" : "error");
    } finally {
        elements.uploadButton.disabled = false;
        elements.fileInput.disabled = false;
        elements.programNameInput.disabled = false;
        elements.argumentsInput.disabled = false;
        elements.cancelUploadButton.hidden = true;
    }
}

elements.refreshButton.addEventListener("click", () => {
    clearMessage();
    refreshPrograms();
});
elements.uploadButton.addEventListener("click", beginUpload);
elements.fileInput.addEventListener("change", () => {
    const file = elements.fileInput.files[0];
    elements.programNameInput.placeholder = file
        ? `File name (optional; default: ${file.name})`
        : "File name (optional)";
});
elements.cancelUploadButton.addEventListener("click", () => {
    if (activeUploadRequest) activeUploadRequest.abort();
});

refreshPrograms();
