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

async function loadAccessPage() {
    const payload = await adminFetch("/api/admin/users");
    const regularUsers = payload.users.filter((user) => !user.superadmin);
    const permissionEntries = Object.entries(payload.permissions);
    const cards = regularUsers.map((user) => {
        const card = document.createElement("article");
        card.className = "access-row";
        const head = document.createElement("div");
        head.className = "access-user-head";
        head.append(userIdentity(user));
        const save = document.createElement("button");
        save.className = "auth-primary";
        save.type = "button";
        save.textContent = adminText("Save access");
        head.append(save);
        const grid = document.createElement("div");
        grid.className = "permission-grid";
        const checkboxes = permissionEntries.map(([permission, label]) => {
            const option = document.createElement("label");
            option.className = "permission-option";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.value = permission;
            checkbox.checked = user.permissions.includes(permission);
            const copy = document.createElement("span");
            const strong = document.createElement("strong");
            strong.textContent = adminText(label);
            const small = document.createElement("small");
            small.textContent = permission === "deploy"
                ? "Allows WinPE login for one deployment at a time; cannot be combined with any other access."
                : `Allows the ${label} page and its backend API.`;
            copy.append(strong, small);
            option.append(checkbox, copy);
            grid.append(option);
            return checkbox;
        });
        const deploymentCheckbox = checkboxes.find((item) => item.value === "deploy");
        const syncExclusiveDeploymentAccess = (changed = null) => {
            if (!deploymentCheckbox) return;
            const browserCheckboxes = checkboxes.filter((item) => item !== deploymentCheckbox);
            if (changed === deploymentCheckbox && deploymentCheckbox.checked) {
                browserCheckboxes.forEach((item) => { item.checked = false; });
            } else if (changed && changed.checked && changed !== deploymentCheckbox) {
                deploymentCheckbox.checked = false;
            }
            const deploymentOnly = deploymentCheckbox.checked;
            browserCheckboxes.forEach((item) => { item.disabled = deploymentOnly; });
            deploymentCheckbox.disabled = browserCheckboxes.some((item) => item.checked);
        };
        checkboxes.forEach((checkbox) => {
            checkbox.addEventListener("change", () => syncExclusiveDeploymentAccess(checkbox));
        });
        syncExclusiveDeploymentAccess();
        save.addEventListener("click", async () => {
            save.disabled = true;
            try {
                await adminFetch(`/api/admin/users/${user.id}/permissions`, {
                    method: "PUT",
                    body: JSON.stringify({
                        permissions: checkboxes.filter((item) => item.checked).map((item) => item.value),
                    }),
                });
                showToast(`Access for ${user.username} saved. Active sessions were closed.`);
            } catch (error) { showToast(error.message, "error"); }
            finally { save.disabled = false; }
        });
        card.append(head, grid);
        return card;
    });
    const list = document.querySelector("#access-list");
    if (cards.length) list.replaceChildren(...cards);
    else {
        const empty = document.createElement("div");
        empty.className = "admin-empty";
        empty.textContent = adminText("Create a regular user first.");
        list.replaceChildren(empty);
    }
}

async function loadWinPEAuth() {
    const policy = await adminFetch("/api/admin/winpe-auth");
    const selected = document.querySelector(
        `input[name="winpe-auth-mode"][value="${policy.mode}"]`
    );
    if (selected) selected.checked = true;
    document.querySelector("#winpe-pin-status").textContent = adminText(
        policy.pinConfigured ? "PIN is configured" : "PIN is not configured"
    );
    document.querySelector("#winpe-pin").placeholder = adminText(
        "Leave blank to keep the configured PIN"
    );
}

async function saveWinPEAuth() {
    const save = document.querySelector("#save-winpe-auth");
    const selected = document.querySelector('input[name="winpe-auth-mode"]:checked');
    const pin = document.querySelector("#winpe-pin");
    if (!selected) return;
    if (pin.value && !/^[0-9]{6,10}$/.test(pin.value)) {
        showToast(adminText("PIN must contain 6-10 digits."), "error");
        pin.focus();
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
    save.disabled = true;
    try {
        await adminFetch("/api/admin/winpe-auth", {
            method: "PUT",
            body: JSON.stringify({ mode: selected.value, pin: pin.value || null }),
        });
        pin.value = "";
        showToast(adminText("WinPE authorization settings saved."));
        await loadWinPEAuth();
    } catch (error) {
        showToast(error.message, "error");
    } finally {
        save.disabled = false;
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
    document.querySelector("#save-winpe-auth").addEventListener("click", saveWinPEAuth);
    loadWinPEAuth().catch((error) => showToast(error.message, "error"));
    loadAccessPage().catch((error) => showToast(error.message, "error"));
}
