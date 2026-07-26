const form = document.querySelector("#login-form");
const button = document.querySelector("#login-button");
const message = document.querySelector("#login-message");

function safeNextPath() {
    const value = new URLSearchParams(window.location.search).get("next") || "/";
    return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function landingPath(user) {
    const desired = safeNextPath();
    const required = {
        "/": "dashboard",
        "/images": "images",
        "/programs": "programs",
        "/drivers": "drivers",
        "/image-config": "image_config",
        "/users": "superadmin",
        "/access-control": "superadmin",
    }[desired];
    if (user.superadmin || !required || user.permissions.includes(required)) return desired;
    const fallback = [
        ["dashboard", "/"],
        ["images", "/images"],
        ["programs", "/programs"],
        ["drivers", "/drivers"],
        ["image_config", "/image-config"],
    ].find(([permission]) => user.permissions.includes(permission));
    return fallback ? fallback[1] : "/login";
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.hidden = true;
    button.disabled = true;
    try {
        const response = await fetch("/api/auth/login", {
            method: "POST",
            headers: { "content-type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({
                username: document.querySelector("#login-username").value,
                password: document.querySelector("#login-password").value,
            }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "Sign-in failed.");
        window.location.replace(landingPath(payload.user));
    } catch (error) {
        message.textContent = error.message;
        message.hidden = false;
        document.querySelector("#login-password").select();
    } finally {
        button.disabled = false;
    }
});
