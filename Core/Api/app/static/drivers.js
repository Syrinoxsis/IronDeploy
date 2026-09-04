const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    vendorForm: document.querySelector("#vendor-form"),
    vendorName: document.querySelector("#vendor-name"),
    vendorList: document.querySelector("#vendor-list"),
    packageVendor: document.querySelector("#package-vendor"),
    packageVendorLabel: document.querySelector("#package-vendor-label"),
    packageModel: document.querySelector("#package-model"),
    packageFolder: document.querySelector("#package-folder"),
    folderSummary: document.querySelector("#folder-summary"),
    uploadButton: document.querySelector("#upload-button"),
    progressRow: document.querySelector("#upload-progress-row"),
    progress: document.querySelector("#upload-progress"),
    progressText: document.querySelector("#upload-progress-text"),
    cancelUploadButton: document.querySelector("#cancel-upload-button"),
    packageList: document.querySelector("#package-list"),
    emptyState: document.querySelector("#empty-state"),
    packageCount: document.querySelector("#package-count"),
    driversDirectory: document.querySelector("#drivers-directory"),
    selectedVendorName: document.querySelector("#selected-vendor-name"),
    renameVendorButton: document.querySelector("#rename-vendor-button"),
    deleteVendorButton: document.querySelector("#delete-vendor-button"),
};

let currentListing = { vendors: [], packages: [] };
let activeVendorName = null;
let activeUpload = null;
let messageTimer = null;

function translate(value) {
    return window.IronI18n?.t(value) || value;
}

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
    const unit = Math.min(
        Math.floor(Math.log(bytes) / Math.log(1024)),
        units.length - 1
    );
    return `${(bytes / (1024 ** unit)).toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`;
}

function validateName(value, label) {
    if (
        !value ||
        value !== value.trim() ||
        value === "." ||
        value === ".." ||
        value.endsWith(".") ||
        value.endsWith(" ") ||
        /[<>:"/\\|?*\x00-\x1f]/.test(value)
    ) {
        throw new Error(`Enter a valid ${label} name.`);
    }
    const stem = value.split(".", 1)[0].replace(/[ .]+$/, "").toUpperCase();
    if (
        ["CON", "PRN", "AUX", "NUL"].includes(stem) ||
        /^(COM|LPT)[1-9]$/.test(stem)
    ) {
        throw new Error("This is a reserved Windows name.");
    }
    return value;
}

function packageUrl(vendor, model, suffix = "") {
    return `/api/drivers/packages/${encodeURIComponent(vendor)}/${encodeURIComponent(model)}${suffix}`;
}

function renderVendor(vendor) {
    const row = document.createElement("button");
    row.className = `vendor-row${vendor.name === activeVendorName ? " is-active" : ""}`;
    row.type = "button";
    row.setAttribute("aria-pressed", String(vendor.name === activeVendorName));
    const main = document.createElement("span");
    main.className = "vendor-row-main";
    main.innerHTML =
        '<svg aria-hidden="true" viewBox="0 0 24 24">' +
        '<path d="M5 4h14v16H5z"></path>' +
        '<path d="M9 8h6M9 12h6M9 16h3"></path></svg>';
    const name = document.createElement("span");
    name.className = "vendor-name";
    name.textContent = vendor.name;
    const count = document.createElement("span");
    count.className = "vendor-count";
    count.textContent = String(vendor.packageCount);
    main.append(name);
    row.append(main, count);
    row.addEventListener("click", () => selectVendor(vendor.name));
    return row;
}

function renderPackage(driverPackage) {
    const card = document.createElement("article");
    card.className = `package-card availability-card${driverPackage.enabled ? "" : " is-disabled"}`;
    const top = document.createElement("div");
    top.className = "package-card-top";
    const identity = document.createElement("div");
    identity.className = "package-identity";
    const title = document.createElement("h3");
    title.textContent = driverPackage.model;
    const details = document.createElement("p");
    details.className = "package-details";
    details.textContent =
        `${formatBytes(driverPackage.size)} · ${driverPackage.infCount} INF · ` +
        `${driverPackage.fileCount} files`;
    identity.append(title, details);

    const actions = document.createElement("div");
    actions.className = "package-actions";
    const availability = document.createElement("label");
    availability.className = "availability-toggle";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.role = "switch";
    enabled.checked = Boolean(driverPackage.enabled);
    enabled.setAttribute(
        "aria-label",
        `Offer ${driverPackage.vendor} / ${driverPackage.model} in WinPE`
    );
    const track = document.createElement("span");
    track.className = "availability-track";
    track.setAttribute("aria-hidden", "true");
    const state = document.createElement("span");
    state.className = "availability-state";
    function updateAvailability() {
        state.textContent = translate(driverPackage.enabled ? "Enabled" : "Disabled");
        card.classList.toggle("is-disabled", !driverPackage.enabled);
    }
    enabled.addEventListener("change", async () => {
        const nextEnabled = enabled.checked;
        enabled.disabled = true;
        try {
            const result = await apiFetch(
                packageUrl(driverPackage.vendor, driverPackage.model, "/enabled"),
                {
                    method: "POST",
                    body: JSON.stringify({ enabled: nextEnabled }),
                }
            );
            driverPackage.enabled = Boolean(result.package.enabled);
            enabled.checked = driverPackage.enabled;
            updateAvailability();
            showMessage(
                `${driverPackage.vendor} / ${driverPackage.model} ` +
                `${driverPackage.enabled ? "enabled" : "disabled"} for WinPE.`
            );
        } catch (error) {
            enabled.checked = Boolean(driverPackage.enabled);
            showMessage(error.message, "error");
        } finally {
            enabled.disabled = false;
        }
    });
    availability.append(enabled, track, state);
    updateAvailability();
    const rename = document.createElement("button");
    rename.className = "text-button";
    rename.type = "button";
    rename.textContent = translate("Rename");
    const remove = document.createElement("button");
    remove.className = "text-button danger";
    remove.type = "button";
    remove.textContent = translate("Delete");
    actions.append(availability, rename, remove);
    top.append(identity, actions);
    card.append(top);

    rename.addEventListener("click", () => {
        const nextModel = window.prompt(
            translate("New model name"),
            driverPackage.model
        );
        if (nextModel === null || nextModel === driverPackage.model) return;
        renamePackage(driverPackage, nextModel);
    });
    remove.addEventListener("click", () => deletePackage(driverPackage));
    return card;
}

function selectVendor(name) {
    if (activeUpload || name === activeVendorName) return;
    activeVendorName = name;
    elements.packageModel.value = "";
    elements.packageFolder.value = "";
    updateFolderSummary();
    renderListing();
}

function activeVendor() {
    return currentListing.vendors.find(
        (vendor) => vendor.name === activeVendorName
    ) || null;
}

function renderListing() {
    const previousVendor = activeVendorName || elements.packageVendor.value;
    activeVendorName = currentListing.vendors.some(
        (vendor) => vendor.name === previousVendor
    )
        ? previousVendor
        : (currentListing.vendors[0]?.name || null);

    elements.vendorList.replaceChildren(
        ...currentListing.vendors.map(renderVendor)
    );
    elements.packageVendor.replaceChildren(
        new Option(translate("Select vendor"), ""),
        ...currentListing.vendors.map((vendor) => new Option(vendor.name, vendor.name))
    );
    elements.packageVendor.value = activeVendorName || "";

    const vendor = activeVendor();
    const packages = activeVendorName
        ? currentListing.packages.filter(
            (driverPackage) => driverPackage.vendor === activeVendorName
        )
        : [];
    elements.packageList.replaceChildren(
        ...packages.map(renderPackage)
    );
    elements.selectedVendorName.textContent =
        activeVendorName || translate("No vendor selected");
    elements.packageVendorLabel.textContent =
        activeVendorName || translate("Vendor");
    elements.packageCount.textContent =
        `${packages.length} ${translate(packages.length === 1 ? "package" : "packages")}`;
    elements.emptyState.textContent = activeVendorName
        ? translate("No driver packages were found.")
        : translate("Create a vendor to begin.");
    elements.emptyState.hidden = packages.length > 0;
    elements.driversDirectory.textContent = currentListing.directory || "";
    elements.renameVendorButton.disabled = !vendor;
    elements.deleteVendorButton.disabled = !vendor;
    elements.uploadButton.disabled = !vendor;
    elements.packageModel.disabled = !vendor;
    elements.packageFolder.disabled = !vendor;
}

async function refreshDrivers() {
    elements.refreshButton.disabled = true;
    try {
        currentListing = await apiFetch("/api/drivers");
        renderListing();
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        elements.refreshButton.disabled = false;
    }
}

async function createVendor(event) {
    event.preventDefault();
    clearMessage();
    let name;
    try {
        name = validateName(elements.vendorName.value, "vendor");
    } catch (error) {
        showMessage(error.message, "error");
        return;
    }
    try {
        await apiFetch("/api/drivers/vendors", {
            method: "POST",
            body: JSON.stringify({ name }),
        });
        elements.vendorName.value = "";
        showMessage(`${name} created.`);
        activeVendorName = name;
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

async function renameVendor(name, value) {
    clearMessage();
    try {
        const newName = validateName(value, "vendor");
        const result = await apiFetch(
            `/api/drivers/vendors/${encodeURIComponent(name)}/rename`,
            {
                method: "POST",
                body: JSON.stringify({ newName }),
            }
        );
        showMessage(`${result.oldName} renamed to ${result.name}.`);
        if (activeVendorName === name) activeVendorName = result.name;
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

async function deleteVendor(vendor) {
    const warning = vendor.packageCount
        ? `Delete ${vendor.name} and all ${vendor.packageCount} driver packages?`
        : `Delete vendor ${vendor.name}?`;
    if (!window.confirm(warning)) return;
    clearMessage();
    try {
        await apiFetch(`/api/drivers/vendors/${encodeURIComponent(vendor.name)}`, {
            method: "DELETE",
        });
        showMessage(`${vendor.name} deleted.`);
        if (activeVendorName === vendor.name) activeVendorName = null;
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

async function renamePackage(driverPackage, value) {
    clearMessage();
    try {
        const newModel = validateName(value, "model");
        const result = await apiFetch(
            packageUrl(driverPackage.vendor, driverPackage.model, "/rename"),
            {
                method: "POST",
                body: JSON.stringify({ newModel }),
            }
        );
        showMessage(`${result.oldModel} renamed to ${result.model}.`);
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

async function deletePackage(driverPackage) {
    if (!window.confirm(`Delete ${driverPackage.vendor} / ${driverPackage.model}?`)) {
        return;
    }
    clearMessage();
    try {
        await apiFetch(packageUrl(driverPackage.vendor, driverPackage.model), {
            method: "DELETE",
        });
        showMessage(`${driverPackage.vendor} / ${driverPackage.model} deleted.`);
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    }
}

function selectedFiles() {
    const files = Array.from(elements.packageFolder.files || []);
    if (!files.length) throw new Error("Choose a driver folder.");
    return files.map((file) => {
        const original = file.webkitRelativePath || file.name;
        const parts = original.split("/");
        const relativePath = parts.length > 1 ? parts.slice(1).join("/") : original;
        if (!relativePath) throw new Error("The selected folder contains an invalid path.");
        return { file, relativePath };
    });
}

function uploadOneFile(uploadId, item, onProgress) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeUpload.request = request;
        request.open(
            "PUT",
            `/api/drivers/package-uploads/${encodeURIComponent(uploadId)}/files`
        );
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader(
            "x-irondeploy-relative-path",
            encodeURIComponent(item.relativePath)
        );
        request.upload.addEventListener("progress", (event) => {
            onProgress(event.loaded);
        });
        request.addEventListener("load", () => {
            if (request.status >= 200 && request.status < 300) {
                resolve();
                return;
            }
            let detail = request.responseText;
            try {
                detail = JSON.parse(request.responseText).detail || detail;
            } catch {
                // Use response text.
            }
            reject(new Error(detail || `HTTP ${request.status}`));
        });
        request.addEventListener("error", () => reject(new Error("Driver upload failed.")));
        request.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
        request.send(item.file);
    });
}

function setUploadState(uploading) {
    elements.progressRow.hidden = !uploading;
    elements.uploadButton.disabled = uploading || !activeVendor();
    elements.packageVendor.disabled = uploading;
    elements.packageModel.disabled = uploading;
    elements.packageFolder.disabled = uploading;
    elements.renameVendorButton.disabled = uploading || !activeVendor();
    elements.deleteVendorButton.disabled = uploading || !activeVendor();
    elements.vendorList.querySelectorAll(".vendor-row").forEach((row) => {
        row.disabled = uploading;
    });
}

async function uploadPackage() {
    clearMessage();
    let vendor;
    let model;
    let files;
    try {
        vendor = validateName(elements.packageVendor.value, "vendor");
        model = validateName(elements.packageModel.value, "model");
        files = selectedFiles();
        if (!files.some((item) => item.relativePath.toLowerCase().endsWith(".inf"))) {
            throw new Error("The selected folder contains no INF files.");
        }
    } catch (error) {
        showMessage(error.message, "error");
        return;
    }

    const totalSize = files.reduce((sum, item) => sum + item.file.size, 0);
    let completedSize = 0;
    let uploadId = null;
    let finalized = false;
    activeUpload = { cancelled: false, request: null };
    setUploadState(true);
    elements.progress.value = 0;
    elements.progressText.textContent = translate("Preparing upload...");

    try {
        const started = await apiFetch("/api/drivers/package-uploads", {
            method: "POST",
            body: JSON.stringify({ vendor, model }),
        });
        uploadId = started.uploadId;
        for (let index = 0; index < files.length; index += 1) {
            if (activeUpload.cancelled) throw new Error("Upload cancelled.");
            const item = files[index];
            elements.progressText.textContent =
                `Uploading ${index + 1} of ${files.length}: ${item.relativePath}`;
            await uploadOneFile(uploadId, item, (loaded) => {
                const sent = completedSize + loaded;
                elements.progress.value = totalSize > 0
                    ? Math.min(100, (sent / totalSize) * 100)
                    : ((index + 1) / files.length) * 100;
            });
            completedSize += item.file.size;
        }
        elements.progressText.textContent = "Publishing package...";
        await apiFetch(
            `/api/drivers/package-uploads/${encodeURIComponent(uploadId)}/finalize`,
            { method: "POST" }
        );
        finalized = true;
        elements.progress.value = 100;
        elements.packageModel.value = "";
        elements.packageFolder.value = "";
        updateFolderSummary();
        showMessage(`${vendor} / ${model} uploaded.`);
        await refreshDrivers();
    } catch (error) {
        showMessage(error.message, "error");
    } finally {
        if (uploadId && !finalized) {
            try {
                await apiFetch(
                    `/api/drivers/package-uploads/${encodeURIComponent(uploadId)}`,
                    { method: "DELETE" }
                );
            } catch {
                // A failed cleanup remains hidden and never becomes a package.
            }
        }
        activeUpload = null;
        setUploadState(false);
    }
}

function cancelUpload() {
    if (!activeUpload) return;
    activeUpload.cancelled = true;
    activeUpload.request?.abort();
    elements.progressText.textContent = "Cancelling upload...";
}

function updateFolderSummary() {
    const files = Array.from(elements.packageFolder.files || []);
    if (!files.length) {
        elements.folderSummary.textContent = translate("No folder selected");
        elements.folderSummary.hidden = true;
        return;
    }
    const root = (files[0].webkitRelativePath || "").split("/")[0] || "folder";
    const size = files.reduce((sum, file) => sum + file.size, 0);
    const infCount = files.filter((file) => file.name.toLowerCase().endsWith(".inf")).length;
    elements.folderSummary.textContent =
        `${root} · ${files.length} files · ${infCount} INF · ${formatBytes(size)}`;
    elements.folderSummary.hidden = false;
}

elements.vendorForm.addEventListener("submit", createVendor);
elements.packageFolder.addEventListener("change", updateFolderSummary);
elements.uploadButton.addEventListener("click", uploadPackage);
elements.cancelUploadButton.addEventListener("click", cancelUpload);
elements.refreshButton.addEventListener("click", refreshDrivers);
elements.renameVendorButton.addEventListener("click", () => {
    const vendor = activeVendor();
    if (!vendor) return;
    const nextName = window.prompt(translate("New vendor name"), vendor.name);
    if (nextName === null || nextName === vendor.name) return;
    renameVendor(vendor.name, nextName);
});
elements.deleteVendorButton.addEventListener("click", () => {
    const vendor = activeVendor();
    if (vendor) deleteVendor(vendor);
});

refreshDrivers();
