const adminPage = document.body.dataset.page;
const toast = document.querySelector("#admin-toast");
let toastTimer = null;
const adminText = (value) => window.IronI18n?.t(value) || value;

function showToast(text, type = "success") {
    if (toastTimer) window.clearTimeout(toastTimer);
    toast.hidden = false;
    toast.className = `admin-toast ${type}`;
    toast.textContent = text;
    if (type === "success") {
        toastTimer = window.setTimeout(() => { toast.hidden = true; }, 4000);
    }
}

async function adminFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set("content-type", "application/json");
    if (options.method && options.method !== "GET") {
        headers.set("x-requested-with", "IronDeploy");
    }
    const response = await fetch(url, { ...options, headers, credentials: "same-origin" });
    if (response.status === 401) {
        window.location.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        throw new Error("Login required.");
    }
    const text = await response.text();
    let payload = {};
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = {}; }
    if (!response.ok) throw new Error(payload.detail || text || `HTTP ${response.status}`);
    return payload;
}

function badge(text, className = "") {
    const result = document.createElement("span");
    result.className = `account-status ${className}`;
    result.textContent = text;
    return result;
}

function userIdentity(user) {
    const wrapper = document.createElement("div");
    wrapper.className = "account-identity";
    const name = document.createElement("strong");
    name.textContent = user.username;
    const labels = document.createElement("div");
    labels.className = "account-labels";
    labels.append(
        user.superadmin
            ? badge(adminText("Superadmin"), "superadmin")
            : badge(adminText(user.active ? "Active" : "Blocked"), user.active ? "active" : "blocked")
    );
    wrapper.append(name, labels);
    return wrapper;
}

async function loadUsersPage() {
    const payload = await adminFetch("/api/admin/users");
    const list = document.querySelector("#users-list");
    document.querySelector("#user-count").textContent = `${payload.users.length} account${payload.users.length === 1 ? "" : "s"}`;
    const cards = payload.users.map((user) => {
        const card = document.createElement("article");
        card.className = "user-row";
        card.append(userIdentity(user));
        if (user.superadmin) {
            const note = document.createElement("span");
            note.className = "managed-note";
            note.textContent = adminText("Credentials managed in SetupWeb");
            card.append(note);
            return card;
        }

        const controls = document.createElement("div");
        controls.className = "user-controls";
        const password = document.createElement("input");
        password.type = "password";
        password.minLength = 12;
        password.placeholder = adminText("New password (12+ characters)");
        password.autocomplete = "new-password";
        const reset = document.createElement("button");
        reset.className = "admin-secondary";
        reset.type = "button";
        reset.textContent = adminText("Reset password");
        reset.addEventListener("click", async () => {
            if (password.value.length < 12) {
                showToast("Password must contain at least 12 characters.", "error");
                password.focus();
                return;
            }
            reset.disabled = true;
            try {
                await adminFetch(`/api/admin/users/${user.id}/password`, {
                    method: "POST", body: JSON.stringify({ password: password.value }),
                });
                password.value = "";
                showToast(`Password for ${user.username} changed. Active sessions were closed.`);
            } catch (error) { showToast(error.message, "error"); }
            finally { reset.disabled = false; }
        });
        const active = document.createElement("button");
        active.className = user.active ? "admin-danger" : "admin-secondary";
        active.type = "button";
        active.textContent = adminText(user.active ? "Block user" : "Enable user");
        active.addEventListener("click", async () => {
            active.disabled = true;
            try {
                await adminFetch(`/api/admin/users/${user.id}/active`, {
                    method: "POST", body: JSON.stringify({ active: !user.active }),
                });
                showToast(`${user.username} ${user.active ? "blocked" : "enabled"}.`);
                await loadUsersPage();
            } catch (error) { showToast(error.message, "error"); active.disabled = false; }
        });
        controls.append(password, reset, active);
        card.append(controls);
        return card;
    });
    list.replaceChildren(...cards);
}

const accessElements = adminPage === "access-control" ? {
    tabs: [...document.querySelectorAll("[data-access-tab]")],
    panels: [...document.querySelectorAll("[data-access-panel]")],
    refresh: document.querySelector("#access-refresh"),
    save: document.querySelector("#access-save-button"),
    dirtyState: document.querySelector("#access-dirty-state"),
    dirtyCopy: document.querySelector("#access-dirty-copy"),
    currentPolicySection: document.querySelector("#current-policy-section"),
    currentPolicy: document.querySelector("#current-policy"),
    permissionNote: document.querySelector("#permission-note"),
    userSelect: document.querySelector("#access-user-select"),
    selectedUserState: document.querySelector("#selected-user-state"),
    permissionList: document.querySelector("#access-list"),
    pin: document.querySelector("#winpe-pin"),
    pinEditor: document.querySelector("#pin-editor"),
    pinStatus: document.querySelector("#winpe-pin-status"),
    authModes: [...document.querySelectorAll('input[name="winpe-auth-mode"]')],
} : null;

let accessTab = "authorization";
let authorizationDirty = false;
let permissionsDirty = false;
let currentPolicy = null;
let regularUsers = [];
let permissionEntries = [];
let selectedUserId = null;
let permissionCheckboxes = [];

const permissionDescriptions = {
    dashboard: "View deployments and computer inventory.",
    images: "Manage Windows deployment images.",
    programs: "Manage post-install software.",
    drivers: "Manage driver packages.",
    image_config: "Change deployed Windows defaults.",
    deploy: "Authorize one WinPE deployment at a time.",
};

const permissionIcons = {
    dashboard: '<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"></path>',
    images: '<path d="M4 5h16v14H4z"></path><path d="M4 9h16M8 15h4"></path>',
    programs: '<path d="M12 3v10"></path><path d="m8 9 4 4 4-4"></path><rect x="4" y="16" width="16" height="5" rx="1"></rect>',
    drivers: '<path d="M5 4h14v16H5z"></path><path d="M9 8h6M9 12h6M9 16h3"></path>',
    image_config: '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"></path>',
    deploy: '<rect x="4" y="10" width="16" height="11" rx="2"></rect><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"></path>',
};

function policyLabel(mode) {
    return {
        account: "Username and password",
        pin: "PIN code",
        none: "No operator authorization",
    }[mode] || "Not configured";
}

function renderAccessRail() {
    const dirty = accessTab === "authorization" ? authorizationDirty : permissionsDirty;
    accessElements.dirtyState.classList.toggle("is-dirty", dirty);
    accessElements.dirtyState.classList.toggle("is-clean", !dirty);
    accessElements.dirtyState.textContent = dirty
        ? adminText("Unsaved changes")
        : adminText("All changes saved");
    accessElements.dirtyCopy.textContent = dirty
        ? adminText("You have unsaved changes.")
        : adminText("Settings match the saved configuration.");
    accessElements.save.disabled = !dirty;
    accessElements.save.textContent = adminText(
        accessTab === "authorization" ? "Save authorization" : "Save access"
    );
    accessElements.currentPolicySection.hidden = accessTab !== "authorization";
    accessElements.permissionNote.hidden = accessTab !== "permissions";
}

function setAccessDirty(kind, dirty) {
    if (kind === "authorization") authorizationDirty = dirty;
    else permissionsDirty = dirty;
    renderAccessRail();
}

function showAccessTab(tab) {
    accessTab = tab;
    for (const button of accessElements.tabs) {
        const active = button.dataset.accessTab === tab;
        button.classList.toggle("is-active", active);
        if (active) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
    }
    for (const panel of accessElements.panels) {
        panel.hidden = panel.dataset.accessPanel !== tab;
    }
    renderAccessRail();
}

function updatePinEditor() {
    const selected = accessElements.authModes.find((item) => item.checked);
    accessElements.pinEditor.hidden = selected?.value !== "pin";
}

function syncExclusiveDeploymentAccess(changed = null) {
    const deploymentCheckbox = permissionCheckboxes.find(
        (item) => item.value === "deploy"
    );
    if (!deploymentCheckbox) return;
    const browserCheckboxes = permissionCheckboxes.filter(
        (item) => item !== deploymentCheckbox
    );
    if (changed === deploymentCheckbox && deploymentCheckbox.checked) {
        browserCheckboxes.forEach((item) => { item.checked = false; });
    } else if (changed && changed.checked && changed !== deploymentCheckbox) {
        deploymentCheckbox.checked = false;
    }
    const deploymentOnly = deploymentCheckbox.checked;
    browserCheckboxes.forEach((item) => { item.disabled = deploymentOnly; });
    deploymentCheckbox.disabled = false;
}

function renderSelectedUser() {
    const user = regularUsers.find((item) => String(item.id) === String(selectedUserId));
    if (!user) {
        accessElements.selectedUserState.textContent = "";
        accessElements.permissionList.replaceChildren();
        permissionCheckboxes = [];
        return;
    }

    accessElements.userSelect.value = String(user.id);
    accessElements.selectedUserState.classList.toggle("is-blocked", !user.active);
    accessElements.selectedUserState.textContent = adminText(
        user.active ? "Active account" : "Blocked account"
    );

    permissionCheckboxes = permissionEntries.map(([permission, label]) => {
        const row = document.createElement("label");
        row.className = `permission-row${permission === "deploy" ? " deploy-only" : ""}`;

        const icon = document.createElement("span");
        icon.className = "permission-icon";
        icon.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${permissionIcons[permission] || ""}</svg>`;

        const copy = document.createElement("span");
        copy.className = "permission-copy";
        const strong = document.createElement("strong");
        strong.textContent = adminText(label);
        const small = document.createElement("small");
        small.textContent = adminText(permissionDescriptions[permission] || "");
        copy.append(strong, small);

        const control = document.createElement("span");
        control.className = "permission-switch";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = permission;
        checkbox.checked = user.permissions.includes(permission);
        checkbox.setAttribute("aria-label", adminText(label));
        const track = document.createElement("span");
        track.className = "permission-switch-track";
        track.setAttribute("aria-hidden", "true");
        control.append(checkbox, track);

        checkbox.addEventListener("change", () => {
            syncExclusiveDeploymentAccess(checkbox);
            setAccessDirty("permissions", true);
        });
        row.append(icon, copy, control);
        return { row, checkbox };
    });

    accessElements.permissionList.replaceChildren(
        ...permissionCheckboxes.map((item) => item.row),
        Object.assign(document.createElement("p"), {
            className: "permission-exclusivity",
            textContent: adminText(
                "WinPE deployment only cannot be combined with page access."
            ),
        })
    );
    permissionCheckboxes = permissionCheckboxes.map((item) => item.checkbox);
    syncExclusiveDeploymentAccess();
    setAccessDirty("permissions", false);
}

async function loadAccessPage() {
    const payload = await adminFetch("/api/admin/users");
    regularUsers = payload.users.filter((user) => !user.superadmin);
    permissionEntries = Object.entries(payload.permissions);
    const previousUserId = selectedUserId;
    selectedUserId = regularUsers.some(
        (user) => String(user.id) === String(previousUserId)
    ) ? previousUserId : regularUsers[0]?.id ?? null;

    accessElements.userSelect.replaceChildren(
        ...regularUsers.map((user) => {
            const option = document.createElement("option");
            option.value = String(user.id);
            option.textContent = user.username;
            return option;
        })
    );
    accessElements.userSelect.disabled = regularUsers.length === 0;
    if (!regularUsers.length) {
        const empty = document.createElement("div");
        empty.className = "admin-empty";
        empty.textContent = adminText("Create a regular user first.");
        accessElements.permissionList.replaceChildren(empty);
        return;
    }
    renderSelectedUser();
}

async function loadWinPEAuth() {
    currentPolicy = await adminFetch("/api/admin/winpe-auth");
    const selected = accessElements.authModes.find(
        (item) => item.value === currentPolicy.mode
    );
    if (selected) selected.checked = true;
    accessElements.pin.value = "";
    accessElements.pinStatus.textContent = adminText(
        currentPolicy.pinConfigured ? "Configured" : "Not configured"
    );
    accessElements.currentPolicy.textContent = adminText(policyLabel(currentPolicy.mode));
    updatePinEditor();
    setAccessDirty("authorization", false);
}

async function saveWinPEAuth() {
    const selected = accessElements.authModes.find((item) => item.checked);
    if (!selected) return;
    if (accessElements.pin.value && !/^[0-9]{6,10}$/.test(accessElements.pin.value)) {
        showToast(adminText("PIN must contain 6-10 digits."), "error");
        accessElements.pin.focus();
        return;
    }
    if (
        selected.value === "none" &&
        !window.confirm(adminText(
            "Anyone who can boot this WinPE will be able to erase disk 0. Enable deployment without operator authorization?"
        ))
    ) {
        return;
    }
    accessElements.save.disabled = true;
    try {
        await adminFetch("/api/admin/winpe-auth", {
            method: "PUT",
            body: JSON.stringify({
                mode: selected.value,
                pin: accessElements.pin.value || null,
            }),
        });
        showToast(adminText("WinPE authorization settings saved."));
        await loadWinPEAuth();
    } catch (error) {
        showToast(error.message, "error");
    } finally {
        renderAccessRail();
    }
}

async function saveSelectedUserAccess() {
    const user = regularUsers.find((item) => String(item.id) === String(selectedUserId));
    if (!user) return;
    accessElements.save.disabled = true;
    try {
        const permissions = permissionCheckboxes
            .filter((item) => item.checked)
            .map((item) => item.value);
        await adminFetch(`/api/admin/users/${user.id}/permissions`, {
            method: "PUT",
            body: JSON.stringify({ permissions }),
        });
        user.permissions = permissions;
        setAccessDirty("permissions", false);
        showToast(`Access for ${user.username} saved. Active sessions were closed.`);
    } catch (error) {
        showToast(error.message, "error");
    } finally {
        renderAccessRail();
    }
}

async function refreshAccessControl() {
    if (
        (authorizationDirty || permissionsDirty) &&
        !window.confirm(adminText("Discard unsaved changes?"))
    ) {
        return;
    }
    accessElements.refresh.disabled = true;
    accessElements.refresh.classList.add("is-loading");
    try {
        await Promise.all([loadWinPEAuth(), loadAccessPage()]);
    } catch (error) {
        showToast(error.message, "error");
    } finally {
        accessElements.refresh.disabled = false;
        accessElements.refresh.classList.remove("is-loading");
    }
}

if (adminPage === "users") {
    document.querySelector("#create-user-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const username = document.querySelector("#new-username");
        const password = document.querySelector("#new-password");
        try {
            await adminFetch("/api/admin/users", {
                method: "POST",
                body: JSON.stringify({ username: username.value, password: password.value }),
            });
            showToast(`${username.value} created.`);
            username.value = "";
            password.value = "";
            await loadUsersPage();
        } catch (error) { showToast(error.message, "error"); }
    });
    loadUsersPage().catch((error) => showToast(error.message, "error"));
} else if (adminPage === "access-control") {
    for (const tab of accessElements.tabs) {
        tab.addEventListener("click", () => showAccessTab(tab.dataset.accessTab));
    }
    for (const mode of accessElements.authModes) {
        mode.addEventListener("change", () => {
            updatePinEditor();
            setAccessDirty("authorization", true);
        });
    }
    accessElements.pin.addEventListener("input", () => {
        setAccessDirty("authorization", true);
    });
    accessElements.userSelect.addEventListener("change", () => {
        if (
            permissionsDirty &&
            !window.confirm(adminText("Discard unsaved changes?"))
        ) {
            accessElements.userSelect.value = String(selectedUserId);
            return;
        }
        selectedUserId = accessElements.userSelect.value;
        renderSelectedUser();
    });
    accessElements.save.addEventListener("click", () => {
        if (accessTab === "authorization") saveWinPEAuth();
        else saveSelectedUserAccess();
    });
    accessElements.refresh.addEventListener("click", refreshAccessControl);
    showAccessTab("authorization");
    refreshAccessControl();
}
