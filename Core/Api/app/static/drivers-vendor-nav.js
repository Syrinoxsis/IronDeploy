// Additive UI behavior only — does not touch drivers.js or any API call.
// Lets an operator click a vendor in the rail to filter the package list
// down to that vendor's packages, and nudges the upload form's vendor
// select to match. Re-applies itself whenever drivers.js re-renders the
// vendor or package lists (create/rename/delete/refresh).
(function () {
    const vendorList = document.querySelector("#vendor-list");
    const packageList = document.querySelector("#package-list");
    const vendorSelect = document.querySelector("#package-vendor");
    if (!vendorList || !packageList) return;

    let activeVendor = null;

    function vendorNameOf(row) {
        return row.querySelector(".vendor-name")?.textContent || "";
    }

    function applyFilter() {
        packageList.querySelectorAll(".package-card").forEach((card) => {
            const badge = card.querySelector(".vendor-badge")?.textContent || "";
            card.hidden = Boolean(activeVendor) && badge !== activeVendor;
        });
        vendorList.querySelectorAll(".vendor-row").forEach((row) => {
            row.classList.toggle(
                "is-active",
                Boolean(activeVendor) && vendorNameOf(row) === activeVendor
            );
        });
    }

    vendorList.addEventListener("click", (event) => {
        if (event.target.closest("button")) return;
        const row = event.target.closest(".vendor-row");
        if (!row) return;
        const name = vendorNameOf(row);
        activeVendor = activeVendor === name ? null : name;
        if (activeVendor && vendorSelect) {
            const option = [...vendorSelect.options].find(
                (opt) => opt.value === activeVendor
            );
            if (option) vendorSelect.value = activeVendor;
        }
        applyFilter();
    });

    new MutationObserver(applyFilter).observe(vendorList, { childList: true });
    new MutationObserver(applyFilter).observe(packageList, { childList: true });
})();
