const DETAIL_REFRESH_MS = 10_000;

const DETAIL_STAGE_LABELS = {
    disk_partitioning: "Disk partitioning",
    image_download: "Download image",
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

const detailElements = {
    refreshButton: document.querySelector("#refresh-button"),
    updatedAt: document.querySelector("#updated-at"),
    syncDot: document.querySelector(".sync-dot"),
    loading: document.querySelector("#detail-loading"),
    content: document.querySelector("#detail-content"),
    number: document.querySelector("#deployment-number"),
    title: document.querySelector("#deployment-title"),
    subtitle: document.querySelector("#deployment-subtitle"),
    status: document.querySelector("#deployment-status"),
    totalDuration: document.querySelector("#total-duration"),
    startedAt: document.querySelector("#started-at"),
    completedAt: document.querySelector("#completed-at"),
    deploymentError: document.querySelector("#deployment-error"),
    deploymentErrorMessage: document.querySelector("#deployment-error-message"),
    stageList: document.querySelector("#stage-list"),
    stageEmpty: document.querySelector("#stage-empty"),
    stageCount: document.querySelector("#stage-count"),
    programList: document.querySelector("#program-list"),
    programEmpty: document.querySelector("#program-empty"),
    programCount: document.querySelector("#program-count"),
    copyState: document.querySelector("#copy-state"),
    networkReportState: document.querySelector("#network-report-state"),
    networkEmpty: document.querySelector("#network-empty"),
    networkContent: document.querySelector("#network-content"),
    networkSmbAdapter: document.querySelector("#network-smb-adapter"),
    networkSmbIp: document.querySelector("#network-smb-ip"),
    networkLinkSpeed: document.querySelector("#network-link-speed"),
    networkApiAdapter: document.querySelector("#network-api-adapter"),
    networkApiIp: document.querySelector("#network-api-ip"),
    networkApiLinkSpeed: document.querySelector("#network-api-link-speed"),
    networkAdapterRouteNote: document.querySelector("#network-adapter-route-note"),
    networkPingTarget: document.querySelector("#network-ping-target"),
    networkIcmpStatus: document.querySelector("#network-icmp-status"),
    networkPingCounts: document.querySelector("#network-ping-counts"),
    networkPingLoss: document.querySelector("#network-ping-loss"),
    networkRtt: document.querySelector("#network-rtt"),
    networkSpikes: document.querySelector("#network-spikes"),
    networkInboundRate: document.querySelector("#network-inbound-rate"),
    networkReceivedBytes: document.querySelector("#network-received-bytes"),
    networkLinkUse: document.querySelector("#network-link-use"),
    networkDuration: document.querySelector("#network-duration"),
    networkApiRequests: document.querySelector("#network-api-requests"),
    networkApiErrors: document.querySelector("#network-api-errors"),
    networkApiRtt: document.querySelector("#network-api-rtt"),
    networkSmbStatus: document.querySelector("#network-smb-status"),
    networkSmbAttempts: document.querySelector("#network-smb-attempts"),
    networkSmbDuration: document.querySelector("#network-smb-duration"),
    networkSmbError: document.querySelector("#network-smb-error"),
    networkStageList: document.querySelector("#network-stage-list"),
    networkErrors: document.querySelector("#network-errors"),
    networkErrorList: document.querySelector("#network-error-list"),
};

const detailState = {
    startedAt: null,
    completedAt: null,
    status: null,
};

const deploymentId = Number.parseInt(
    window.location.pathname.split("/").filter(Boolean).at(-1),
    10,
);

function detailText(value) {
    return window.IronI18n?.t(value) || value;
}

function detailFormatDate(value) {
    if (!value) return "-";
    return new Intl.DateTimeFormat(
        window.IronI18n?.language === "ru" ? "ru-RU" : "en-US",
        { dateStyle: "medium", timeStyle: "medium" },
    ).format(new Date(value));
}

function detailFormatDuration(startedAt, completedAt = null) {
    if (!startedAt) return "-";
    const start = new Date(startedAt).getTime();
    const end = completedAt ? new Date(completedAt).getTime() : Date.now();
    const totalSeconds = Math.max(0, Math.floor((end - start) / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes || hours) parts.push(`${minutes}m`);
    parts.push(`${seconds}s`);
    return parts.join(" ");
}

function detailStatusLabel(status) {
    return {
        begin: "Running",
        completed: "Completed",
        failed: "Failed",
        running: "Running",
        skipped: "Skipped",
        installed: "Installed",
        timed_out: "Timed out",
    }[status] || status || "-";
}

function detailFormatNumber(value, maximumFractionDigits = 1) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "-";
    }
    return new Intl.NumberFormat(
        window.IronI18n?.language === "ru" ? "ru-RU" : "en-US",
        { maximumFractionDigits },
    ).format(Number(value));
}

function detailFormatMilliseconds(value) {
    const formatted = detailFormatNumber(value);
    return formatted === "-" ? formatted : `${formatted} ms`;
}

function detailFormatRtt(record, keys = ["rtt_min_ms", "rtt_avg_ms", "rtt_max_ms"]) {
    const values = keys.map((key) => record?.[key]);
    if (values.some((value) => value === null || value === undefined)) return "-";
    return `${values.map((value) => detailFormatNumber(value)).join(" / ")} ms`;
}

function detailFormatBytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
        return "-";
    }
    const units = ["B", "KB", "MB", "GB", "TB"];
    let amount = Number(value);
    let unitIndex = 0;
    while (amount >= 1024 && unitIndex < units.length - 1) {
        amount /= 1024;
        unitIndex += 1;
    }
    return `${detailFormatNumber(amount, unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function detailFormatLinkSpeed(value) {
    if (value === null || value === undefined || Number(value) <= 0) return "-";
    const mbps = Number(value) / 1_000_000;
    if (mbps >= 1000) return `${detailFormatNumber(mbps / 1000, 2)} Gbit/s`;
    return `${detailFormatNumber(mbps, 0)} Mbit/s`;
}

function detailFormatDurationSeconds(value) {
    if (value === null || value === undefined) return "-";
    const totalSeconds = Math.max(0, Math.round(Number(value)));
    if (totalSeconds >= 60) {
        const minutes = Math.floor(totalSeconds / 60);
        return `${minutes}m ${totalSeconds % 60}s`;
    }
    return `${totalSeconds}s`;
}

function detailFormatPercent(value) {
    const formatted = detailFormatNumber(value);
    return formatted === "-" ? formatted : `${formatted}%`;
}

function setNetworkValueState(element, state = null) {
    element.classList.remove("is-ok", "is-warning", "is-error");
    if (state) element.classList.add(`is-${state}`);
}

function networkIcmpLabel(record) {
    return {
        available: record.loss_percentage > 0
            ? `${detailFormatPercent(record.loss_percentage)} ${detailText("loss")}`
            : detailText("Available"),
        unavailable: detailText("ICMP unavailable"),
        not_measured: detailText("Not measured"),
    }[record.icmp_status] || detailText("Not measured");
}

function networkIcmpState(record) {
    if (record.icmp_status === "available") {
        return record.loss_percentage > 0 ? "warning" : "ok";
    }
    return record.icmp_status === "unavailable" ? "warning" : null;
}

function setAdapterDetails(nameElement, ipElement, adapter) {
    nameElement.textContent = adapter?.name || detailText("Not detected");
    nameElement.title = adapter?.description || "";
    ipElement.textContent = adapter?.local_ip || detailText("IP unavailable");
}

function createNetworkStageMetric(labelText, valueText) {
    const metric = document.createElement("div");
    metric.className = "network-stage-metric";
    const label = document.createElement("span");
    label.textContent = detailText(labelText);
    const value = document.createElement("strong");
    value.textContent = valueText;
    metric.append(label, value);
    return metric;
}

function renderNetworkStages(stages) {
    const order = [
        "image_download", "image_apply", "driver_injection", "postinstall_copy",
    ];
    const byStage = new Map(stages.map((stage) => [stage.stage, stage]));
    detailElements.networkStageList.replaceChildren();

    for (const stageName of order) {
        const stage = byStage.get(stageName);
        const row = document.createElement("article");
        row.className = "network-stage-row";

        const name = document.createElement("div");
        name.className = "network-stage-name";
        const title = document.createElement("strong");
        title.textContent = detailText(DETAIL_STAGE_LABELS[stageName]);
        const state = document.createElement("span");
        state.textContent = stage
            ? detailText("Measured")
            : detailText("Not reported");
        name.append(title, state);

        if (!stage) {
            row.append(
                name,
                createNetworkStageMetric("Duration", "-"),
                createNetworkStageMetric("Sent / received / lost", "-"),
                createNetworkStageMetric("Packet loss", "-"),
                createNetworkStageMetric("RTT min / avg / max", "-"),
                createNetworkStageMetric("RTT above 50 ms", "-"),
                createNetworkStageMetric(
                    "Average inbound adapter traffic during stage",
                    "-",
                ),
                createNetworkStageMetric("Link utilization", "-"),
            );
        } else {
            row.append(
                name,
                createNetworkStageMetric(
                    "Duration",
                    detailFormatDurationSeconds(stage.duration_seconds),
                ),
                createNetworkStageMetric(
                    "Sent / received / lost",
                    `${stage.ping_sent} / ${stage.ping_received} / ${stage.ping_lost}`,
                ),
                createNetworkStageMetric(
                    "Packet loss",
                    stage.icmp_status === "available"
                        ? detailFormatPercent(stage.loss_percentage)
                        : networkIcmpLabel(stage),
                ),
                createNetworkStageMetric("RTT min / avg / max", detailFormatRtt(stage)),
                createNetworkStageMetric(
                    "RTT above 50 ms",
                    String(stage.latency_spikes),
                ),
                createNetworkStageMetric(
                    "Average inbound adapter traffic during stage",
                    stage.average_inbound_mbps === null
                        ? "-"
                        : `${detailFormatNumber(stage.average_inbound_mbps, 2)} MB/s`,
                ),
                createNetworkStageMetric(
                    "Link utilization",
                    detailFormatPercent(stage.link_utilization_percent),
                ),
            );
        }
        detailElements.networkStageList.append(row);
    }
}

function renderNetworkDiagnostics(report) {
    const hasReport = Boolean(report);
    detailElements.networkEmpty.hidden = hasReport;
    detailElements.networkContent.hidden = !hasReport;
    detailElements.networkReportState.textContent = detailText(
        hasReport ? "Reported" : "Not reported",
    );
    detailElements.networkReportState.classList.toggle("is-reported", hasReport);
    if (!hasReport) return;

    setAdapterDetails(
        detailElements.networkSmbAdapter,
        detailElements.networkSmbIp,
        report.smb_adapter,
    );
    setAdapterDetails(
        detailElements.networkApiAdapter,
        detailElements.networkApiIp,
        report.api_adapter,
    );
    detailElements.networkLinkSpeed.textContent =
        detailFormatLinkSpeed(report.smb_adapter?.link_speed_bps);
    detailElements.networkApiLinkSpeed.textContent =
        detailFormatLinkSpeed(report.api_adapter?.link_speed_bps);
    detailElements.networkAdapterRouteNote.textContent =
        report.adapters_differ === null
            ? "-"
            : detailText(
                report.adapters_differ ? "Different adapter" : "Same adapter",
            );
    detailElements.networkPingTarget.textContent = report.ping_target || "-";

    const overall = report.overall;
    if (overall) {
        detailElements.networkIcmpStatus.textContent = networkIcmpLabel(overall);
        setNetworkValueState(
            detailElements.networkIcmpStatus,
            networkIcmpState(overall),
        );
        detailElements.networkPingCounts.textContent =
            `${overall.ping_sent} / ${overall.ping_received} / ${overall.ping_lost}`;
        detailElements.networkPingLoss.textContent =
            overall.icmp_status === "available"
                ? detailFormatPercent(overall.loss_percentage)
                : "-";
        detailElements.networkRtt.textContent = detailFormatRtt(overall);
        detailElements.networkSpikes.textContent = String(overall.latency_spikes);
        detailElements.networkInboundRate.textContent =
            overall.average_inbound_mbps === null
                ? "-"
                : `${detailFormatNumber(overall.average_inbound_mbps, 2)} MB/s`;
        detailElements.networkReceivedBytes.textContent =
            detailFormatBytes(overall.bytes_received);
        detailElements.networkLinkUse.textContent =
            detailFormatPercent(overall.link_utilization_percent);
        detailElements.networkDuration.textContent =
            detailFormatDurationSeconds(overall.duration_seconds);
    } else {
        detailElements.networkIcmpStatus.textContent = "-";
        setNetworkValueState(detailElements.networkIcmpStatus, null);
        detailElements.networkPingCounts.textContent = "-";
        detailElements.networkPingLoss.textContent = "-";
        detailElements.networkRtt.textContent = "-";
        detailElements.networkSpikes.textContent = "-";
        detailElements.networkInboundRate.textContent = "-";
        detailElements.networkReceivedBytes.textContent = "-";
        detailElements.networkLinkUse.textContent = "-";
        detailElements.networkDuration.textContent = "-";
    }

    const api = report.api;
    detailElements.networkApiRequests.textContent = api
        ? `${api.request_count} ${detailText(
            api.request_count === 1 ? "request" : "requests",
        )}`
        : "-";
    detailElements.networkApiErrors.textContent =
        api ? String(api.error_count) : "-";
    detailElements.networkApiRtt.textContent =
        api ? detailFormatRtt(api, ["min_ms", "avg_ms", "max_ms"]) : "-";

    const smb = report.smb;
    const smbStatus = smb?.success === true
        ? detailText("Connected")
        : smb?.success === false
            ? detailText("Failed")
            : smb
                ? detailText("Not measured")
                : "-";
    detailElements.networkSmbStatus.textContent = smbStatus;
    setNetworkValueState(
        detailElements.networkSmbStatus,
        smb?.success === true
            ? "ok"
            : smb?.success === false
                ? "error"
                : null,
    );
    detailElements.networkSmbAttempts.textContent =
        smb ? String(smb.attempts) : "-";
    detailElements.networkSmbDuration.textContent =
        smb ? detailFormatMilliseconds(smb.duration_ms) : "-";
    detailElements.networkSmbError.hidden = !smb?.error_message;
    detailElements.networkSmbError.textContent = smb?.error_message || "";

    renderNetworkStages(report.stages || []);

    const warnings = report.diagnostic_errors || [];
    detailElements.networkErrors.hidden = warnings.length === 0;
    detailElements.networkErrorList.replaceChildren();
    for (const warning of warnings) {
        const item = document.createElement("li");
        item.textContent = warning;
        detailElements.networkErrorList.append(item);
    }
}

function setCopyableField(name, value) {
    const element = document.querySelector(`[data-field="${name}"]`);
    const text = value === null || value === undefined || value === "" ? "-" : String(value);
    element.textContent = text;
    element.classList.toggle("is-empty", text === "-");
    if (text === "-") {
        delete element.dataset.copyValue;
        element.removeAttribute("title");
    } else {
        element.dataset.copyValue = text;
        element.title = detailText("Click to copy");
    }
}

function formatTargetDisk(deployment) {
    if (
        deployment.target_disk_number === null ||
        deployment.target_disk_number === undefined ||
        !deployment.target_disk_model ||
        !deployment.target_disk_size_bytes
    ) {
        return null;
    }
    const gibibytes = deployment.target_disk_size_bytes / (1024 ** 3);
    return `${detailText("Disk")} ${deployment.target_disk_number} — ${deployment.target_disk_model} — ${gibibytes.toFixed(2)} GiB`;
}

async function copyDetailValue(value) {
    if (!value || value === "-") return;
    let copied = false;
    try {
        if (window.isSecureContext && navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            copied = true;
        } else {
            const textarea = document.createElement("textarea");
            textarea.value = value;
            textarea.readOnly = true;
            textarea.style.position = "fixed";
            textarea.style.inset = "-9999px auto auto -9999px";
            document.body.append(textarea);
            textarea.select();
            copied = document.execCommand("copy");
            textarea.remove();
        }
    } catch {
        copied = false;
    }
    if (copied) {
        detailElements.copyState.textContent = `${detailText("Copied")}: ${value}`;
        window.setTimeout(() => {
            detailElements.copyState.textContent = detailText("Click a value to copy");
        }, 1800);
    } else {
        detailElements.copyState.textContent = detailText("Clipboard unavailable");
    }
}

function renderStages(stages) {
    detailElements.stageList.replaceChildren();
    detailElements.stageCount.textContent = `${stages.length} ${detailText(
        stages.length === 1 ? "stage" : "stages",
    )}`;
    detailElements.stageEmpty.hidden = stages.length !== 0;
    detailElements.stageList.hidden = stages.length === 0;

    for (const stage of stages) {
        const row = document.createElement("article");
        row.className = `detail-stage status-${stage.status}`;

        const marker = document.createElement("span");
        marker.className = "stage-marker";
        marker.setAttribute("aria-hidden", "true");

        const main = document.createElement("div");
        main.className = "detail-stage-main";
        const label = document.createElement("strong");
        label.textContent = detailText(DETAIL_STAGE_LABELS[stage.stage] || stage.stage);
        const status = document.createElement("span");
        status.textContent = detailText(detailStatusLabel(stage.status));
        main.append(label, status);

        const time = document.createElement("div");
        time.className = "stage-time";
        time.textContent = detailFormatDate(stage.started_at);

        const duration = document.createElement("div");
        duration.className = "stage-duration";
        duration.textContent = detailFormatDuration(stage.started_at, stage.completed_at);

        row.append(marker, main, time, duration);
        if (stage.error_message) {
            const error = document.createElement("pre");
            error.className = "stage-error";
            error.textContent = stage.error_message;
            row.append(error);
        }
        detailElements.stageList.append(row);
    }
}

function renderPrograms(programs) {
    detailElements.programList.replaceChildren();
    detailElements.programCount.textContent = `${programs.length} ${detailText(
        programs.length === 1 ? "program" : "programs",
    )}`;
    detailElements.programEmpty.hidden = programs.length !== 0;
    detailElements.programList.hidden = programs.length === 0;

    for (const program of programs) {
        const card = document.createElement("article");
        card.className = `program-card status-${program.status}`;

        const head = document.createElement("div");
        head.className = "program-card-head";
        const name = document.createElement("strong");
        name.className = "program-name";
        name.textContent = program.name;
        const status = document.createElement("span");
        status.className = "program-status";
        status.textContent = detailText(detailStatusLabel(program.status));
        head.append(name, status);

        const meta = document.createElement("div");
        meta.className = "program-meta";
        const duration = document.createElement("span");
        duration.textContent = `${detailText("Duration")}: ${program.duration_seconds}s`;
        const exitCode = document.createElement("span");
        exitCode.textContent = `${detailText("Exit code")}: ${program.exit_code ?? "-"}`;
        meta.append(duration, exitCode);
        if (program.reason) {
            const reason = document.createElement("span");
            reason.textContent = `${detailText("Reason")}: ${program.reason}`;
            meta.append(reason);
        }

        card.append(head, meta);
        if (program.error_message) {
            const error = document.createElement("p");
            error.className = "program-error";
            error.textContent = program.error_message;
            card.append(error);
        }
        detailElements.programList.append(card);
    }
}

function renderDeployment(deployment) {
    detailState.startedAt = deployment.started_at;
    detailState.completedAt = deployment.completed_at;
    detailState.status = deployment.status;
    document.title = `IronDeploy - Deployment #${deployment.deployment_id}`;
    detailElements.number.textContent =
        `${detailText("Deployment")} #${deployment.deployment_id}`;
    detailElements.title.textContent = deployment.computer_name;
    detailElements.subtitle.textContent =
        deployment.model || detailText("Hardware model was not reported");
    detailElements.status.textContent = detailText(detailStatusLabel(deployment.status));
    detailElements.status.className = `detail-status status-${deployment.status}`;

    setCopyableField("computer_name", deployment.computer_name);
    setCopyableField("manufacturer", deployment.manufacturer);
    setCopyableField("model", deployment.model);
    setCopyableField("system_sku", deployment.system_sku);
    setCopyableField("serial_number", deployment.serial_number);
    setCopyableField("mac_address", deployment.mac_address);
    setCopyableField("ip_address", deployment.ip_address);
    setCopyableField("image_name", deployment.image_name);
    setCopyableField("image_apply_mode", deployment.imageApplyMode);
    setCopyableField("target_disk", formatTargetDisk(deployment));
    setCopyableField(
        "domain_join",
        detailText(deployment.domain_join ? "Yes" : "No"),
    );
    setCopyableField("deployment_id", deployment.deployment_id);

    detailElements.totalDuration.textContent = detailFormatDuration(
        deployment.started_at,
        deployment.completed_at,
    );
    detailElements.startedAt.textContent = detailFormatDate(deployment.started_at);
    detailElements.completedAt.textContent = detailFormatDate(deployment.completed_at);

    detailElements.deploymentError.hidden = !deployment.last_error_message;
    detailElements.deploymentErrorMessage.textContent =
        deployment.last_error_message || "";
    renderStages(deployment.stages || []);
    renderPrograms(deployment.programs || []);
    renderNetworkDiagnostics(deployment.network_diagnostics);

    detailElements.loading.hidden = true;
    detailElements.content.hidden = false;
}

async function loadDeployment() {
    if (!Number.isInteger(deploymentId) || deploymentId < 1) {
        detailElements.loading.textContent = detailText("Invalid deployment ID.");
        detailElements.loading.classList.add("is-error");
        return;
    }

    detailElements.refreshButton.disabled = true;
    detailElements.refreshButton.classList.add("is-loading");
    try {
        const response = await fetch(`/api/deployments/${deploymentId}`, {
            headers: { Accept: "application/json" },
            cache: "no-store",
        });
        if (response.status === 401) {
            window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
            return;
        }
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail || `HTTP ${response.status}`);
        }
        renderDeployment(await response.json());
        detailElements.updatedAt.textContent =
            `${detailText("Updated")} ${new Intl.DateTimeFormat(
                window.IronI18n?.language === "ru" ? "ru-RU" : "en-US",
                { timeStyle: "medium" },
            ).format(new Date())}`;
        detailElements.syncDot.classList.remove("is-error");
    } catch (error) {
        if (detailElements.content.hidden) {
            detailElements.loading.hidden = false;
            detailElements.loading.classList.add("is-error");
            detailElements.loading.textContent =
                `${detailText("Unable to load deployment")}: ${error.message}`;
        }
        detailElements.syncDot.classList.add("is-error");
        detailElements.updatedAt.textContent = detailText("Refresh failed");
    } finally {
        detailElements.refreshButton.disabled = false;
        detailElements.refreshButton.classList.remove("is-loading");
    }
}

document.addEventListener("click", (event) => {
    const value = event.target.closest("[data-copy-value]")?.dataset.copyValue;
    if (value) copyDetailValue(value);
});
detailElements.refreshButton.addEventListener("click", loadDeployment);
window.setInterval(loadDeployment, DETAIL_REFRESH_MS);
window.setInterval(() => {
    if (
        detailState.status === "begin"
        && detailState.startedAt
        && !detailElements.content.hidden
    ) {
        detailElements.totalDuration.textContent = detailFormatDuration(
            detailState.startedAt,
            detailState.completedAt,
        );
    }
}, 1000);
loadDeployment();
