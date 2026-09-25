// TEMPORARY WINPE DRIVER UPLOAD: isolated UI for the removable stop-gap feature.
const winpeDriverElements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    folder: document.querySelector("#winpe-driver-folder"),
    folderSummary: document.querySelector("#winpe-driver-folder-summary"),
    uploadButton: document.querySelector("#winpe-driver-upload-button"),
    progressRow: document.querySelector("#winpe-driver-progress-row"),
    progress: document.querySelector("#winpe-driver-progress"),
    progressText: document.querySelector("#winpe-driver-progress-text"),
    cancelButton: document.querySelector("#winpe-driver-cancel-button"),
    current: document.querySelector("#winpe-driver-current"),
    currentDetails: document.querySelector("#winpe-driver-current-details"),
    directory: document.querySelector("#winpe-driver-directory"),
    empty: document.querySelector("#winpe-driver-empty"),
    deleteButton: document.querySelector("#winpe-driver-delete-button"),
};

let activeWinPEDriverUpload = null;

function winpeDriverTranslate(value) {
    return window.IronI18n?.t(value) || value;
}

function showWinPEDriverMessage(text, type = "success") {
    winpeDriverElements.message.hidden = false;
    winpeDriverElements.message.className = `notice ${type}`;
    winpeDriverElements.message.textContent = text;
}

async function winpeDriverFetch(url, options = {}) {
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

function formatWinPEDriverBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    return `${(bytes / (1024 ** unit)).toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function selectedWinPEDriverFiles() {
    const files = Array.from(winpeDriverElements.folder.files || []);
    if (!files.length) throw new Error("Choose a WinPE driver folder.");
    return files.map((file) => {
        const original = file.webkitRelativePath || file.name;
        const parts = original.split("/");
        const relativePath = parts.length > 1 ? parts.slice(1).join("/") : original;
        if (!relativePath) throw new Error("The selected folder contains an invalid path.");
        return { file, relativePath };
    });
}

function updateWinPEDriverFolderSummary() {
    const files = Array.from(winpeDriverElements.folder.files || []);
    if (!files.length) {
        winpeDriverElements.folderSummary.hidden = true;
        winpeDriverElements.folderSummary.textContent = "";
        return;
    }
    const root = (files[0].webkitRelativePath || "").split("/")[0] || "folder";
    const size = files.reduce((sum, file) => sum + file.size, 0);
    const infCount = files.filter((file) => file.name.toLowerCase().endsWith(".inf")).length;
    winpeDriverElements.folderSummary.textContent =
        `${root} · ${files.length} files · ${infCount} INF · ${formatWinPEDriverBytes(size)}`;
    winpeDriverElements.folderSummary.hidden = false;
}

function renderWinPEDrivers(details) {
    winpeDriverElements.current.hidden = !details.available;
    winpeDriverElements.empty.hidden = Boolean(details.available);
    winpeDriverElements.deleteButton.hidden = !details.available;
    winpeDriverElements.currentDetails.textContent = details.available
        ? `${details.fileCount} files · ${details.infCount} INF · ${formatWinPEDriverBytes(details.size)}`
        : "";
    winpeDriverElements.directory.textContent = details.directory || "";
}

async function refreshWinPEDrivers() {
    try {
        renderWinPEDrivers(await winpeDriverFetch("/api/image-config/winpe-drivers"));
    } catch (error) {
        showWinPEDriverMessage(error.message, "error");
    }
}

function uploadOneWinPEDriverFile(uploadId, item, onProgress) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeWinPEDriverUpload.request = request;
        request.open(
            "PUT",
            `/api/image-config/winpe-driver-uploads/${encodeURIComponent(uploadId)}/files`
        );
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader(
            "x-irondeploy-relative-path",
            encodeURIComponent(item.relativePath)
        );
        request.upload.addEventListener("progress", (event) => onProgress(event.loaded));
        request.addEventListener("load", () => {
            if (request.status >= 200 && request.status < 300) {
                resolve();
                return;
            }
            let detail = request.responseText;
            try {
                detail = JSON.parse(request.responseText).detail || detail;
            } catch {
                // Use the response body as-is.
            }
            reject(new Error(detail || `HTTP ${request.status}`));
        });
        request.addEventListener("error", () => reject(new Error("WinPE driver upload failed.")));
        request.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
        request.send(item.file);
    });
}

function setWinPEDriverUploadState(uploading) {
    winpeDriverElements.progressRow.hidden = !uploading;
    winpeDriverElements.uploadButton.disabled = uploading;
    winpeDriverElements.folder.disabled = uploading;
    winpeDriverElements.deleteButton.disabled = uploading;
}

async function uploadWinPEDrivers() {
    let files;
    try {
        files = selectedWinPEDriverFiles();
        if (!files.some((item) => item.relativePath.toLowerCase().endsWith(".inf"))) {
            throw new Error("The selected folder contains no INF files.");
        }
    } catch (error) {
        showWinPEDriverMessage(error.message, "error");
        return;
    }

    const totalSize = files.reduce((sum, item) => sum + item.file.size, 0);
    let completedSize = 0;
    let uploadId = null;
    let finalized = false;
    activeWinPEDriverUpload = { cancelled: false, request: null };
    setWinPEDriverUploadState(true);
    winpeDriverElements.progress.value = 0;
    winpeDriverElements.progressText.textContent = winpeDriverTranslate("Preparing upload...");

    try {
        const started = await winpeDriverFetch("/api/image-config/winpe-driver-uploads", {
            method: "POST",
        });
        uploadId = started.uploadId;
        for (let index = 0; index < files.length; index += 1) {
            if (activeWinPEDriverUpload.cancelled) throw new Error("Upload cancelled.");
            const item = files[index];
            winpeDriverElements.progressText.textContent =
                `Uploading ${index + 1} of ${files.length}: ${item.relativePath}`;
            await uploadOneWinPEDriverFile(uploadId, item, (loaded) => {
                const sent = completedSize + loaded;
                winpeDriverElements.progress.value = totalSize > 0
                    ? Math.min(100, (sent / totalSize) * 100)
                    : ((index + 1) / files.length) * 100;
            });
            completedSize += item.file.size;
        }
        winpeDriverElements.progressText.textContent = winpeDriverTranslate("Publishing package...");
        const result = await winpeDriverFetch(
            `/api/image-config/winpe-driver-uploads/${encodeURIComponent(uploadId)}/finalize`,
            { method: "POST" }
        );
        finalized = true;
        winpeDriverElements.progress.value = 100;
        winpeDriverElements.folder.value = "";
        updateWinPEDriverFolderSummary();
        renderWinPEDrivers(result);
        showWinPEDriverMessage(
            winpeDriverTranslate("WinPE drivers uploaded. Rebuild WinPE to include them.")
        );
    } catch (error) {
        showWinPEDriverMessage(error.message, "error");
    } finally {
        if (uploadId && !finalized) {
            try {
                await winpeDriverFetch(
                    `/api/image-config/winpe-driver-uploads/${encodeURIComponent(uploadId)}`,
                    { method: "DELETE" }
                );
            } catch {
                // An unfinished upload is never copied into WinPE.
            }
        }
        activeWinPEDriverUpload = null;
        setWinPEDriverUploadState(false);
    }
}

function cancelWinPEDriverUpload() {
    if (!activeWinPEDriverUpload) return;
    activeWinPEDriverUpload.cancelled = true;
    activeWinPEDriverUpload.request?.abort();
    winpeDriverElements.progressText.textContent = winpeDriverTranslate("Cancelling upload...");
}

async function deleteWinPEDrivers() {
    if (!window.confirm(winpeDriverTranslate("Delete the WinPE boot drivers?"))) return;
    try {
        await winpeDriverFetch("/api/image-config/winpe-drivers", { method: "DELETE" });
        winpeDriverElements.folder.value = "";
        updateWinPEDriverFolderSummary();
        await refreshWinPEDrivers();
        showWinPEDriverMessage(
            winpeDriverTranslate(
                "WinPE drivers deleted. Rebuild WinPE to remove them from the image."
            )
        );
    } catch (error) {
        showWinPEDriverMessage(error.message, "error");
    }
}

winpeDriverElements.folder.addEventListener("change", updateWinPEDriverFolderSummary);
winpeDriverElements.uploadButton.addEventListener("click", uploadWinPEDrivers);
winpeDriverElements.cancelButton.addEventListener("click", cancelWinPEDriverUpload);
winpeDriverElements.deleteButton.addEventListener("click", deleteWinPEDrivers);
winpeDriverElements.refreshButton.addEventListener("click", refreshWinPEDrivers);
refreshWinPEDrivers();
