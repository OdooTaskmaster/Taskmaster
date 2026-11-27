(() => {
    "use strict";

    const form = document.getElementById("risk-generate");
    if (!form) {
        return;
    }
    const button = form.querySelector("button[type=submit]");
    const notice = document.getElementById("risk-operation-state");
    const resultPath = "/taskmaster/risk-summary";
    const key = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
    const delay = () => new Promise((resolve) => setTimeout(resolve, 2000));

    function show(text, failed = false) {
        notice.textContent = text;
        notice.classList.toggle("risk-notice-error", failed);
        notice.hidden = false;
    }

    async function waitForResult(project, operation) {
        const statusUrl = `/taskmaster/api/projects/${project}/operations/${encodeURIComponent(operation)}`;
        const deadline = Date.now() + 300_000;
        let status = "accepted";
        while (status === "accepted" || status === "running") {
            show(status === "accepted" ? "Сводка принята. Ожидаем расчёт…" : "Сводка формируется…");
            await delay();
            if (Date.now() >= deadline) {
                show("Ожидание остановлено. Расчёт может продолжаться; готовая сводка появится при повторном открытии проекта.");
                return;
            }
            const response = await fetch(statusUrl, {
                headers: { Accept: "application/json" },
                cache: "no-store",
                redirect: "error",
                signal: AbortSignal.timeout(10000),
            });
            if (response.status === 404) {
                show("Результат недоступен.", true);
                return;
            }
            if (!response.ok) {
                throw new Error("Unreadable operation status");
            }
            const result = await response.json();
            status = result.status;
            if (status === "succeeded" && typeof result.result_id === "string" && key.test(result.result_id)) {
                const query = new URLSearchParams({ project_id: project, result_id: result.result_id });
                window.location.assign(`${resultPath}?${query}`);
                return;
            }
            if (status === "failed") {
                show("Не удалось сформировать сводку.", true);
                return;
            }
            if (status !== "accepted" && status !== "running") {
                throw new Error("Unexpected operation status");
            }
        }
    }

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (button.disabled) {
            return;
        }
        button.disabled = true;
        let accepted = false;
        try {
            const body = new URLSearchParams(new FormData(form));
            const project = body.get("project_id");
            if (!/^\d+$/.test(project ?? "")) {
                throw new Error("Invalid project");
            }
            show("Запускаем расчёт сводки…");
            const response = await fetch(form.action, { method: "POST", body, cache: "no-store" });
            if (response.redirected) {
                const target = new URL(response.url);
                if (target.origin !== window.location.origin || target.pathname !== resultPath) {
                    throw new Error("Unexpected redirect");
                }
                window.location.assign(target);
                return;
            }
            if (response.status !== 202) {
                show(response.status === 404 ? "Сводка недоступна." : "Не удалось запустить расчёт сводки.", true);
                return;
            }
            accepted = true;
            const { operation_id: operation } = await response.json();
            if (typeof operation !== "string" || !key.test(operation)) {
                throw new Error("Invalid operation ID");
            }
            await waitForResult(project, operation);
        } catch {
            show(accepted ? "Не удалось прочитать статус. Расчёт мог продолжиться." : "Не удалось получить ответ о запуске сводки.", true);
        } finally {
            button.disabled = false;
        }
    });
})();
