const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    fileInput: document.querySelector("#image-file"),
    uploadButton: document.querySelector("#upload-button"),
    progressRow: document.querySelector("#upload-progress-row"),
    progress: document.querySelector("#upload-progress"),
    progressText: document.querySelector("#upload-progress-text"),
    cancelUploadButton: document.querySelector("#cancel-upload-button"),
    imageList: document.querySelector("#image-list"),
    emptyState: document.querySelector("#empty-state"),
    imageCount: document.querySelector("#image-count"),
    imagesDirectory: document.querySelector("#images-directory"),
    conversionCard: document.querySelector("#conversion-card"),
    conversionMessage: document.querySelector("#conversion-message"),
    conversionStatus: document.querySelector("#conversion-status"),
    cancelConversionButton: document.querySelector("#cancel-conversion-button"),
    esdDecision: document.querySelector("#esd-decision"),
    esdDecisionFile: document.querySelector("#esd-decision-file"),
    uploadConvertButton: document.querySelector("#upload-convert-button"),
    uploadOnlyButton: document.querySelector("#upload-only-button"),
    uploadCancelButton: document.querySelector("#upload-cancel-button"),
};

let activeUploadRequest = null;
let pendingEsdFile = null;
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

function indexLabel(index) {
    const details = [index.name, index.architecture].filter(Boolean).join(" · ");
    return `Index ${index.index}${details ? ` — ${details}` : ""}`;
}

function createImageCard(image) {
    const card = document.createElement("article");
    card.className = "image-card";

    const top = document.createElement("div");
    top.className = "image-card-top";
    const identity = document.createElement("div");
    const titleRow = document.createElement("div");
    titleRow.className = "image-title-row";
    const title = document.createElement("h3");
    title.textContent = image.name;
    const format = document.createElement("span");
    format.className = `format-badge ${image.format.toLowerCase()}`;
    format.textContent = image.format;
    titleRow.append(title, format);
    const details = document.createElement("p");
    details.textContent = `${formatBytes(image.size)} · Updated ${new Date(image.modifiedAt).toLocaleString()}`;
    identity.append(titleRow, details);
    top.append(identity);

    const actions = document.createElement("div");
    actions.className = "image-actions";
    if (image.format === "WIM") {
        const rename = document.createElement("button");
        rename.className = "secondary-button";
        rename.type = "button";
        rename.textContent = "Rename";
        rename.addEventListener("click", () => {
            beginInlineRename(image, title, actions);
        });
        actions.append(rename);
    }
    if (image.canConvert) {
        const convert = document.createElement("button");
        convert.className = "secondary-button";
        convert.type = "button";
        convert.textContent = "Convert to WIM";
        convert.addEventListener("click", () => convertImage(image.name));
        actions.append(convert);
    }
    if (actions.childElementCount) top.append(actions);
    card.append(top);

    if (image.inspectionError) {
        const error = document.createElement("div");
        error.className = "inspection-error";
        error.textContent = image.inspectionError;
        card.append(error);
        return card;
    }

    const indexArea = document.createElement("div");
    indexArea.className = "index-area";
    const label = document.createElement("label");
    const labelText = document.createElement("span");
    labelText.textContent = "Default deployment index";
    const select = document.createElement("select");
    for (const index of image.indexes) {
        const option = document.createElement("option");
        option.value = String(index.index);
        option.textContent = indexLabel(index);
        option.title = index.description || index.name || "";
        select.append(option);
    }
    select.value = String(image.defaultIndex ?? "");
    select.addEventListener("change", async () => {
        select.disabled = true;
        try {
            await apiFetch(`/api/deployment-images/${encodeURIComponent(image.name)}/default-index`, {
                method: "POST",
                body: JSON.stringify({ defaultIndex: Number(select.value) }),
            });
            showMessage(`Default index for ${image.name} saved.`);
        } catch (error) {
            showMessage(error.message, "error");
            await refreshImages();
        } finally {
            select.disabled = false;
        }
    });
    label.append(labelText, select);
    indexArea.append(label);
    card.append(indexArea);
    return card;
}

function renderImages(payload) {
    elements.imagesDirectory.textContent = payload.directory || "";
    const images = payload.images || [];
    elements.imageCount.textContent = `${images.length} image${images.length === 1 ? "" : "s"}`;
    elements.emptyState.hidden = images.length !== 0;
    elements.imageList.replaceChildren(...images.map(createImageCard));
}

async function refreshImages() {
    elements.refreshButton.disabled = true;
    try {
        renderImages(await apiFetch("/api/deployment-images"));
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        elements.refreshButton.disabled = false;
    }
}

function uploadFile(file) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeUploadRequest = request;
        const finish = () => {
            if (activeUploadRequest === request) activeUploadRequest = null;
        };
        request.open("POST", "/api/deployment-images/upload");
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader("x-irondeploy-filename", encodeURIComponent(file.name));
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

function closeEsdDecision() {
    elements.esdDecision.hidden = true;
    pendingEsdFile = null;
}

function openEsdDecision(file) {
    pendingEsdFile = file;
    elements.esdDecisionFile.textContent = `${file.name} · ${formatBytes(file.size)}`;
    elements.esdDecision.hidden = false;
    elements.uploadConvertButton.focus();
}

async function beginUpload() {
    clearMessage();
    const file = elements.fileInput.files[0];
    if (!file) {
        showMessage("Choose an install.wim or install.esd file first.", "error");
        return;
    }
    if (!/\.(wim|esd)$/i.test(file.name)) {
        showMessage("Only .wim and .esd files are accepted.", "error");
        return;
    }
    if (/\.esd$/i.test(file.name)) {
        openEsdDecision(file);
        return;
    }
    await performUpload(file, false);
}

async function performUpload(file, convertAfterUpload) {
    elements.uploadButton.disabled = true;
    elements.fileInput.disabled = true;
    elements.progressRow.hidden = false;
    elements.cancelUploadButton.hidden = false;
    elements.progress.value = 0;
    elements.progressText.textContent = "Starting upload...";
    try {
        const result = await uploadFile(file);
        elements.progress.value = 100;
        elements.progressText.textContent = `Uploaded ${formatBytes(result.size)}.`;
        elements.fileInput.value = "";
        showMessage(`${result.name} uploaded successfully.`);
        await refreshImages();
        if (result.canConvert && convertAfterUpload) {
            await convertImage(result.name);
        }
    } catch (error) {
        showMessage(error.message, error.message === "Upload cancelled." ? "success" : "error");
    } finally {
        elements.uploadButton.disabled = false;
        elements.fileInput.disabled = false;
        elements.cancelUploadButton.hidden = true;
    }
}

async function convertImage(name) {
    clearMessage();
    try {
        renderConversion(await apiFetch(`/api/deployment-images/${encodeURIComponent(name)}/convert`, {
            method: "POST",
        }));
    } catch (error) {
        showMessage(error.message, "error");
    }
}

function beginInlineRename(image, title, actions) {
    if (actions.dataset.renaming === "1") return;
    actions.dataset.renaming = "1";
    const originalActions = [...actions.childNodes];
    const input = document.createElement("input");
    input.className = "rename-input";
    input.type = "text";
    input.value = image.name;
    input.setAttribute("aria-label", `Rename ${image.name}`);

    const save = document.createElement("button");
    save.className = "primary-button compact-button";
    save.type = "button";
    save.textContent = "Save";
    const cancel = document.createElement("button");
    cancel.className = "secondary-button compact-button";
    cancel.type = "button";
    cancel.textContent = "Cancel";

    function restore() {
        input.replaceWith(title);
        actions.replaceChildren(...originalActions);
        delete actions.dataset.renaming;
    }

    async function submit() {
        const newName = input.value;
        const baseName = newName.split(".", 1)[0].replace(/[ .]+$/, "").toUpperCase();
        const invalidName = (
            !newName ||
            newName !== newName.trim() ||
            newName === "." ||
            newName === ".." ||
            newName.endsWith(".") ||
            /[<>:"/\\|?*\x00-\x1f]/.test(newName) ||
            ["CON", "PRN", "AUX", "NUL"].includes(baseName) ||
            /^(COM|LPT)[1-9]$/.test(baseName) ||
            !newName.toLowerCase().endsWith(".wim")
        );
        if (invalidName) {
            showMessage(
                "Enter a valid .wim file name. Reserved Windows names are not allowed.",
                "error"
            );
            input.focus();
            return;
        }
        if (newName === image.name) {
            restore();
            return;
        }

        clearMessage();
        input.disabled = true;
        save.disabled = true;
        cancel.disabled = true;
        try {
            const result = await apiFetch(
                `/api/deployment-images/${encodeURIComponent(image.name)}/rename`,
                {
                    method: "POST",
                    body: JSON.stringify({ newName }),
                }
            );
            showMessage(`${result.oldName} renamed to ${result.name}.`);
            await refreshImages();
        } catch (error) {
            showMessage(error.message, "error");
            input.disabled = false;
            save.disabled = false;
            cancel.disabled = false;
            input.focus();
            input.select();
        }
    }

    save.addEventListener("click", submit);
    cancel.addEventListener("click", restore);
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            submit();
        } else if (event.key === "Escape") {
            event.preventDefault();
            restore();
        }
    });

    title.replaceWith(input);
    actions.replaceChildren(save, cancel);
    input.focus();
    input.setSelectionRange(0, image.name.toLowerCase().endsWith(".wim")
        ? image.name.length - 4
        : image.name.length);
}

function renderConversion(state) {
    elements.conversionCard.hidden = state.status === "idle";
    elements.conversionStatus.className = `status-pill ${state.status}`;
    elements.conversionStatus.textContent = state.status || "idle";
    elements.cancelConversionButton.hidden = state.status !== "running";
    elements.cancelConversionButton.disabled = state.message === "Cancelling image conversion...";
    elements.conversionMessage.textContent = [state.message, state.logPath ? `Log: ${state.logPath}` : ""]
        .filter(Boolean).join(" ");
}

async function cancelConversion() {
    elements.cancelConversionButton.disabled = true;
    try {
        renderConversion(await apiFetch("/api/deployment-images/conversion/cancel", {
            method: "POST",
        }));
    } catch (error) {
        showMessage(error.message, "error");
        elements.cancelConversionButton.disabled = false;
    }
}

async function refreshConversion() {
    try {
        const state = await apiFetch("/api/deployment-images/conversion");
        const wasRunning = elements.conversionStatus.textContent === "running";
        renderConversion(state);
        if (wasRunning && state.status === "succeeded") await refreshImages();
    } catch {
        // Image listing remains usable if conversion status is unavailable.
    }
}

elements.refreshButton.addEventListener("click", () => {
    clearMessage();
    refreshImages();
});
elements.uploadButton.addEventListener("click", beginUpload);
elements.cancelUploadButton.addEventListener("click", () => {
    if (activeUploadRequest) activeUploadRequest.abort();
});
elements.cancelConversionButton.addEventListener("click", cancelConversion);
elements.uploadConvertButton.addEventListener("click", () => {
    const file = pendingEsdFile;
    closeEsdDecision();
    if (file) performUpload(file, true);
});
elements.uploadOnlyButton.addEventListener("click", () => {
    const file = pendingEsdFile;
    closeEsdDecision();
    if (file) performUpload(file, false);
});
elements.uploadCancelButton.addEventListener("click", closeEsdDecision);
elements.esdDecision.addEventListener("click", (event) => {
    if (event.target === elements.esdDecision) closeEsdDecision();
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.esdDecision.hidden) {
        closeEsdDecision();
    }
});

refreshImages();
refreshConversion();
window.setInterval(refreshConversion, 3000);
