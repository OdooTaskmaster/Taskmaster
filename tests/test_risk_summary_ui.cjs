"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(join(__dirname, "../static/src/js/risk_summary.js"), "utf8");

function fakePage(responses, now = Date.now) {
    const calls = [];
    const waits = [];
    const locations = [];
    const button = { disabled: false };
    const notice = { hidden: true, classList: { toggle() {} } };
    let submit;
    const form = {
        action: "/taskmaster/risk-summary/generate",
        querySelector: () => button,
        addEventListener: (_, fn) => { submit = fn; },
    };
    runInNewContext(script, {
        document: { getElementById: (id) => id === "risk-generate" ? form : notice },
        window: { location: { origin: "http://example.test", assign: (url) => locations.push(String(url)) } },
        fetch: async (url, options) => {
            calls.push({ url, method: options.method || "GET" });
            assert.ok(responses.length, "unexpected extra request");
            return responses.shift();
        },
        setTimeout: (resolve, ms) => waits.push({ ms, resolve }),
        FormData: class {
            constructor() { return [["project_id", "4"], ["csrf_token", "test"]]; }
        },
        AbortSignal,
        Date: { now },
        URL,
        URLSearchParams,
    });
    return { calls, waits, locations, button, notice, submit: () => submit({ preventDefault() {} }) };
}

function response(status, payload) {
    return { status, ok: status >= 200 && status < 300, json: async () => payload };
}

async function tick(page) {
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(page.waits.length, 1);
    const wait = page.waits.shift();
    assert.equal(wait.ms, 2000);
    wait.resolve();
}

test("one POST and serial status reads show the exact saved result", async () => {
    const page = fakePage([
        response(202, { operation_id: "op-1" }),
        response(200, { status: "running" }),
        response(200, { status: "succeeded", result_id: "result-1" }),
    ]);
    const pending = page.submit();
    assert.equal(page.button.disabled, true);
    await page.submit();
    await tick(page);
    await tick(page);
    await pending;
    assert.equal(page.waits.length, 0);
    assert.deepEqual(page.locations, ["/taskmaster/risk-summary?project_id=4&result_id=result-1"]);
    assert.deepEqual(page.calls.map(({ method, url }) => `${method} ${url}`), [
        "POST /taskmaster/risk-summary/generate",
        "GET /taskmaster/api/projects/4/operations/op-1",
        "GET /taskmaster/api/projects/4/operations/op-1",
    ]);
});

test("the synchronous redirect opens the saved result without polling", async () => {
    const target = "http://example.test/taskmaster/risk-summary?project_id=4&result_id=result-2";
    const page = fakePage([{ ...response(200, {}), redirected: true, url: target }]);
    await page.submit();
    assert.deepEqual(page.locations, [target]);
    assert.deepEqual(page.calls, [{ method: "POST", url: "/taskmaster/risk-summary/generate" }]);
    assert.equal(page.waits.length, 0);
});

test("the waiting deadline stops polling without restarting generation", async () => {
    let now = 0;
    const page = fakePage([response(202, { operation_id: "op-1" })], () => now);
    const pending = page.submit();
    await new Promise((resolve) => setImmediate(resolve));
    now = 300_000;
    await tick(page);
    await pending;
    assert.equal(page.calls.length, 1);
    assert.equal(page.waits.length, 0);
    assert.match(page.notice.textContent, /Расчёт может продолжаться/);
});

for (const [label, final, message] of [
    ["a failed operation", response(200, { status: "failed" }), /Не удалось сформировать/],
    ["revoked access", response(404, { error: "not_found" }), /Результат недоступен/],
    ["a status read error", response(503, { error: "storage_unavailable" }), /мог продолжиться/],
]) {
    test(`polling stops after ${label} without resubmission`, async () => {
        const page = fakePage([response(202, { operation_id: "op-1" }), final]);
        const pending = page.submit();
        await tick(page);
        await pending;
        assert.equal(page.calls.length, 2);
        assert.equal(page.waits.length, 0);
        assert.equal(page.locations.length, 0);
        assert.equal(page.notice.hidden, false);
        assert.match(page.notice.textContent, message);
    });
}
