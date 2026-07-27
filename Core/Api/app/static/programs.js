const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    fileInput: document.querySelector("#program-file"),
    programNameInput: document.querySelector("#program-name"),
    argumentsInput: document.querySelector("#program-arguments"),
    msiPropertiesInput: document.querySelector("#program-msi-properties"),
    msiPropertiesField: document.querySelector("#program-msi-properties-field"),
    msiPropertiesHelp: document.querySelector("#program-msi-properties-help"),
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

const MAX_ARGUMENT_COUNT = 100;
const MAX_ARGUMENT_LENGTH = 512;
const MAX_ARGUMENTS_LENGTH = 4096;
const MAX_MSI_PROPERTY_COUNT = 100;
const MAX_MSI_PROPERTY_NAME_LENGTH = 72;
const MAX_MSI_PROPERTY_VALUE_LENGTH = 512;
const MAX_MSI_PROPERTIES_LENGTH = 4096;
const MSI_PROPERTY_NAME = /^[A-Z_][A-Z0-9_.]*$/;

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

function validateCommandLineValue(value, label, maximumLength, allowEmpty = false) {
    if (typeof value !== "string") {
        throw new Error(`${label} must be a string.`);
    }
    if (!allowEmpty && !value) {
        throw new Error(`${label} must not be empty.`);
    }
    if (value.length > maximumLength) {
        throw new Error(`${label} is limited to ${maximumLength} characters.`);
    }
    if (/[\s\u0000"']/.test(value)) {
        throw new Error(`${label} must not contain whitespace, NUL, or quotes.`);
    }
    return value;
}

function asArgumentArray(value) {
    if (Array.isArray(value)) return value;
    if (typeof value === "string") {
        return value.trim() ? value.trim().split(/\s+/) : [];
    }
    return [];
}

function validateArguments(values, programType) {
    if (!Array.isArray(values)) {
        throw new Error("Launch arguments must be an array.");
    }
    const normalized = values.filter((value) => value !== "");
    if (normalized.length > MAX_ARGUMENT_COUNT) {
        throw new Error(`Launch arguments are limited to ${MAX_ARGUMENT_COUNT} items.`);
    }
    normalized.forEach((value, index) => {
        validateCommandLineValue(
            value,
            `Launch argument ${index + 1}`,
            MAX_ARGUMENT_LENGTH
        );
        if (programType === "MSI" && value.includes("=")) {
            throw new Error(
                "MSI arguments must not contain '='. Add NAME=VALUE items " +
                "to the MSI properties section."
            );
        }
    });
    const totalLength = normalized.join(" ").length;
    if (totalLength > MAX_ARGUMENTS_LENGTH) {
        throw new Error(
            `Launch arguments are limited to ${MAX_ARGUMENTS_LENGTH} characters in total.`
        );
    }
    return normalized;
}

function validateMsiProperties(entries, programType) {
    if (!Array.isArray(entries)) {
        throw new Error("MSI properties must be an object.");
    }
    if (entries.some(([name, value]) => name === "" && value !== "")) {
        throw new Error("MSI property values require a property name.");
    }
    const populated = entries.filter(([name]) => name !== "");
    if (populated.length > MAX_MSI_PROPERTY_COUNT) {
        throw new Error(`MSI properties are limited to ${MAX_MSI_PROPERTY_COUNT} items.`);
    }
    if (programType !== "MSI" && populated.length) {
        throw new Error("MSI properties are only valid for MSI programs.");
    }
    const normalized = {};
    for (const [rawName, rawValue] of populated) {
        const name = rawName.toUpperCase();
        if (name.length > MAX_MSI_PROPERTY_NAME_LENGTH || !MSI_PROPERTY_NAME.test(name)) {
            throw new Error(
                `Invalid MSI property name '${rawName}'. Names must match ` +
                "^[A-Z_][A-Z0-9_.]*$."
            );
        }
        if (Object.prototype.hasOwnProperty.call(normalized, name)) {
            throw new Error(`Duplicate MSI property name: ${name}.`);
        }
        normalized[name] = validateCommandLineValue(
            rawValue,
            `MSI property ${name} value`,
            MAX_MSI_PROPERTY_VALUE_LENGTH,
            true
        );
    }
    const totalLength = Object.entries(normalized)
        .map(([name, value]) => `${name}=${value}`)
        .join(" ").length;
    if (totalLength > MAX_MSI_PROPERTIES_LENGTH) {
        throw new Error(
            `MSI properties are limited to ${MAX_MSI_PROPERTIES_LENGTH} characters in total.`
        );
    }
    return normalized;
}

function parseMsiPropertyLines(value) {
    if (!value.trim()) return [];
    return value.split(/\r?\n/).filter((line) => line !== "").map((line) => {
        const separator = line.indexOf("=");
        if (separator < 0) {
            throw new Error(`MSI property '${line}' must use NAME=VALUE format.`);
        }
        return [line.slice(0, separator), line.slice(separator + 1)];
    });
}

function updateUploadInstallerType(file) {
    const isMsi = Boolean(file && /\.msi$/i.test(file.name));
    elements.msiPropertiesField.classList.toggle("is-unavailable", !isMsi);
    elements.msiPropertiesInput.disabled = !isMsi;
    elements.msiPropertiesHelp.textContent = isMsi
        ? "Enter one NAME=VALUE property per line."
        : "Select an MSI installer to enable this field.";
    if (!isMsi) elements.msiPropertiesInput.value = "";
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
    card.className = "image-card";

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
    const rename = document.createElement("button");
    rename.className = "secondary-button compact-button";
    rename.type = "button";
    rename.textContent = "Rename";
    const remove = document.createElement("button");
    remove.className = "danger-button";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => deleteProgram(program.name, remove));
    actions.append(rename, remove);
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
    summaryLabel.textContent = "Installer configuration";
    const summaryValue = document.createElement("code");
    summaryValue.className = "program-arguments-value";
    summaryCopy.append(summaryLabel, summaryValue);

    const summaryActions = document.createElement("div");
    summaryActions.className = "program-arguments-actions";
    const edit = document.createElement("button");
    edit.className = "argument-action-button";
    edit.type = "button";
    edit.textContent = "Edit configuration";
    const addArgument = document.createElement("button");
    addArgument.className = "argument-action-button";
    addArgument.type = "button";
    addArgument.textContent = "+ Add argument";
    summaryActions.append(edit, addArgument);
    const addPropertyFromSummary = document.createElement("button");
    addPropertyFromSummary.className = "argument-action-button";
    addPropertyFromSummary.type = "button";
    addPropertyFromSummary.textContent = "+ Add MSI property";
    if (program.type === "MSI") {
        summaryActions.append(addPropertyFromSummary);
    }
    summary.append(summaryCopy, summaryActions);

    const editor = document.createElement("div");
    editor.className = "program-argument-editor";
    editor.hidden = true;

    const argumentsGroup = document.createElement("div");
    argumentsGroup.className = "program-config-group";
    const argumentsHeader = document.createElement("div");
    argumentsHeader.className = "program-config-group-header";
    const argumentsLabel = document.createElement("strong");
    argumentsLabel.textContent = "Launch arguments (one item per row)";
    const addAnother = document.createElement("button");
    addAnother.className = "argument-action-button";
    addAnother.type = "button";
    addAnother.textContent = "+ Add argument";
    argumentsHeader.append(argumentsLabel, addAnother);
    const inputs = document.createElement("div");
    inputs.className = "program-argument-inputs";
    argumentsGroup.append(argumentsHeader, inputs);

    const propertiesGroup = document.createElement("div");
    propertiesGroup.className = "program-config-group program-msi-properties";
    propertiesGroup.hidden = program.type !== "MSI";
    const propertiesHeader = document.createElement("div");
    propertiesHeader.className = "program-config-group-header";
    const propertiesLabel = document.createElement("strong");
    propertiesLabel.textContent = "MSI properties";
    const propertyInputs = document.createElement("div");
    propertyInputs.className = "program-argument-inputs";
    const addProperty = document.createElement("button");
    addProperty.className = "argument-action-button";
    addProperty.type = "button";
    addProperty.textContent = "+ Add MSI property";
    propertiesHeader.append(propertiesLabel, addProperty);
    propertiesGroup.append(propertiesHeader, propertyInputs);

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
    editor.append(argumentsGroup, propertiesGroup, editorActions);

    function updateArgumentSummary() {
        const argumentsValues = asArgumentArray(program.arguments);
        const properties = program.msi_properties || {};
        const values = [
            ...argumentsValues,
            ...Object.entries(properties).map(([name, value]) => `${name}=${value}`),
        ];
        summaryValue.textContent = values.join(" ") || "No arguments or properties";
        summaryValue.classList.toggle("is-empty", values.length === 0);
    }

    function addArgumentInput(value = "", focus = true) {
        const row = document.createElement("div");
        row.className = "program-argument-input-row";
        const input = document.createElement("input");
        input.type = "text";
        input.className = "arguments-input";
        input.maxLength = MAX_ARGUMENT_LENGTH;
        input.spellcheck = false;
        input.placeholder = program.type === "MSI" ? "e.g. /qn" : "e.g. /S or install";
        input.value = value;
        const removeInput = document.createElement("button");
        removeInput.className = "argument-remove-button";
        removeInput.type = "button";
        removeInput.title = "Remove this argument row";
        removeInput.setAttribute("aria-label", "Remove this argument row");
        removeInput.textContent = "×";
        removeInput.addEventListener("click", () => row.remove());
        input.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                save.click();
            }
        });
        row.append(input, removeInput);
        inputs.append(row);
        if (focus) input.focus();
    }

    function addPropertyInput(name = "", value = "", focus = true) {
        const row = document.createElement("div");
        row.className = "program-argument-input-row msi-property-input-row";
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.className = "arguments-input msi-property-name-input";
        nameInput.maxLength = MAX_MSI_PROPERTY_NAME_LENGTH;
        nameInput.spellcheck = false;
        nameInput.placeholder = "NAME";
        nameInput.value = name;
        const valueInput = document.createElement("input");
        valueInput.type = "text";
        valueInput.className = "arguments-input";
        valueInput.maxLength = MAX_MSI_PROPERTY_VALUE_LENGTH;
        valueInput.spellcheck = false;
        valueInput.placeholder = "VALUE";
        valueInput.value = value;
        const removeInput = document.createElement("button");
        removeInput.className = "argument-remove-button";
        removeInput.type = "button";
        removeInput.title = "Remove this MSI property";
        removeInput.setAttribute("aria-label", "Remove this MSI property");
        removeInput.textContent = "×";
        removeInput.addEventListener("click", () => row.remove());
        row.append(nameInput, valueInput, removeInput);
        propertyInputs.append(row);
        if (focus) nameInput.focus();
    }

    function openConfigurationEditor({
        addArgumentRow = false,
        addPropertyRow = false,
    } = {}) {
        summary.hidden = true;
        editor.hidden = false;
        inputs.replaceChildren();
        propertyInputs.replaceChildren();
        const argumentsValues = asArgumentArray(program.arguments);
        argumentsValues.forEach((value) => addArgumentInput(value, false));
        Object.entries(program.msi_properties || {}).forEach(
            ([name, value]) => addPropertyInput(name, value, false)
        );
        if (addArgumentRow) {
            addArgumentInput();
        } else if (addPropertyRow) {
            addPropertyInput();
        }
    }

    edit.addEventListener("click", () => openConfigurationEditor());
    addArgument.addEventListener("click", () => openConfigurationEditor({
        addArgumentRow: true,
    }));
    addPropertyFromSummary.addEventListener("click", () => {
        openConfigurationEditor({ addPropertyRow: true });
    });
    addAnother.addEventListener("click", () => addArgumentInput());
    addProperty.addEventListener("click", () => addPropertyInput());
    cancel.addEventListener("click", () => {
        editor.hidden = true;
        summary.hidden = false;
    });
    save.addEventListener("click", async () => {
        clearMessage();
        let normalizedArguments;
        let normalizedProperties;
        try {
            const values = Array.from(inputs.querySelectorAll("input"))
                .map((input) => input.value);
            normalizedArguments = validateArguments(values, program.type);
            const properties = Array.from(
                propertyInputs.querySelectorAll(".msi-property-input-row")
            ).map((row) => {
                const rowInputs = row.querySelectorAll("input");
                return [rowInputs[0].value, rowInputs[1].value];
            });
            normalizedProperties = validateMsiProperties(properties, program.type);
        } catch (error) {
            showMessage(error.message, "error");
            inputs.querySelector("input")?.focus();
            return;
        }
        editor.querySelectorAll("input, button").forEach((control) => {
            control.disabled = true;
        });
        try {
            const result = await apiFetch(
                `/api/programs/${encodeURIComponent(program.name)}/arguments`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        arguments: normalizedArguments,
                        msi_properties: normalizedProperties,
                    }),
                }
            );
            program.arguments = result.arguments || [];
            program.msi_properties = result.msi_properties || {};
            updateArgumentSummary();
            editor.hidden = true;
            summary.hidden = false;
            showMessage(`Installer configuration for ${program.name} saved.`);
        } catch (error) {
            showMessage(error.message, "error");
        } finally {
            editor.querySelectorAll("input, button").forEach((control) => {
                control.disabled = false;
            });
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

function uploadFile(file, programName, argumentsValue, msiProperties) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeUploadRequest = request;
        const finish = () => {
            if (activeUploadRequest === request) activeUploadRequest = null;
        };
        request.open("POST", "/api/programs/upload");
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader("x-irondeploy-filename", encodeURIComponent(programName));
        request.setRequestHeader(
            "x-irondeploy-arguments",
            encodeURIComponent(JSON.stringify(argumentsValue))
        );
        request.setRequestHeader(
            "x-irondeploy-msi-properties",
            encodeURIComponent(JSON.stringify(msiProperties))
        );
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
    const programType = /\.msi$/i.test(programName) ? "MSI" : "EXE";
    let argumentsValue;
    let msiProperties;
    try {
        const argumentLines = elements.argumentsInput.value
            ? elements.argumentsInput.value.split(/\r?\n/)
            : [];
        argumentsValue = validateArguments(argumentLines, programType);
        msiProperties = validateMsiProperties(
            programType === "MSI"
                ? parseMsiPropertyLines(elements.msiPropertiesInput.value)
                : [],
            programType
        );
    } catch (error) {
        showMessage(error.message, "error");
        const errorInput = error.message.startsWith("MSI")
            ? elements.msiPropertiesInput
            : elements.argumentsInput;
        errorInput.focus();
        return;
    }

    elements.uploadButton.disabled = true;
    elements.fileInput.disabled = true;
    elements.programNameInput.disabled = true;
    elements.argumentsInput.disabled = true;
    elements.msiPropertiesInput.disabled = true;
    elements.progressRow.hidden = false;
    elements.cancelUploadButton.hidden = false;
    elements.progress.value = 0;
    elements.progressText.textContent = "Starting upload...";
    try {
        const result = await uploadFile(
            file,
            programName,
            argumentsValue,
            msiProperties
        );
        elements.progress.value = 100;
        elements.progressText.textContent = `Uploaded ${formatBytes(result.size)}.`;
        elements.fileInput.value = "";
        elements.programNameInput.value = "";
        elements.argumentsInput.value = "";
        elements.msiPropertiesInput.value = "";
        updateUploadInstallerType(null);
        showMessage(`${result.name} uploaded successfully.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, error.message === "Upload cancelled." ? "success" : "error");
    } finally {
        elements.uploadButton.disabled = false;
        elements.fileInput.disabled = false;
        elements.programNameInput.disabled = false;
        elements.argumentsInput.disabled = false;
        updateUploadInstallerType(elements.fileInput.files[0]);
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
    updateUploadInstallerType(file);
});
elements.cancelUploadButton.addEventListener("click", () => {
    if (activeUploadRequest) activeUploadRequest.abort();
});

refreshPrograms();
