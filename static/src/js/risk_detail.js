(() => {
    "use strict";
    const form = document.getElementById("risk-analyze");
    if (!form) return;
    let pending = false;
    form.addEventListener("submit", (event) => {
        if (pending) {
            event.preventDefault();
            return;
        }
        pending = true;
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        button.textContent = "Агент работает…";
        form.setAttribute("aria-busy", "true");
        document.getElementById("risk-analysis-state").hidden = false;
        // Keep the question intact: the browser still needs to serialize it for POST.
    });
    window.addEventListener("pageshow", () => {
        pending = false;
        const button = form.querySelector("button[type=submit]");
        button.disabled = false;
        button.textContent = "Разобрать риск";
        form.removeAttribute("aria-busy");
        document.getElementById("risk-analysis-state").hidden = true;
    });
})();
