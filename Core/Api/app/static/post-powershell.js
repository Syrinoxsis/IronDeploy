const psElements = {
    refresh: document.querySelector("#refresh-button"),
    message: document.querySelector("#message-area"),
    file: document.querySelector("#script-file"),
    name: document.querySelector("#script-name"),
    arguments: document.querySelector("#script-arguments"),
    phase: document.querySelector("#script-phase"),
    mode: document.querySelector("#script-mode"),
    timeout: document.querySelector("#script-timeout"),
    upload: document.querySelector("#upload-button"),
    cancel: document.querySelector("#cancel-upload-button"),
    progressRow: document.querySelector("#upload-progress-row"),
    progress: document.querySelector("#upload-progress"),
    progressText: document.querySelector("#upload-progress-text"),
    profileName: document.querySelector("#profile-name"),
    directory: document.querySelector("#scripts-directory"),
    count: document.querySelector("#script-count"),
    empty: document.querySelector("#empty-state"),
    list: document.querySelector("#script-list"),
};

let activeUpload = null;

function psShowMessage(message, error = false) {
    psElements.message.textContent = message;
    psElements.message.className = `notice${error ? " error" : " success"}`;
    psElements.message.hidden = false;
}

function psFormatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "-";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
}

async function psFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
        headers.set("x-requested-with", "IronDeploy");
    }
    if (options.body && typeof options.body === "string") {
        headers.set("content-type", "application/json");
    }
    const response = await fetch(url, { ...options, headers, cache: "no-store" });
    if (response.status === 401) {
        window.location.assign(`/login?next=${encodeURIComponent(location.pathname)}`);
        throw new Error("Authentication required.");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status}).`);
    return payload;
}

function psOption(value, label, selected) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = value === selected;
    return option;
}

function psSetting(labelText, control, className) {
    const field = document.createElement("label");
    field.className = `powershell-field ${className}`;
    const label = document.createElement("span");
    label.textContent = labelText;
    field.append(label, control);
    return field;
}

function psCreateCard(script) {
    const card = document.createElement("article");
    card.className = "image-card powershell-card";

    const head = document.createElement("div");
    head.className = "powershell-card-head";
    const title = document.createElement("div");
    title.className = "powershell-card-title";
    const name = document.createElement("strong");
    name.textContent = script.name;
    const meta = document.createElement("span");
    meta.textContent = `${psFormatBytes(script.size)} · position ${script.position + 1}`;
    title.append(name, meta);
    if (!script.available) {
        const unavailable = document.createElement("span");
        unavailable.className = "powershell-unavailable";
        unavailable.textContent = "The file is missing or exceeds the configured limit.";
        title.append(unavailable);
    }
    const sha = document.createElement("span");
    sha.className = "powershell-sha";
    sha.textContent = `SHA-256 ${script.sha256}`;
    head.append(title, sha);

    const settings = document.createElement("div");
    settings.className = "powershell-settings";
    const args = document.createElement("input");
    args.className = "arguments-input";
    args.maxLength = 500;
    args.value = script.arguments || "";
    args.placeholder = "Raw launch arguments";
    const phase = document.createElement("select");
    phase.className = "arguments-input";
    phase.append(
        psOption("before_software", "Before software", script.runPhase),
        psOption("after_software", "After software", script.runPhase),
    );
    const mode = document.createElement("select");
    mode.className = "arguments-input";
    mode.append(
        psOption("operator", "Operator chooses", script.selectionMode),
        psOption("automatic", "Automatic", script.selectionMode),
    );
    const timeout = document.createElement("input");
    timeout.className = "arguments-input";
    timeout.type = "number";
    timeout.min = "1";
    timeout.max = "86400";
    timeout.value = script.timeoutSeconds;
    settings.append(
        psSetting("Launch arguments", args, "powershell-setting-arguments"),
        psSetting("Run phase", phase, "powershell-setting-phase"),
        psSetting("Selection", mode, "powershell-setting-mode"),
        psSetting("Timeout, seconds", timeout, "powershell-setting-timeout"),
    );

    const actions = document.createElement("div");
    actions.className = "powershell-actions";
    const save = document.createElement("button");
    save.className = "primary-button";
    save.type = "button";
    save.textContent = "Save settings";
    save.addEventListener("click", async () => {
        save.disabled = true;
        try {
            await psFetch(`/api/post-powershell/${script.id}/settings`, {
                method: "POST",
                body: JSON.stringify({
                    arguments: args.value,
                    runPhase: phase.value,
                    selectionMode: mode.value,
                    timeoutSeconds: Number.parseInt(timeout.value, 10),
                }),
            });
            psShowMessage(`Settings for ${script.name} saved.`);
            await psRefresh();
        } catch (error) {
            psShowMessage(error.message, true);
        } finally {
            save.disabled = false;
        }
    });
    const remove = document.createElement("button");
    remove.className = "cancel-button";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
        if (!window.confirm(`Delete ${script.name}?`)) return;
        remove.disabled = true;
        try {
            await psFetch(`/api/post-powershell/${script.id}`, { method: "DELETE" });
            psShowMessage(`${script.name} deleted.`);
            await psRefresh();
        } catch (error) {
            psShowMessage(error.message, true);
            remove.disabled = false;
        }
    });
    actions.append(save, remove);
    card.append(head, settings, actions);
    return card;
}

async function psRefresh() {
    psElements.refresh.disabled = true;
    try {
        const payload = await psFetch("/api/post-powershell");
        const scripts = payload.scripts || [];
        psElements.profileName.textContent = payload.profileName || "Default";
        psElements.directory.textContent = payload.directory || "";
        psElements.count.textContent = `${scripts.length} script${scripts.length === 1 ? "" : "s"}`;
        psElements.empty.hidden = scripts.length !== 0;
        psElements.list.replaceChildren(...scripts.map(psCreateCard));
    } catch (error) {
        psShowMessage(error.message, true);
    } finally {
        psElements.refresh.disabled = false;
    }
}

function psNormalizeName(value, sourceName) {
    const name = (value.trim() || sourceName).trim();
    if (!name.toLowerCase().endsWith(".ps1") || /[\\/:*?"<>|]/.test(name)) {
        throw new Error("Enter a valid .ps1 file name.");
    }
    return name;
}

function psUploadFile(file, name) {
    return new Promise((resolve, reject) => {
        const request = new XMLHttpRequest();
        activeUpload = request;
        request.open("POST", "/api/post-powershell/upload");
        request.setRequestHeader("x-requested-with", "IronDeploy");
        request.setRequestHeader("x-irondeploy-filename", encodeURIComponent(name));
        request.setRequestHeader("x-irondeploy-arguments", encodeURIComponent(psElements.arguments.value));
        request.setRequestHeader("x-irondeploy-run-phase", psElements.phase.value);
        request.setRequestHeader("x-irondeploy-selection-mode", psElements.mode.value);
        request.setRequestHeader("x-irondeploy-timeout-seconds", psElements.timeout.value);
        request.upload.addEventListener("progress", event => {
            if (!event.lengthComputable) return;
            const percent = Math.round((event.loaded / event.total) * 100);
            psElements.progress.value = percent;
            psElements.progressText.textContent = `${percent}% · ${psFormatBytes(event.loaded)} / ${psFormatBytes(event.total)}`;
        });
        request.addEventListener("load", () => {
            activeUpload = null;
            const payload = JSON.parse(request.responseText || "{}");
            if (request.status >= 200 && request.status < 300) resolve(payload);
            else reject(new Error(payload.detail || `Upload failed (${request.status}).`));
        });
        request.addEventListener("error", () => reject(new Error("Upload connection failed.")));
        request.addEventListener("abort", () => reject(new Error("Upload cancelled.")));
        request.send(file);
    });
}

psElements.upload.addEventListener("click", async () => {
    const file = psElements.file.files?.[0];
    if (!file) return psShowMessage("Choose a .ps1 file.", true);
    if (file.size > 50 * 1024 ** 2) return psShowMessage("The script exceeds 50 MiB.", true);
    let name;
    try {
        name = psNormalizeName(psElements.name.value, file.name);
    } catch (error) {
        return psShowMessage(error.message, true);
    }
    psElements.upload.disabled = true;
    psElements.progressRow.hidden = false;
    psElements.progress.value = 0;
    try {
        await psUploadFile(file, name);
        psShowMessage(`${name} uploaded.`);
        psElements.file.value = "";
        psElements.name.value = "";
        psElements.arguments.value = "";
        await psRefresh();
    } catch (error) {
        psShowMessage(error.message, true);
    } finally {
        psElements.upload.disabled = false;
        psElements.progressRow.hidden = true;
    }
});

psElements.cancel.addEventListener("click", () => activeUpload?.abort());
psElements.refresh.addEventListener("click", psRefresh);
psElements.file.addEventListener("change", () => {
    psElements.name.placeholder = psElements.file.files?.[0]?.name || "File name (optional)";
});

psRefresh();
