const navigationItems = [
    {
        page: "dashboard",
        permission: "dashboard",
        label: "Dashboard",
        href: "/",
        icon: '<path d="M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z"></path>',
    },
    {
        page: "images",
        permission: "images",
        label: "Windows images",
        href: "/images",
        icon: '<path d="M4 5h16v14H4z"></path><path d="M4 9h16M8 15h4"></path>',
    },
    {
        page: "programs",
        permission: "programs",
        label: "Post-install software",
        href: "/programs",
        icon: '<path d="M12 3v10"></path><path d="m8 9 4 4 4-4"></path><rect x="4" y="16" width="16" height="5" rx="1"></rect>',
    },
    {
        page: "post-powershell",
        permission: "post_powershell",
        label: "Post-PowerShell",
        href: "/post-powershell",
        icon: '<path d="M5 4h14v16H5z"></path><path d="m8 9 3 3-3 3M13 15h3"></path>',
    },
    {
        page: "drivers",
        permission: "drivers",
        label: "Drivers",
        href: "/drivers",
        icon: '<path d="M5 4h14v16H5z"></path><path d="M9 8h6M9 12h6M9 16h3"></path>',
    },
    {
        page: "image-config",
        permission: "image_config",
        label: "Image settings",
        href: "/image-config",
        icon: '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1z"></path>',
    },
    {
        page: "users",
        superadmin: true,
        label: "Users",
        href: "/users",
        icon: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M19 8v6M22 11h-6"></path>',
    },
    {
        page: "access-control",
        superadmin: true,
        label: "Access control",
        href: "/access-control",
        icon: '<rect x="3" y="11" width="18" height="10" rx="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4M12 15v2"></path>',
    },
    {
        page: "info",
        superadmin: true,
        label: "Info",
        href: "/info",
        icon: '<circle cx="12" cy="12" r="9"></circle><path d="M12 11v6M12 7h.01"></path>',
    },
];
const navText = (value) => window.IronI18n?.t(value) || value;

const NAVIGATION_USER_CACHE_KEY = "irondeploy-navigation-user";
const SIDEBAR_COLLAPSED_KEY = "irondeploy-sidebar-collapsed";

function readCachedUser() {
    try {
        const payload = JSON.parse(sessionStorage.getItem(NAVIGATION_USER_CACHE_KEY));
        return payload && typeof payload.username === "string" ? payload : null;
    } catch {
        return null;
    }
}

function cacheUser(currentUser) {
    try {
        sessionStorage.setItem(NAVIGATION_USER_CACHE_KEY, JSON.stringify(currentUser));
    } catch {
        // Navigation still works when session storage is unavailable.
    }
}

function clearCachedUser() {
    try {
        sessionStorage.removeItem(NAVIGATION_USER_CACHE_KEY);
    } catch {
        // Ignore unavailable storage while redirecting to login.
    }
}

function userSignature(currentUser) {
    if (!currentUser) return "";
    return JSON.stringify({
        username: currentUser.username,
        superadmin: Boolean(currentUser.superadmin),
        permissions: [...(currentUser.permissions || [])].sort(),
    });
}

function sidebarIsCollapsed() {
    try {
        return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
    } catch {
        return false;
    }
}

function applyInitialSidebarState() {
    document.documentElement.classList.add("navigation-initializing");
    document.documentElement.classList.toggle("sidebar-collapsed", sidebarIsCollapsed());
}

function finishInitialNavigation() {
    window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
            document.documentElement.classList.remove("navigation-initializing");
        });
    });
}

function renderNavigation(currentUser) {
    document.querySelector(".app-sidebar")?.remove();
    const granted = new Set(currentUser?.permissions || []);
    const activePage = document.body.dataset.page || "dashboard";
    const sidebar = document.createElement("aside");
    sidebar.className = "app-sidebar";
    const head = document.createElement("div");
    head.className = "sidebar-head";
    const brand = document.createElement("a");
    brand.className = "sidebar-brand";
    brand.href = "/";
    brand.textContent = "IronDeploy";
    const toggle = document.createElement("button");
    toggle.className = "sidebar-toggle";
    toggle.type = "button";
    toggle.title = navText("Collapse navigation");
    toggle.setAttribute("aria-label", toggle.title);
    toggle.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6"></path></svg>';
    head.append(brand, toggle);

    const nav = document.createElement("nav");
    nav.className = "sidebar-nav";
    nav.setAttribute("aria-label", navText("Main navigation"));
    if (currentUser) {
        for (const item of navigationItems) {
            if (!currentUser.superadmin && (item.superadmin || !granted.has(item.permission))) {
                continue;
            }
            const link = document.createElement("a");
            link.className = `sidebar-link${item.page === activePage ? " is-active" : ""}`;
            link.href = item.href;
            link.title = navText(item.label);
            link.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${item.icon}</svg><span class="sidebar-label"></span>`;
            link.querySelector(".sidebar-label").textContent = navText(item.label);
            nav.append(link);
        }
    }
    const language = window.IronI18n?.makeSwitcher("sidebar-language");
    const footer = document.createElement("div");
    footer.className = "sidebar-footer";
    const identity = document.createElement("div");
    identity.className = "sidebar-identity";
    identity.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0 1 16 0"></path></svg><span class="sidebar-label"></span>';
    identity.querySelector(".sidebar-label").textContent = currentUser?.username || navText("Loading...");
    const logout = document.createElement("button");
    logout.className = "sidebar-logout";
    logout.type = "button";
    logout.title = navText("Sign out");
    logout.setAttribute("aria-label", logout.title);
    logout.hidden = !currentUser;
    logout.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 17l5-5-5-5M15 12H3M14 3h5a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-5"></path></svg>';
    logout.addEventListener("click", async () => {
        await fetch("/api/auth/logout", {
            method: "POST",
            headers: { "x-requested-with": "IronDeploy" },
            credentials: "same-origin",
        });
        clearCachedUser();
        window.location.replace("/login");
    });
    footer.append(identity, logout);
    sidebar.append(head, nav);
    if (language) sidebar.append(language);
    sidebar.append(footer);
    document.body.prepend(sidebar);

    const initiallyCollapsed = document.documentElement.classList.contains("sidebar-collapsed");
    toggle.title = navText(initiallyCollapsed ? "Expand navigation" : "Collapse navigation");
    toggle.setAttribute("aria-label", toggle.title);
    toggle.addEventListener("click", () => {
        const collapsed = document.documentElement.classList.toggle("sidebar-collapsed");
        try {
            localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
        } catch {
            // Keep the current-page state even when storage is unavailable.
        }
        toggle.title = navText(collapsed ? "Expand navigation" : "Collapse navigation");
        toggle.setAttribute("aria-label", toggle.title);
    });
}

async function buildNavigation() {
    const cachedUser = readCachedUser();
    renderNavigation(cachedUser);
    if (cachedUser) finishInitialNavigation();

    const response = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (!response.ok) {
        clearCachedUser();
        window.location.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
        return;
    }

    const auth = await response.json();
    const currentUser = auth.user;
    cacheUser(currentUser);
    if (userSignature(currentUser) !== userSignature(cachedUser)) {
        renderNavigation(currentUser);
    }
    if (!cachedUser) finishInitialNavigation();
}

function startNavigation() {
    buildNavigation().catch(() => {
        clearCachedUser();
        window.location.replace("/login");
    });
}

applyInitialSidebarState();
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startNavigation, { once: true });
} else {
    startNavigation();
}
