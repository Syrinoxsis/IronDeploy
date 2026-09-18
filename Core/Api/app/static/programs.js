const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    installerMode: document.querySelector("#installer-mode"),
    folderMode: document.querySelector("#folder-mode"),
    installerInput: document.querySelector("#installer-input"),
    folderInput: document.querySelector("#folder-input"),
    dropZone: document.querySelector("#package-drop-zone"),
    packageName: document.querySelector("#package-name"),
    arguments: document.querySelector("#package-arguments"),
    uploadButton: document.querySelector("#upload-button"),
    selectedPackage: document.querySelector("#selected-package"),
    selectionTitle: document.querySelector("#selection-title"),
    selectionSummary: document.querySelector("#selection-summary"),
    entrypoint: document.querySelector("#entrypoint-select"),
    selectedFiles: document.querySelector("#selected-files"),
    clearSelection: document.querySelector("#clear-selection"),
    progressRow: document.querySelector("#upload-progress-row"),
    progress: document.querySelector("#upload-progress"),
    progressText: document.querySelector("#upload-progress-text"),
    cancelUpload: document.querySelector("#cancel-upload-button"),
    programList: document.querySelector("#program-list"),
    emptyState: document.querySelector("#empty-state"),
    programCount: document.querySelector("#program-count"),
    programsDirectory: document.querySelector("#programs-directory"),
    search: document.querySelector("#package-search"),
};

const MAX_PACKAGE_SIZE = 5 * 1024 ** 3;
const MAX_PACKAGE_FILES = 10000;
let sourceMode = "installer";
let selectedItems = [];
let currentPrograms = [];
let activeUpload = null;
let messageTimer = null;

function showMessage(text, type = "success") {
    if (messageTimer) window.clearTimeout(messageTimer);
    elements.message.hidden = false;
    elements.message.className = `notice ${type}`;
    elements.message.textContent = text;
    if (type === "success") messageTimer = window.setTimeout(clearMessage, 4500);
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
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = {}; }
    if (!response.ok) throw new Error(payload.detail || text || `HTTP ${response.status}`);
    return payload;
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const digits = unit < 2 ? 0 : 1;
    return `${(bytes / (1024 ** unit)).toFixed(digits)} ${units[unit]}`;
}

function formatDate(value) {
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "Unknown" : date.toLocaleString();
}

function validateArguments(value) {
    if (/[\0\r\n]/.test(value)) {
        throw new Error("Launch arguments must not contain NUL, CR, or LF characters.");
    }
    const normalized = value.replace(/^ +| +$/g, "");
    if (normalized.length > 500) {
        throw new Error("Launch arguments are limited to 500 characters.");
    }
    return normalized;
}

function validatePackageName(value) {
    if (
        !value || value !== value.trim() || value === "." || value === ".." ||
        value.endsWith(".") || /[<>:"/\\|?*\x00-\x1f]/.test(value)
    ) {
        throw new Error("Enter a valid package name.");
    }
    const base = value.split(".", 1)[0].replace(/[ .]+$/, "").toUpperCase();
    if (["CON", "PRN", "AUX", "NUL"].includes(base) || /^(COM|LPT)[1-9]$/.test(base)) {
        throw new Error("This is a reserved Windows name.");
    }
    return value;
}

function normalizeRelativePath(value) {
    return value.replaceAll("/", "\\");
}

function packageSize(items = selectedItems) {
    return items.reduce((total, item) => total + item.file.size, 0);
}

function setSourceMode(mode, openPicker = false) {
    sourceMode = mode;
    elements.installerMode.classList.toggle("is-selected", mode === "installer");
    elements.folderMode.classList.toggle("is-selected", mode === "folder");
    elements.installerMode.setAttribute("aria-pressed", String(mode === "installer"));
    elements.folderMode.setAttribute("aria-pressed", String(mode === "folder"));
    if (openPicker) {
        if (mode === "installer") elements.installerInput.click();
        else elements.folderInput.click();
    }
}

function clearSelection() {
    selectedItems = [];
    elements.installerInput.value = "";
    elements.folderInput.value = "";
    elements.selectedPackage.hidden = true;
    elements.selectedFiles.replaceChildren();
    elements.entrypoint.replaceChildren();
    elements.uploadButton.disabled = true;
}

function ensurePackageLimits(items) {
    if (!items.length) throw new Error("The selected package is empty.");
    if (items.length > MAX_PACKAGE_FILES) {
        throw new Error(`Packages are limited to ${MAX_PACKAGE_FILES.toLocaleString()} files.`);
    }
    if (packageSize(items) > MAX_PACKAGE_SIZE) {
        throw new Error("Program packages are limited to 5 GiB.");
    }
    const unique = new Set();
    for (const item of items) {
        const path = normalizeRelativePath(item.path);
        if (!path || path.startsWith("\\") || path.includes("..\\")) {
            throw new Error(`Invalid relative file path: ${path || "(empty)"}`);
        }
        const key = path.toLocaleLowerCase();
        if (unique.has(key)) throw new Error(`Duplicate file path: ${path}`);
        unique.add(key);
        item.path = path;
    }
}

function renderSelection(defaultName = "") {
    ensurePackageLimits(selectedItems);
    const entrypoints = selectedItems.filter(item => /\.(exe|msi)$/i.test(item.path));
    if (!entrypoints.length) throw new Error("The package must contain an EXE or MSI installer.");
    entrypoints.sort((left, right) => {
        const setupScore = path => /(^|\\)(setup|install|installer)\.(exe|msi)$/i.test(path) ? 0 : 1;
        return setupScore(left.path) - setupScore(right.path) || left.path.localeCompare(right.path);
    });

    elements.entrypoint.replaceChildren(...entrypoints.map(item => {
        const option = document.createElement("option");
        option.value = item.path;
        option.textContent = item.path;
        return option;
    }));
    const total = packageSize();
    elements.selectionTitle.textContent = sourceMode === "folder" ? "Selected folder" : "Selected installer";
    elements.selectionSummary.textContent = `${selectedItems.length.toLocaleString()} file${selectedItems.length === 1 ? "" : "s"} · ${formatBytes(total)}`;
    if (!elements.packageName.value && defaultName) elements.packageName.value = defaultName;

    const preview = selectedItems.slice(0, 8).map(item => {
        const row = document.createElement("div");
        row.className = "selected-file-row";
        const name = document.createElement("span");
        name.textContent = item.path;
        const size = document.createElement("span");
        size.textContent = formatBytes(item.file.size);
        row.append(name, size);
        return row;
    });
    if (selectedItems.length > 8) {
        const more = document.createElement("div");
        more.className = "selected-file-more";
        more.textContent = `+ ${selectedItems.length - 8} more files`;
        preview.push(more);
    }
    elements.selectedFiles.replaceChildren(...preview);
    elements.selectedPackage.hidden = false;
    elements.uploadButton.disabled = false;
}

function selectInstaller(file) {
    if (!file || !/\.(exe|msi)$/i.test(file.name)) {
        throw new Error("Choose an EXE or MSI installer.");
    }
    sourceMode = "installer";
    setSourceMode("installer");
    selectedItems = [{ file, path: file.name }];
    renderSelection(file.name.replace(/\.(exe|msi)$/i, ""));
}

function selectFolder(files) {
    const sourceFiles = Array.from(files || []);
    if (!sourceFiles.length) throw new Error("Choose a folder containing files.");
    const firstPath = sourceFiles[0].webkitRelativePath || sourceFiles[0].name;
    const root = firstPath.split("/")[0];
    selectedItems = sourceFiles.map(file => {
        const fullPath = file.webkitRelativePath || file.name;
        const parts = fullPath.split("/");
        const relative = parts.length > 1 ? parts.slice(1).join("/") : fullPath;
        return { file, path: normalizeRelativePath(relative) };
    });
    sourceMode = "folder";
    setSourceMode("folder");
    renderSelection(root);
}

function fileFromEntry(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
}

function readEntries(reader) {
    return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walkDroppedEntry(entry, prefix = "") {
    if (entry.isFile) {
        const file = await fileFromEntry(entry);
        return [{ file, path: `${prefix}${file.name}` }];
    }
    if (!entry.isDirectory) return [];
    const reader = entry.createReader();
    const children = [];
    for (;;) {
        const batch = await readEntries(reader);
        if (!batch.length) break;
        children.push(...batch);
    }
    const nested = [];
    for (const child of children) {
        nested.push(...await walkDroppedEntry(child, `${prefix}${entry.name}\\`));
    }
    return nested;
}

async function selectDrop(dataTransfer) {
    const entries = Array.from(dataTransfer.items || [])
        .map(item => item.webkitGetAsEntry?.())
        .filter(Boolean);
    if (entries.some(entry => entry.isDirectory)) {
        const collected = [];
        for (const entry of entries) collected.push(...await walkDroppedEntry(entry));
        const top = entries.find(entry => entry.isDirectory)?.name || "Package";
        selectedItems = collected.map(item => {
            const prefix = `${top}\\`;
            return {
                file: item.file,
                path: item.path.startsWith(prefix) ? item.path.slice(prefix.length) : item.path,
            };
        });
        sourceMode = "folder";
        setSourceMode("folder");
        renderSelection(top);
        return;
    }
    const files = Array.from(dataTransfer.files || []);
    if (files.length !== 1) throw new Error("Drop one installer or one folder.");
    selectInstaller(files[0]);
}

function uploadBinary(url, file, relativePath, onProgress) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        if (activeUpload) activeUpload.request = request;
        request.open("POST", url);
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader("x-irondeploy-relative-path", encodeURIComponent(relativePath));
        request.setRequestHeader("content-type", "application/octet-stream");
        request.upload.addEventListener("progress", event => onProgress(event.loaded));
        request.addEventListener("load", () => {
            let payload = {};
            try { payload = JSON.parse(request.responseText || "{}"); } catch { /* ignored */ }
            if (request.status >= 200 && request.status < 300) resolve(payload);
            else reject(new Error(payload.detail || request.responseText || `HTTP ${request.status}`));
        });
        request.addEventListener("error", () => reject(new Error("Upload connection failed.")));
        request.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
        request.send(file);
    });
}

function setUploadLocked(locked) {
    for (const element of [
        elements.installerMode, elements.folderMode, elements.installerInput,
        elements.folderInput, elements.packageName, elements.arguments,
        elements.entrypoint, elements.clearSelection,
    ]) element.disabled = locked;
    elements.uploadButton.disabled = locked || selectedItems.length === 0;
    elements.progressRow.hidden = !locked;
}

async function beginUpload() {
    clearMessage();
    if (!selectedItems.length) {
        showMessage("Choose an installer or folder first.", "error");
        return;
    }
    let packageName;
    let argumentsValue;
    try {
        packageName = validatePackageName(elements.packageName.value);
        argumentsValue = validateArguments(elements.arguments.value);
        ensurePackageLimits(selectedItems);
    } catch (error) {
        showMessage(error.message, "error");
        return;
    }

    activeUpload = { uploadId: null, request: null, cancelled: false };
    setUploadLocked(true);
    elements.progress.value = 0;
    elements.progressText.textContent = "Preparing package...";
    let uploadedBytes = 0;
    const totalBytes = packageSize() || 1;
    try {
        const started = await apiFetch("/api/programs/uploads", {
            method: "POST",
            body: JSON.stringify({
                name: packageName,
                entrypoint: elements.entrypoint.value,
                arguments: argumentsValue,
                files: selectedItems.map(item => ({ path: item.path, size: item.file.size })),
            }),
        });
        activeUpload.uploadId = started.uploadId;
        for (let index = 0; index < selectedItems.length; index += 1) {
            if (activeUpload.cancelled) throw new Error("Upload cancelled.");
            const item = selectedItems[index];
            elements.progressText.textContent = `Uploading ${index + 1} of ${selectedItems.length}: ${item.path}`;
            await uploadBinary(
                `/api/programs/uploads/${encodeURIComponent(started.uploadId)}/files`,
                item.file,
                item.path,
                loaded => {
                    const percent = Math.min(99, Math.round(((uploadedBytes + loaded) / totalBytes) * 100));
                    elements.progress.value = percent;
                }
            );
            uploadedBytes += item.file.size;
        }
        elements.progressText.textContent = "Publishing package...";
        await apiFetch(`/api/programs/uploads/${encodeURIComponent(started.uploadId)}/finalize`, {
            method: "POST",
        });
        elements.progress.value = 100;
        showMessage(`${packageName} uploaded as a software package.`);
        clearSelection();
        elements.packageName.value = "";
        elements.arguments.value = "";
        await refreshPrograms();
    } catch (error) {
        if (activeUpload?.uploadId) {
            try {
                await apiFetch(`/api/programs/uploads/${encodeURIComponent(activeUpload.uploadId)}`, {
                    method: "DELETE",
                });
            } catch { /* best-effort cleanup */ }
        }
        showMessage(error.message, error.message === "Upload cancelled." ? "success" : "error");
    } finally {
        activeUpload = null;
        setUploadLocked(false);
    }
}

function makeButton(label, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    button.addEventListener("click", handler);
    return button;
}

function createAvailability(program, card) {
    const label = document.createElement("label");
    label.className = "availability-toggle";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.role = "switch";
    input.checked = Boolean(program.enabled);
    input.setAttribute("aria-label", `Offer ${program.name} in WinPE`);
    const track = document.createElement("span");
    track.className = "availability-track";
    const state = document.createElement("span");
    state.className = "availability-state";
    const update = () => {
        state.textContent = program.enabled ? "Enabled" : "Disabled";
        card.classList.toggle("is-disabled", !program.enabled);
    };
    input.addEventListener("change", async () => {
        input.disabled = true;
        try {
            const result = await apiFetch(`/api/programs/${encodeURIComponent(program.name)}/enabled`, {
                method: "POST", body: JSON.stringify({ enabled: input.checked }),
            });
            program.enabled = Boolean(result.program.enabled);
            showMessage(`${program.name} ${program.enabled ? "enabled" : "disabled"} for WinPE.`);
        } catch (error) {
            input.checked = program.enabled;
            showMessage(error.message, "error");
        } finally {
            input.checked = program.enabled;
            input.disabled = false;
            update();
        }
    });
    label.append(input, track, state);
    update();
    return label;
}

async function renamePackage(program) {
    const value = window.prompt("New package name", program.name);
    if (value === null || value === program.name) return;
    try {
        const newName = validatePackageName(value);
        const result = await apiFetch(`/api/programs/${encodeURIComponent(program.name)}/rename`, {
            method: "POST", body: JSON.stringify({ newName }),
        });
        showMessage(`${result.oldName} renamed to ${result.name}.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

async function deletePackage(program, button) {
    if (!window.confirm(`Delete the ${program.name} package and all of its files?`)) return;
    button.disabled = true;
    try {
        await apiFetch(`/api/programs/${encodeURIComponent(program.name)}`, { method: "DELETE" });
        showMessage(`${program.name} deleted.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, "error");
        button.disabled = false;
    }
}

async function saveArguments(program, value) {
    const normalized = validateArguments(value);
    const result = await apiFetch(`/api/programs/${encodeURIComponent(program.name)}/arguments`, {
        method: "POST", body: JSON.stringify({ arguments: normalized }),
    });
    program.arguments = result.arguments || "";
    showMessage(`Launch arguments for ${program.name} saved.`);
}

async function setEntrypoint(program, value) {
    const result = await apiFetch(`/api/programs/${encodeURIComponent(program.name)}/entrypoint`, {
        method: "POST", body: JSON.stringify({ entrypoint: value }),
    });
    program.entrypoint = result.entrypoint;
    program.type = result.type;
    showMessage(`${program.name} will run ${result.entrypoint}.`);
    await refreshPrograms();
}

async function addFiles(program, files, button) {
    const additions = Array.from(files || []);
    if (!additions.length) return;
    button.disabled = true;
    try {
        for (const file of additions) {
            const relativePath = normalizeRelativePath(file.webkitRelativePath || file.name);
            await uploadBinary(
                `/api/programs/${encodeURIComponent(program.name)}/files`,
                file,
                relativePath,
                () => {}
            );
        }
        showMessage(`${additions.length} file${additions.length === 1 ? "" : "s"} added to ${program.name}.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        button.disabled = false;
    }
}

async function deletePackageFile(program, file, button) {
    if (!window.confirm(`Delete ${file.path} from ${program.name}?`)) return;
    button.disabled = true;
    try {
        await apiFetch(
            `/api/programs/${encodeURIComponent(program.name)}/files?path=${encodeURIComponent(file.path)}`,
            { method: "DELETE" }
        );
        showMessage(`${file.path} deleted from ${program.name}.`);
        await refreshPrograms();
    } catch (error) {
        showMessage(error.message, "error");
        button.disabled = false;
    }
}

function createPackageCard(program) {
    const card = document.createElement("article");
    card.className = "package-card";

    const overview = document.createElement("div");
    overview.className = "package-overview";
    const expand = makeButton("", "package-expand", () => {
        const open = details.hidden;
        details.hidden = !open;
        expand.classList.toggle("is-open", open);
        expand.setAttribute("aria-expanded", String(open));
        filesButton.textContent = open ? "Hide files" : "Files";
    });
    expand.setAttribute("aria-label", `Show files in ${program.name}`);
    expand.setAttribute("aria-expanded", "false");

    const identity = document.createElement("div");
    identity.className = "package-identity";
    const titleLine = document.createElement("div");
    titleLine.className = "package-name-line";
    const title = document.createElement("h3");
    title.textContent = program.name;
    const badge = document.createElement("span");
    badge.className = `format-badge ${program.type.toLowerCase()}`;
    badge.textContent = program.type;
    titleLine.append(title, badge);
    const metadata = document.createElement("p");
    metadata.textContent = `${formatBytes(program.size)} · ${program.fileCount.toLocaleString()} file${program.fileCount === 1 ? "" : "s"} · Updated ${formatDate(program.modifiedAt)}`;
    identity.append(titleLine, metadata);

    const facts = document.createElement("div");
    facts.className = "package-facts";
    const entrypointFact = document.createElement("div");
    entrypointFact.innerHTML = "<span>Entrypoint</span>";
    const entrypointCode = document.createElement("code");
    entrypointCode.textContent = program.entrypoint;
    entrypointFact.append(entrypointCode);
    const argumentsFact = document.createElement("div");
    argumentsFact.innerHTML = "<span>Arguments</span>";
    const argumentsCode = document.createElement("code");
    argumentsCode.textContent = program.arguments || "None";
    argumentsCode.classList.toggle("is-empty", !program.arguments);
    argumentsFact.append(argumentsCode);
    facts.append(entrypointFact, argumentsFact);

    const actions = document.createElement("div");
    actions.className = "package-actions";
    const filesButton = makeButton("Files", "secondary-button compact-button", () => expand.click());
    const rename = makeButton("Rename", "secondary-button compact-button", () => renamePackage(program));
    const remove = makeButton("Delete", "danger-button compact-button", () => deletePackage(program, remove));
    actions.append(createAvailability(program, card), filesButton, rename, remove);
    overview.append(expand, identity, facts, actions);

    const details = document.createElement("div");
    details.className = "package-details";
    details.hidden = true;
    const settings = document.createElement("div");
    settings.className = "package-settings";
    const entrypointLabel = document.createElement("label");
    entrypointLabel.innerHTML = "<span>Installer to run</span>";
    const entrypointSelect = document.createElement("select");
    const candidates = program.files.filter(file => /\.(exe|msi)$/i.test(file.path));
    entrypointSelect.replaceChildren(...candidates.map(file => {
        const option = document.createElement("option");
        option.value = file.path;
        option.textContent = file.path;
        option.selected = file.path.toLocaleLowerCase() === program.entrypoint.toLocaleLowerCase();
        return option;
    }));
    entrypointSelect.addEventListener("change", async () => {
        entrypointSelect.disabled = true;
        try { await setEntrypoint(program, entrypointSelect.value); }
        catch (error) { showMessage(error.message, "error"); entrypointSelect.disabled = false; }
    });
    entrypointLabel.append(entrypointSelect);
    const argumentsLabel = document.createElement("label");
    argumentsLabel.innerHTML = "<span>Launch arguments</span>";
    const argumentsInput = document.createElement("input");
    argumentsInput.type = "text";
    argumentsInput.maxLength = 500;
    argumentsInput.value = program.arguments || "";
    argumentsInput.placeholder = "No arguments";
    const saveArgs = makeButton("Save arguments", "secondary-button compact-button", async () => {
        saveArgs.disabled = true;
        try {
            await saveArguments(program, argumentsInput.value);
            argumentsCode.textContent = program.arguments || "None";
            argumentsCode.classList.toggle("is-empty", !program.arguments);
        } catch (error) { showMessage(error.message, "error"); }
        finally { saveArgs.disabled = false; }
    });
    argumentsLabel.append(argumentsInput);
    settings.append(entrypointLabel, argumentsLabel, saveArgs);

    const fileToolbar = document.createElement("div");
    fileToolbar.className = "package-file-toolbar";
    const fileTitle = document.createElement("strong");
    fileTitle.textContent = `Files (${program.fileCount.toLocaleString()})`;
    const addInput = document.createElement("input");
    addInput.type = "file";
    addInput.multiple = true;
    addInput.hidden = true;
    const addButton = makeButton("+ Add files", "secondary-button compact-button", () => addInput.click());
    const addFolderInput = document.createElement("input");
    addFolderInput.type = "file";
    addFolderInput.multiple = true;
    addFolderInput.hidden = true;
    addFolderInput.setAttribute("webkitdirectory", "");
    addFolderInput.setAttribute("directory", "");
    const addFolderButton = makeButton("+ Add folder", "secondary-button compact-button", () => addFolderInput.click());
    addInput.addEventListener("change", async () => {
        await addFiles(program, addInput.files, addButton);
        addInput.value = "";
    });
    addFolderInput.addEventListener("change", async () => {
        await addFiles(program, addFolderInput.files, addFolderButton);
        addFolderInput.value = "";
    });
    const addActions = document.createElement("div");
    addActions.className = "package-file-add-actions";
    addActions.append(addButton, addFolderButton, addInput, addFolderInput);
    fileToolbar.append(fileTitle, addActions);

    const fileList = document.createElement("div");
    fileList.className = "package-files";
    for (const file of program.files) {
        const row = document.createElement("div");
        row.className = "package-file-row";
        const path = document.createElement("span");
        path.className = "package-file-path";
        path.textContent = file.path;
        if (file.path.toLocaleLowerCase() === program.entrypoint.toLocaleLowerCase()) {
            const marker = document.createElement("em");
            marker.textContent = "entrypoint";
            path.append(marker);
        }
        const size = document.createElement("span");
        size.textContent = formatBytes(file.size);
        const updated = document.createElement("span");
        updated.textContent = formatDate(file.modifiedAt);
        const fileActions = document.createElement("span");
        if (file.path.toLocaleLowerCase() !== program.entrypoint.toLocaleLowerCase()) {
            const deleteFile = makeButton("Remove", "file-remove-button", () => deletePackageFile(program, file, deleteFile));
            fileActions.append(deleteFile);
        }
        row.append(path, size, updated, fileActions);
        fileList.append(row);
    }
    details.append(settings, fileToolbar, fileList);
    card.append(overview, details);
    return card;
}

function renderPrograms() {
    const query = elements.search.value.trim().toLocaleLowerCase();
    const programs = currentPrograms.filter(program => (
        !query || program.name.toLocaleLowerCase().includes(query) ||
        program.entrypoint.toLocaleLowerCase().includes(query)
    ));
    elements.programCount.textContent = currentPrograms.length.toLocaleString();
    elements.emptyState.hidden = programs.length !== 0;
    elements.emptyState.textContent = currentPrograms.length
        ? "No packages match your search."
        : "No software packages yet. Add an installer or a folder above.";
    elements.programList.replaceChildren(...programs.map(createPackageCard));
}

async function refreshPrograms() {
    elements.refreshButton.disabled = true;
    try {
        const payload = await apiFetch("/api/programs");
        currentPrograms = payload.programs || [];
        elements.programsDirectory.textContent = payload.directory || "";
        renderPrograms();
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        elements.refreshButton.disabled = false;
    }
}

elements.installerMode.addEventListener("click", () => setSourceMode("installer", true));
elements.folderMode.addEventListener("click", () => setSourceMode("folder", true));
elements.installerInput.addEventListener("change", () => {
    try { selectInstaller(elements.installerInput.files[0]); }
    catch (error) { clearSelection(); showMessage(error.message, "error"); }
});
elements.folderInput.addEventListener("change", () => {
    try { selectFolder(elements.folderInput.files); }
    catch (error) { clearSelection(); showMessage(error.message, "error"); }
});
elements.dropZone.addEventListener("click", () => {
    if (sourceMode === "folder") elements.folderInput.click();
    else elements.installerInput.click();
});
elements.dropZone.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        elements.dropZone.click();
    }
});
for (const eventName of ["dragenter", "dragover"]) {
    elements.dropZone.addEventListener(eventName, event => {
        event.preventDefault();
        elements.dropZone.classList.add("is-dragging");
    });
}
for (const eventName of ["dragleave", "drop"]) {
    elements.dropZone.addEventListener(eventName, event => {
        event.preventDefault();
        elements.dropZone.classList.remove("is-dragging");
    });
}
elements.dropZone.addEventListener("drop", async event => {
    try { await selectDrop(event.dataTransfer); }
    catch (error) { clearSelection(); showMessage(error.message, "error"); }
});
elements.clearSelection.addEventListener("click", clearSelection);
elements.uploadButton.addEventListener("click", beginUpload);
elements.cancelUpload.addEventListener("click", () => {
    if (!activeUpload) return;
    activeUpload.cancelled = true;
    activeUpload.request?.abort();
    elements.progressText.textContent = "Cancelling upload...";
});
elements.refreshButton.addEventListener("click", () => {
    clearMessage();
    refreshPrograms();
});
elements.search.addEventListener("input", renderPrograms);

refreshPrograms();
