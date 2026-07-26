const languageHost = document.querySelector("#login-language");
if (languageHost && window.IronI18n) {
    languageHost.replaceChildren(window.IronI18n.makeSwitcher("login-language"));
    languageHost.firstElementChild.classList.remove("login-language");
    languageHost.className = "login-language";
}
