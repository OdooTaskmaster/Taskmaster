"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");
const script = readFileSync(join(__dirname, "../static/src/js/risk_detail.js"), "utf8");

test("native POST preserves question, announces progress and blocks repeat submit", () => {
    const button = { disabled: false };
    const notice = { hidden: true };
    const attrs = {};
    let submit, pageshow;
    const form = {
        querySelector: () => button,
        addEventListener: (_, fn) => { submit = fn; },
        setAttribute: (key, value) => { attrs[key] = value; },
        removeAttribute: (key) => { delete attrs[key]; },
    };
    runInNewContext(script, {
        document: { getElementById: (id) => id === "risk-analyze" ? form : notice },
        window: { addEventListener: (_, fn) => { pageshow = fn; } },
    });
    let prevented = 0;
    const event = { preventDefault: () => { prevented++; } };
    submit(event);
    assert.equal(prevented, 0, "first submission must remain a native POST");
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, "Агент работает…");
    assert.equal(notice.hidden, false);
    assert.equal(attrs["aria-busy"], "true");
    submit(event);
    assert.equal(prevented, 1);
    pageshow();
    assert.equal(button.disabled, false);
    assert.equal(notice.hidden, true);
    assert.equal(attrs["aria-busy"], undefined);
});

test("unrelated page needs no form", () => {
    runInNewContext(script, { document: { getElementById: () => null } });
});

test("answer precedes collapsed history and visible dates are formatted", () => {
    const detail = readFileSync(join(__dirname, "../views/risk_detail.xml"), "utf8");
    const summary = readFileSync(join(__dirname, "../views/risk_summary.xml"), "utf8");
    assert.ok(detail.indexOf("risk-analysis-answer") < detail.indexOf("risk-dialogue-history"));
    assert.match(detail, /<details t-if="history_turns"/);
    assert.doesNotMatch(detail + summary, /t-out="(?:row.deadline|historical_deadline|current_deadline)\.isoformat\(\)"/);
    assert.match(detail, /Выжимка не получена/);
    assert.match(detail, /Проверенный ответ не получен/);
});
