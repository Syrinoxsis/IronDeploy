const AUTO_REFRESH_MS = 10_000;

const STAGE_LABELS = {
    disk_partitioning: "Disk partitioning",
    image_apply: "Apply image",
    driver_injection: "Driver injection",
    deployment_state: "Save deployment state",
    unattend_generation: "Generate unattend.xml",
    unattend_apply: "Apply unattend.xml",
    domain_join: "Domain join",
    postinstall_copy: "Copy post-install",
    boot_files: "Create boot files",
    windows_setup: "Start Windows setup",
};

const VIEW_META = {
    deployments: {
        title: "Deployments",
        empty: "No deployments yet",
        filteredEmpty: "No deployments match the current filters",
    },
    computers: {
        title: "Computers",
        empty: "No computers yet",
        filteredEmpty: "No computers match the current search",
    },
};

const state = {
    view: "deployments",
    deployments: [],
    computers: [],
    totals: {
        deployments: null,
        computers: null,
    },
    deploymentSummary: {
        total: null,
        begin: null,
        completed: null,
        failed: null,
    },
    status: "all",
    search: "",
    lastUpdated: null,
    expandedProgramDeployments: new Set(),
};

const elements = {
    refreshButton: document.querySelector("#refresh-button"),
    updatedAt: document.querySelector("#updated-at"),
    headerPageTitle: document.querySelector(".header-page-title"),
    viewTabs: [...document.querySelectorAll(".view-tab")],
    deploymentSummary: document.querySelector("#deployment-summary"),
    totalCount: document.querySelector("#total-count"),
    beginCount: document.querySelector("#begin-count"),
    completedCount: document.querySelector("#completed-count"),
    failedCount: document.querySelector("#failed-count"),
    searchInput: document.querySelector("#search-input"),
    summaryItems: [...document.querySelectorAll(".summary-item")],
    tableRegion: document.querySelector("#table-region"),
    table: document.querySelector("#table-region table"),
    head: document.querySelector("#table-head"),
    rows: document.querySelector("#table-rows"),
    message: document.querySelector("#table-message"),
    visibleCount: document.querySelector("#visible-count"),
    copyState: document.querySelector("#copy-state"),
    syncDot: document.querySelector(".sync-dot"),
};

function formatDate(value) {
    if (!value) {
        return "-";
    }

    return new Intl.DateTimeFormat(
        window.IronI18n?.language === "ru" ? "ru-RU" : "en-US",
        {
        dateStyle: "short",
        timeStyle: "medium",
        }
    ).format(new Date(value));
}

function formatBoolean(value) {
    return value ? "Yes" : "No";
}

function normalizedText(value) {
    return String(value ?? "").toLocaleLowerCase("ru-RU");
}

function dashboardText(value) {
    return window.IronI18n?.t(value) || value;
}

function createHeader(labels) {
    elements.head.replaceChildren();
    for (const label of labels) {
        const header = document.createElement("th");
        header.scope = "col";
        header.textContent = label;
        elements.head.append(header);
    }
}

function createCell(value, className = "") {
    const cell = document.createElement("td");
    const text = value === null || value === undefined || value === "" ? "-" : String(value);
    cell.textContent = text;
    cell.dataset.copyValue = text;
    cell.title = "Click to copy";
    if (className) {
        cell.className = className;
    }
    return cell;
}

function createDeploymentLinkCell(deploymentId, computerName) {
    const cell = document.createElement("td");
    cell.className = "deployment-primary";
    cell.dataset.copyValue = computerName || String(deploymentId);
    const link = document.createElement("a");
    link.className = "deployment-detail-link";
    link.href = `/dashboard/${deploymentId}`;
    link.title = `Open deployment #${deploymentId}`;
    const label = document.createElement("span");
    label.textContent = computerName || `#${deploymentId}`;
    const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    arrow.setAttribute("viewBox", "0 0 24 24");
    arrow.setAttribute("aria-hidden", "true");
    arrow.innerHTML = '<path d="m9 18 6-6-6-6"></path>';
    link.append(label, arrow);
    cell.append(link);
    return cell;
}

function copyTextFallback(value) {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.readOnly = true;
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.position = "fixed";
    textarea.style.inset = "-9999px auto auto -9999px";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);
    let copied = false;
    try {
        copied = document.execCommand("copy");
    } catch {
        copied = false;
    } finally {
        textarea.remove();
    }
    return copied;
}

async function copyText(value) {
    if (!value || value === "-") {
        return;
    }

    let copied = false;
    if (window.isSecureContext && navigator.clipboard?.writeText) {
        try {
            await navigator.clipboard.writeText(value);
            copied = true;
        } catch {
            copied = copyTextFallback(value);
        }
    } else {
        // navigator.clipboard is normally blocked on an HTTP address that is
        // not localhost. execCommand still works during the user's click.
        copied = copyTextFallback(value);
    }

    if (copied) {
        elements.copyState.textContent = `Copied: ${value}`;
        window.setTimeout(() => {
            elements.copyState.textContent = "Click any cell to copy";
        }, 1800);
    } else {
        elements.copyState.textContent = "Clipboard unavailable";
    }
}

function createStatusCell(status) {
    const cell = document.createElement("td");
    cell.dataset.copyValue = status;
    cell.title = "Click to copy";
    const statusElement = document.createElement("span");
    const statuses = {
        begin: ["status-begin", "Running"],
        completed: ["status-completed", "Completed"],
        failed: ["status-failed", "Failed"],
    };
    const [className, label] = statuses[status] ?? statuses.begin;
    statusElement.className = `status ${className}`;
    statusElement.textContent = label;
    cell.append(statusElement);
    return cell;
}

function formatDuration(startedAt, completedAt = null) {
    const started = new Date(startedAt).getTime();
    const finished = completedAt ? new Date(completedAt).getTime() : Date.now();
    const totalSeconds = Math.max(0, Math.floor((finished - started) / 1000));

    return formatSeconds(totalSeconds);
}

function formatSeconds(value) {
    const totalSeconds = Math.max(0, Math.floor(value));

    if (totalSeconds < 60) {
        return `${totalSeconds}s`;
    }

    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    if (hours > 0) {
        return `${hours}h ${minutes}m ${seconds}s`;
    }
    return `${minutes}m ${seconds}s`;
}

function createDuration(stage, className = "") {
    const duration = document.createElement("span");
    duration.className = className;

    if (stage.status === "skipped") {
        duration.textContent = "Skipped";
        return duration;
    }

    duration.textContent = formatDuration(stage.started_at, stage.completed_at);
    if (!stage.completed_at) {
        duration.dataset.startedAt = stage.started_at;
    }
    return duration;
}

function createDeploymentDurationCell(deployment) {
    const cell = createCell(
        formatDuration(deployment.started_at, deployment.completed_at),
        "mono deployment-duration",
    );
    if (!deployment.completed_at) {
        cell.dataset.startedAt = deployment.started_at;
    }
    return cell;
}

function createSerialNumberCell(value) {
    const cell = createCell(
        value,
        value ? "mono serial-number" : "mono serial-number muted-value",
    );
    if (value) {
        const serialNumber = String(value);
        cell.replaceChildren();
        if (serialNumber.length > 18) {
            const preview = document.createElement("span");
            preview.className = "serial-preview";

            const start = document.createElement("span");
            start.textContent = serialNumber.slice(0, 7);

            const marker = document.createElement("span");
            marker.className = "serial-overflow-marker";
            marker.textContent = "…";
            const hiddenCount = serialNumber.length - 12;
            marker.title = `${hiddenCount} ${dashboardText("characters hidden")}`;

            const end = document.createElement("span");
            end.textContent = serialNumber.slice(-5);
            preview.append(start, marker, end);
            cell.append(preview);
        } else {
            cell.textContent = serialNumber;
        }
        cell.dataset.copyValue = serialNumber;
        cell.title = `${serialNumber}\n${dashboardText("Click to copy")}`;
        cell.setAttribute(
            "aria-label",
            `${dashboardText("Serial number")}: ${serialNumber}. ` +
                dashboardText("Click to copy"),
        );
    }
    return cell;
}

function createProgramsCell(deployment) {
    const cell = document.createElement("td");
    cell.className = "programs-cell";
    const programs = deployment.programs ?? [];

    if (programs.length === 0) {
        cell.textContent = "No software report";
        cell.dataset.copyValue = cell.textContent;
        cell.classList.add("muted-value");
        return cell;
    }

    const counts = { installed: 0, failed: 0, timed_out: 0 };
    for (const program of programs) {
        counts[program.status] = (counts[program.status] ?? 0) + 1;
    }

    const details = document.createElement("details");
    details.className = "program-details";
    details.open = state.expandedProgramDeployments.has(deployment.deployment_id);
    details.addEventListener("toggle", () => {
        if (details.open) {
            state.expandedProgramDeployments.add(deployment.deployment_id);
        } else {
            state.expandedProgramDeployments.delete(deployment.deployment_id);
        }
    });
    const summary = document.createElement("summary");
    summary.textContent = [
        `${counts.installed} installed`,
        `${counts.failed} failed`,
        `${counts.timed_out} timed out`,
    ].join(" | ");

    const list = document.createElement("ul");
    list.className = "program-list";
    for (const program of programs) {
        const item = document.createElement("li");
        item.className = `program-item program-${program.status}`;

        const name = document.createElement("span");
        name.className = "program-name";
        name.textContent = program.name;

        const result = document.createElement("span");
        result.className = "program-result";
        const statusLabel = {
            installed: "Installed",
            failed: program.reason === "hash_mismatch" ? "SHA-256 mismatch" : "Failed",
            timed_out: "Timed out",
        }[program.status] ?? program.status;
        const exitCode = program.exit_code === null ? "" : `, code ${program.exit_code}`;
        result.textContent = `${statusLabel}${exitCode} | ${formatSeconds(program.duration_seconds)}`;

        item.append(name, result);
        const error = createErrorBlock(program.error_message);
        if (error) {
            item.append(error);
        }
        list.append(item);
    }

    details.append(summary, list);
    cell.dataset.copyValue = programs
        .map((program) => `${program.name}: ${program.status}`)
        .join("; ");
    cell.append(details);
    return cell;
}

function createErrorBlock(message) {
    if (!message) {
        return null;
    }
    const error = document.createElement("p");
    error.className = "error-message";
    error.textContent = message;
    return error;
}

function createStagesCell(deployment) {
    const cell = document.createElement("td");
    cell.className = "stages-cell";
    const stages = deployment.stages ?? [];
    const current = document.createElement("span");
    current.className = "stage-current";

    if (stages.length === 0) {
        const hasError = Boolean(deployment.last_error_message);
        current.classList.add(
            hasError ? "stage-current-failed" : "stage-current-pending",
        );
        current.textContent = hasError
            ? "Deployment failed"
            : "Waiting for stage data";
        cell.dataset.copyValue = current.textContent;
        cell.title = "Open deployment details for the full history";
        cell.append(current);
        return cell;
    }

    const currentStage =
        [...stages].reverse().find((stage) => stage.status === "running") ??
        [...stages].reverse().find((stage) => stage.status === "failed") ??
        stages.at(-1);
    const stageLabel = STAGE_LABELS[currentStage.stage] ?? currentStage.stage;

    if (deployment.status === "completed") {
        current.classList.add("stage-current-completed");
        current.textContent = "Completed";
    } else if (deployment.status === "failed" || currentStage.status === "failed") {
        current.classList.add("stage-current-failed");
        current.textContent = stageLabel;
    } else {
        current.classList.add("stage-current-running");
        const label = document.createElement("span");
        label.className = "stage-current-label";
        label.textContent = stageLabel;
        current.append(
            label,
            createDuration(currentStage, "stage-current-duration"),
        );
    }

    cell.dataset.copyValue = current.textContent;
    cell.title = "Open deployment details for the full history";
    cell.append(current);
    return cell;
}

function renderLiveDurations() {
    for (const duration of document.querySelectorAll("[data-started-at]")) {
        duration.textContent = formatDuration(duration.dataset.startedAt);
    }
}

function searchableDeployment(deployment) {
    return [
        deployment.deployment_id,
        deployment.computer_name,
        deployment.serial_number,
        deployment.model,
        deployment.mac_address,
        deployment.ip_address,
        deployment.image_name,
        deployment.domain_join ? "yes" : "no",
        deployment.status,
        deployment.last_error_message,
        ...(deployment.stages ?? []).flatMap((stage) => [
            stage.stage,
            stage.status,
            stage.error_message,
        ]),
        ...(deployment.programs ?? []).flatMap((program) => [
            program.name,
            program.status,
            program.exit_code,
            program.reason,
            program.error_message,
        ]),
    ];
}

function searchableComputer(computer) {
    return [
        computer.computer_id,
        computer.serial_number,
        computer.last_model,
        computer.mac_address,
        computer.last_computer_name,
        computer.last_ip_address,
        computer.last_image_name,
        computer.last_domain_join ? "yes" : "no",
        computer.deployment_count,
        computer.last_deployment_id,
    ];
}

function filteredDeployments() {
    const query = normalizedText(state.search.trim());

    return state.deployments.filter((deployment) => {
        if (state.status !== "all" && deployment.status !== state.status) {
            return false;
        }

        if (!query) {
            return true;
        }

        return searchableDeployment(deployment).some((value) =>
            normalizedText(value).includes(query),
        );
    });
}

function filteredComputers() {
    const query = normalizedText(state.search.trim());
    if (!query) {
        return state.computers;
    }

    return state.computers.filter((computer) =>
        searchableComputer(computer).some((value) =>
            normalizedText(value).includes(query),
        ),
    );
}

function renderDeploymentRows() {
    createHeader([
        "Computer",
        "Model",
        "Serial",
        "IP",
        "Image",
        "Status",
        "Current stage",
        "Duration",
        "Started",
    ]);
    elements.rows.replaceChildren();
    const deployments = filteredDeployments();
    elements.visibleCount.textContent =
        state.totals.deployments === null
            ? "Shown: -"
            : `Shown ${deployments.length} of ${state.totals.deployments}`;

    for (const deployment of deployments) {
        const row = document.createElement("tr");
        row.append(
            createDeploymentLinkCell(
                deployment.deployment_id,
                deployment.computer_name,
            ),
            createCell(deployment.model, deployment.model ? "" : "muted-value"),
            createSerialNumberCell(deployment.serial_number),
            createCell(
                deployment.ip_address,
                deployment.ip_address ? "mono" : "mono muted-value",
            ),
            createCell(deployment.image_name, deployment.image_name ? "" : "muted-value"),
            createStatusCell(deployment.status),
            createStagesCell(deployment),
            createDeploymentDurationCell(deployment),
            createCell(formatDate(deployment.started_at)),
        );
        elements.rows.append(row);
    }

    renderEmptyMessage(deployments.length, state.deployments.length);
}

function renderComputerRows() {
    createHeader([
        "ID",
        "Last name",
        "Serial",
        "Model",
        "MAC",
        "Last IP",
        "Last image",
        "Domain",
        "Deployments",
        "First seen",
        "Last seen",
        "Last deployment",
    ]);
    elements.rows.replaceChildren();
    const computers = filteredComputers();
    elements.visibleCount.textContent =
        state.totals.computers === null
            ? "Shown: -"
            : `Shown ${computers.length} of ${state.totals.computers}`;

    for (const computer of computers) {
        const row = document.createElement("tr");
        row.append(
            createCell(`#${computer.computer_id}`, "deployment-id mono"),
            createCell(computer.last_computer_name, "computer-name"),
            createSerialNumberCell(computer.serial_number),
            createCell(computer.last_model, computer.last_model ? "" : "muted-value"),
            createCell(computer.mac_address, computer.mac_address ? "mono" : "mono muted-value"),
            createCell(computer.last_ip_address, computer.last_ip_address ? "mono" : "mono muted-value"),
            createCell(computer.last_image_name, computer.last_image_name ? "" : "muted-value"),
            createCell(formatBoolean(computer.last_domain_join)),
            createCell(computer.deployment_count, "mono"),
            createCell(formatDate(computer.first_seen_at)),
            createCell(formatDate(computer.last_seen_at)),
            createCell(
                computer.last_deployment_id ? `#${computer.last_deployment_id}` : "-",
                computer.last_deployment_id ? "mono" : "mono muted-value",
            ),
        );
        elements.rows.append(row);
    }

    renderEmptyMessage(computers.length, state.computers.length);
}

function renderEmptyMessage(visibleCount, sourceCount) {
    if (visibleCount === 0) {
        const meta = VIEW_META[state.view];
        elements.message.textContent = sourceCount ? meta.filteredEmpty : meta.empty;
        elements.message.classList.remove("is-error");
        elements.message.hidden = false;
    } else {
        elements.message.hidden = true;
    }
}

function renderRows() {
    const meta = VIEW_META[state.view];
    elements.table.classList.toggle("deployment-table", state.view === "deployments");
    elements.table.classList.toggle("computer-table", state.view === "computers");
    elements.headerPageTitle.textContent = meta.title;
    elements.deploymentSummary.hidden = state.view !== "deployments";

    if (state.view === "deployments") {
        renderDeploymentRows();
    } else {
        renderComputerRows();
    }
}

function setStatus(status) {
    state.status = status;

    for (const item of elements.summaryItems) {
        const active = item.dataset.summaryStatus === status;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", String(active));
    }

    renderRows();
}

function setView(view) {
    state.view = view;
    for (const tab of elements.viewTabs) {
        const active = tab.dataset.view === view;
        tab.classList.toggle("is-active", active);
        tab.setAttribute("aria-pressed", String(active));
    }
    renderRows();
}

function renderUpdatedAt() {
    if (!state.lastUpdated) {
        return;
    }

    const seconds = Math.round((Date.now() - state.lastUpdated.getTime()) / 1000);
    if (seconds < 10) {
        elements.updatedAt.textContent = "Updated just now";
    } else if (seconds < 60) {
        elements.updatedAt.textContent = `Updated ${seconds}s ago`;
    } else {
        elements.updatedAt.textContent = `Updated at ${state.lastUpdated.toLocaleTimeString(
            "ru-RU",
            { hour: "2-digit", minute: "2-digit" },
        )}`;
    }
}

async function fetchJson(url) {
    const response = await fetch(url, {
        headers: { Accept: "application/json" },
        cache: "no-store",
    });

    if (!response.ok) {
        throw new Error(`${url} returned HTTP ${response.status}`);
    }

    return response.json();
}

async function loadData() {
    elements.refreshButton.disabled = true;
    elements.refreshButton.classList.add("is-loading");
    elements.tableRegion.setAttribute("aria-busy", "true");

    try {
        const [deploymentData, computerData] = await Promise.all([
            fetchJson("/api/deployments"),
            fetchJson("/api/computers"),
        ]);

        state.deployments = deploymentData.items;
        state.computers = computerData.items;
        state.totals.deployments = deploymentData.total;
        state.totals.computers = computerData.total;
        state.deploymentSummary = {
            total: deploymentData.total,
            begin: deploymentData.begin,
            completed: deploymentData.completed,
            failed: deploymentData.failed,
        };
        state.lastUpdated = new Date();

        const locale = window.IronI18n?.language === "ru" ? "ru-RU" : "en-US";
        elements.totalCount.textContent = deploymentData.total.toLocaleString(locale);
        elements.beginCount.textContent = deploymentData.begin.toLocaleString(locale);
        elements.completedCount.textContent = deploymentData.completed.toLocaleString(locale);
        elements.failedCount.textContent = deploymentData.failed.toLocaleString(locale);

        elements.message.classList.remove("is-error");
        elements.syncDot.classList.remove("is-error");
        renderRows();
        renderUpdatedAt();
    } catch (error) {
        elements.message.textContent = `Failed to load data: ${error.message}`;
        elements.message.classList.add("is-error");
        elements.message.hidden = false;
        elements.updatedAt.textContent = "Refresh failed";
        elements.syncDot.classList.add("is-error");
    } finally {
        elements.refreshButton.disabled = false;
        elements.refreshButton.classList.remove("is-loading");
        elements.tableRegion.setAttribute("aria-busy", "false");
    }
}

elements.refreshButton.addEventListener("click", loadData);
elements.searchInput.addEventListener("input", (event) => {
    state.search = event.target.value;
    renderRows();
});
elements.searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && elements.searchInput.value) {
        elements.searchInput.value = "";
        state.search = "";
        renderRows();
    }
});
for (const item of elements.summaryItems) {
    item.addEventListener("click", () => setStatus(item.dataset.summaryStatus));
}
for (const tab of elements.viewTabs) {
    tab.addEventListener("click", () => setView(tab.dataset.view));
}
elements.rows.addEventListener("click", (event) => {
    if (event.target.closest("a")) {
        return;
    }
    const cell = event.target.closest("td");
    if (cell) {
        copyText(cell.dataset.copyValue ?? cell.textContent.trim());
    }
});

setStatus("all");
setView("deployments");
loadData();
setInterval(loadData, AUTO_REFRESH_MS);
setInterval(renderUpdatedAt, 5_000);
setInterval(renderLiveDurations, 1_000);
