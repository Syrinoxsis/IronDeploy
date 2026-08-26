const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    driversDirectory: document.querySelector("#drivers-directory"),
    storageState: document.querySelector("#storage-state"),
    freeSpace: document.querySelector("#free-space"),
    storageProgress: document.querySelector("#storage-progress"),
    usedSpace: document.querySelector("#used-space"),
    minimumFreeSpace: document.querySelector("#minimum-free-space"),
    limitFiles: document.querySelector("#limit-files"),
    limitDepth: document.querySelector("#limit-depth"),
    limitPath: document.querySelector("#limit-path"),
    limitTtl: document.querySelector("#limit-ttl"),
    limitActive: document.querySelector("#limit-active"),
    limitFree: document.querySelector("#limit-free"),
    uploadSummary: document.querySelector("#upload-summary"),
    deleteAbandonedButton: document.querySelector("#delete-abandoned-button"),
    uploadList: document.querySelector("#upload-list"),
    emptyState: document.querySelector("#empty-state"),
};

function translate(value) {
    return window.IronI18n?.t(value) || value;
}

async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
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
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    const unit = Math.min(
        Math.floor(Math.log(bytes) / Math.log(1024)),
        units.length - 1
    );
    return `${(bytes / (1024 ** unit)).toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function formatDate(value) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "?" : parsed.toLocaleString();
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

function renderUpload(upload) {
    const row = document.createElement("div");
    row.className = "upload-row";

    const identity = document.createElement("div");
    const name = document.createElement("div");
    name.className = "upload-name";
    name.textContent = upload.vendor && upload.model
        ? `${upload.vendor} / ${upload.model}`
        : translate("Invalid upload metadata");
    const id = document.createElement("span");
    id.className = "upload-id";
    id.textContent = upload.slot
        ? `${upload.uploadId} · ${translate("slot")} ${upload.slot}`
        : upload.uploadId;
    identity.append(name, id);

    const details = document.createElement("div");
    details.className = "upload-details";
    const totals = document.createElement("span");
    totals.textContent = `${upload.fileCount.toLocaleString()} ${translate("files")} ? ${formatBytes(upload.size)}`;
    const timestamps = document.createElement("span");
    timestamps.textContent = `${translate("Last activity")}: ${formatDate(upload.updatedAt)}`;
    details.append(totals, timestamps);

    const status = document.createElement("span");
    status.className = `upload-status ${upload.status}`;
    const statusLabels = {
        abandoned: "Abandoned",
        active: "Active upload",
        interrupted: "Interrupted",
        invalid: "Invalid",
    };
    status.textContent = translate(statusLabels[upload.status] || "Invalid");

    row.append(identity, details, status);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger-button";
    remove.textContent = translate(
        upload.status === "active" ? "Stop and delete" : "Delete"
    );
    remove.addEventListener("click", async () => {
        const confirmations = {
            active: "Stop and delete this active upload? Its temporary files cannot be recovered.",
            interrupted: "Delete this interrupted upload? Its temporary files cannot be recovered.",
            invalid: "Delete this invalid upload? Its temporary files cannot be recovered.",
            abandoned: "Delete this abandoned upload? Its temporary files cannot be recovered.",
        };
        if (!window.confirm(translate(confirmations[upload.status]))) return;
        remove.disabled = true;
        try {
            const result = await apiFetch(
                `/api/info/driver-uploads/${encodeURIComponent(upload.uploadId)}`,
                { method: "DELETE" }
            );
            if (await loadInfo()) {
                showMessage(translate(
                    result.deletionPending
                        ? "Upload stopped. Temporary files are being removed."
                        : "Temporary upload deleted."
                ));
            }
        } catch (error) {
            remove.disabled = false;
            showMessage(error.message, "error");
        }
    });
    row.append(remove);
    return row;
}

function renderInfo(payload) {
    const { storage, limits, counts, uploads } = payload;
    elements.driversDirectory.textContent = payload.directory;
    elements.storageState.textContent = translate(
        storage.hasMinimumFreeSpace ? "Enough space" : "Low disk space"
    );
    elements.storageState.classList.toggle("warning", !storage.hasMinimumFreeSpace);
    elements.freeSpace.textContent = formatBytes(storage.free);
    elements.storageProgress.value = storage.total
        ? Math.min((storage.used / storage.total) * 100, 100)
        : 0;
    elements.usedSpace.textContent = `${translate("Used")}: ${formatBytes(storage.used)} / ${formatBytes(storage.total)}`;
    elements.minimumFreeSpace.textContent = `${translate("Required free")}: ${formatBytes(storage.minimumFree)}`;

    elements.limitFiles.textContent = limits.maxFiles.toLocaleString();
    elements.limitDepth.textContent = limits.maxDepth.toLocaleString();
    elements.limitPath.textContent = `${limits.maxFullPath} ${translate("characters")}`;
    elements.limitTtl.textContent = `${limits.uploadTtlHours} ${translate("hours")}`;
    elements.limitActive.textContent = limits.maxActiveUploads.toLocaleString();
    elements.limitFree.textContent = `${limits.minFreeSpaceGiB} GiB`;

    elements.uploadSummary.textContent =
        `${counts.total} ${translate("unfinished")} ? ` +
        `${counts.active} ${translate("active")} ? ` +
        `${counts.interrupted} ${translate("interrupted")} ? ` +
        `${counts.abandoned} ${translate("abandoned")} ? ` +
        `${counts.invalid} ${translate("invalid")}`;
    elements.deleteAbandonedButton.disabled = counts.abandoned === 0;
    elements.emptyState.hidden = uploads.length !== 0;
    elements.uploadList.replaceChildren(...uploads.map(renderUpload));
}

async function loadInfo() {
    clearMessage();
    elements.refreshButton.disabled = true;
    elements.refreshButton.classList.add("is-loading");
    try {
        renderInfo(await apiFetch("/api/info/driver-uploads"));
        return true;
    } catch (error) {
        showMessage(error.message, "error");
        return false;
    } finally {
        elements.refreshButton.disabled = false;
        elements.refreshButton.classList.remove("is-loading");
    }
}

elements.refreshButton.addEventListener("click", loadInfo);
elements.deleteAbandonedButton.addEventListener("click", async () => {
    if (!window.confirm(translate(
        "Delete all abandoned uploads? Their temporary files cannot be recovered."
    ))) return;
    elements.deleteAbandonedButton.disabled = true;
    try {
        const result = await apiFetch(
            "/api/info/driver-uploads/abandoned",
            { method: "DELETE" }
        );
        if (await loadInfo()) {
            showMessage(`${translate("Deleted abandoned uploads")}: ${result.deleted}.`);
        }
    } catch (error) {
        elements.deleteAbandonedButton.disabled = false;
        showMessage(error.message, "error");
    }
});

loadInfo();
