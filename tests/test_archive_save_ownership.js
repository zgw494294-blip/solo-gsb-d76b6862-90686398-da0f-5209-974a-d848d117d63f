"use strict";

// 前端「保存存档」请求归属的回归测试。
//
// 背景：首次保存（尚未取得版本）会先发 GET /api/archive 预读取槽内版本，再据此 PUT。
// 若预读取延迟期间确认重新导入（或随后恢复存档并修改），旧保存动作必须失效：
// 迟到的预读取无论成功、失败还是收尾，都不得触发后续 PUT，也不得改动新稿的版本、
// 未存档标记、提示或新一次保存的等待状态。
//
// 本文件用最小 DOM 桩在 Node 虚拟机中加载真实的 app/static/app.js（不修改它），
// 直接驱动「保存存档 / 重新导入 / 恢复存档」的真实事件处理器，fetch 由测试逐个收尾。
// 运行：node --test tests/test_archive_save_ownership.js（无第三方依赖）

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "..", "app", "static", "app.js");

// --------------------------------------------------------------------------- //
// 最小 DOM 桩：只实现 app.js 在上述流程中真正触达的元素能力
// --------------------------------------------------------------------------- //
function makeElement(name) {
  const listeners = new Map();
  const classes = new Set();
  return {
    name,
    hidden: false,
    disabled: false,
    checked: false,
    textContent: "",
    innerHTML: "",
    value: "",
    title: "",
    dataset: {},
    classList: {
      add: (...cs) => cs.forEach((c) => classes.add(c)),
      remove: (...cs) => cs.forEach((c) => classes.delete(c)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : Boolean(force);
        if (on) classes.add(c);
        else classes.delete(c);
        return on;
      },
      contains: (c) => classes.has(c),
    },
    addEventListener: (type, fn) => {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    // 触发已注册监听器并返回其 Promise（调用方自行决定何时 await）
    fire(type, event = {}) {
      return Promise.all((listeners.get(type) || []).map((fn) => fn(event)));
    },
    querySelector: () => null,
    querySelectorAll: () => [],
    appendChild: () => {},
    scrollIntoView: () => {},
    focus: () => {},
    setSelectionRange: () => {},
    closest: () => null,
  };
}

// fetch 桩：记录每一次请求，响应时机与结果完全由测试控制
function makeFetchStub() {
  const calls = [];
  const fetch = (url, opts = {}) =>
    new Promise((resolve, reject) => {
      const call = {
        url,
        method: opts.method || "GET",
        body: opts.body ? JSON.parse(opts.body) : null,
        settled: false,
        respond(status, data) {
          call.settled = true;
          resolve({
            ok: status >= 200 && status < 300,
            status,
            json: async () => data,
          });
        },
        fail(err) {
          call.settled = true;
          reject(err || new TypeError("fetch failed"));
        },
      };
      calls.push(call);
    });
  fetch.calls = calls;
  fetch.archiveGets = () =>
    calls.filter((c) => c.url === "/api/archive" && c.method === "GET");
  fetch.archivePuts = () =>
    calls.filter((c) => c.url === "/api/archive" && c.method === "PUT");
  return fetch;
}

// 在虚拟机中加载 app.js，并借追加的测试钩子把模块内部状态暴露出来（app.js 本身不改动）
function loadApp() {
  const elements = new Map();
  const $el = (sel) => {
    if (!elements.has(sel)) elements.set(sel, makeElement(sel));
    return elements.get(sel);
  };
  const fetch = makeFetchStub();
  const confirms = [];
  const sandbox = {
    document: {
      querySelector: $el,
      querySelectorAll: () => [],
      getElementById: () => null,
      createElement: (tag) => makeElement(tag),
      createDocumentFragment: () => makeElement("fragment"),
    },
    fetch,
    confirm: (...args) => {
      confirms.push(args);
      return true; // 测试一律确认（重新导入 / 恢复存档）
    },
    alert: () => {},
    console,
  };
  const source = fs.readFileSync(APP_JS, "utf8");
  const hook = "\n;globalThis.__app = { state, touch };\n";
  vm.createContext(sandbox);
  vm.runInContext(source + hook, sandbox, { filename: "app.js" });
  return { sandbox, fetch, $el, confirms, state: sandbox.__app.state, touch: sandbox.__app.touch };
}

// 让虚拟机中的 Promise 链推进一轮
const flush = () => new Promise((resolve) => setImmediate(resolve));

const IMPORT_OK = {
  clauses: [
    { id: "c1", original: 1, body: "甲方在七日内交付设备", ref_ids: [] },
    { id: "c2", original: 2, body: "乙方按 [[c1]] 验收后付款", ref_ids: ["c1"] },
  ],
};

// 通过页面真实入口完成一次导入（随后的预览请求保持挂起，不影响存档流程）
async function importDoc(app, text) {
  app.$el("#import-text").value = text;
  const done = app.$el("#import-btn").fire("click");
  await flush();
  const call = app.fetch.calls.find((c) => c.url === "/api/import" && !c.settled);
  assert.ok(call, "导入应发出 /api/import 请求");
  call.respond(200, IMPORT_OK);
  await done;
  await flush();
}

// 服务端存档中的既有快照（供恢复）
const STORED_SNAPSHOT = {
  baseline: {
    order_ids: ["c1", "c2"],
    clauses: [
      { id: "c1", body: "存档第一条" },
      { id: "c2", body: "存档第二条见 [[c1]]" },
    ],
  },
  order_ids: ["c1", "c2"],
  clauses: [
    { id: "c1", body: "存档第一条" },
    { id: "c2", body: "存档第二条见 [[c1]]" },
  ],
  id_counter: 2,
};

// --------------------------------------------------------------------------- //
// 迟到预读取成功：重新导入 → 恢复已有存档并修改 → 旧读取才返回
// --------------------------------------------------------------------------- //
test("换稿后迟到的预读取成功：不触发 PUT，不改新稿版本/未存档标记/提示", async () => {
  const app = loadApp();
  await importDoc(app, "1|第一条\n2|第二条 [[1]]");

  // 首次保存：尚未取得版本，先发 GET 预读取（保持挂起，模拟读取延迟）
  const saveDone = app.$el("#archive-save-btn").fire("click");
  await flush();
  assert.equal(app.state.archiving, true);
  assert.equal(app.fetch.archiveGets().length, 1, "首次保存应先发 GET 预读取");
  const staleGet = app.fetch.archiveGets()[0];

  // 读取延迟期间确认重新导入：旧保存动作随即失效
  await app.$el("#reimport-btn").fire("click");
  await flush();
  assert.equal(app.state.archiving, false);

  // 随后恢复已有存档（恢复自身的读取正常返回）并修改草稿
  const restoreDone = app.$el("#archive-restore-import-btn").fire("click");
  await flush();
  assert.equal(app.fetch.archiveGets().length, 2, "恢复会发出自己的 GET");
  app.fetch.archiveGets()[1].respond(200, { version: 3, snapshot: STORED_SNAPSHOT });
  await restoreDone;
  await flush();
  assert.equal(app.state.archiveVersion, 3);
  assert.equal(app.state.archiveDirty, false);
  app.touch(); // 恢复后修改草稿：标为未存档
  assert.equal(app.state.archiveDirty, true);

  // 旧保存的预读取此时才返回：不得发 PUT 保存恢复后的稿件，
  // 也不得改动新稿的版本、未存档标记、提示或等待状态
  staleGet.respond(200, { version: 3, snapshot: null });
  await flush();
  // 若（错误地）发出了 PUT，先让它收尾，使下面的断言能以明确的失败信息结束
  for (const put of app.fetch.archivePuts()) put.respond(200, { version: 4 });
  await saveDone;
  await flush();

  assert.equal(app.fetch.archivePuts().length, 0, "迟到读取不得触发后续 PUT");
  assert.equal(app.state.archiveVersion, 3, "不得改动新稿已取得的版本");
  assert.equal(app.state.archiveDirty, true, "不得清掉新稿的未存档标记");
  assert.equal(app.state.archiving, false);
  assert.equal(app.state.archiveError, null, "不得在新稿上留下错误提示");
  assert.equal(app.state.archiveNotice, null, "不得在新稿上留下提示");
});

// --------------------------------------------------------------------------- //
// 迟到预读取失败：换稿后再次保存已在途，旧读取失败不得干扰
// --------------------------------------------------------------------------- //
test("换稿后迟到的预读取失败：不提示、不解除新保存的等待状态；新保存完整走通", async () => {
  const app = loadApp();
  await importDoc(app, "1|第一条\n2|第二条 [[1]]");

  // 首次保存：预读取挂起
  const save1 = app.$el("#archive-save-btn").fire("click");
  await flush();
  assert.equal(app.fetch.archiveGets().length, 1);
  const staleGet = app.fetch.archiveGets()[0];

  // 重新导入并导入新稿
  await app.$el("#reimport-btn").fire("click");
  await flush();
  await importDoc(app, "1|新稿一\n2|新稿二 [[1]]");

  // 换稿后再次保存：仍未取得版本，发出自己的预读取并进入等待
  const save2 = app.$el("#archive-save-btn").fire("click");
  await flush();
  assert.equal(app.fetch.archiveGets().length, 2, "新保存应发出自己的预读取");
  assert.equal(app.state.archiving, true);

  // 旧预读取迟到失败：不得在新稿上提示，也不得解除新保存的等待状态
  staleGet.fail(new TypeError("network down"));
  await save1;
  await flush();
  assert.equal(app.state.archiving, true, "旧读取失败不得解除新保存的等待状态");
  assert.equal(app.state.archiveError, null, "旧读取失败不得在新稿上提示");
  assert.equal(app.fetch.archivePuts().length, 0, "旧读取失败不得触发 PUT");

  // 新保存的预读取正常返回：据此发出 PUT，成功后清掉未存档标记
  app.fetch.archiveGets()[1].respond(200, { version: 0, snapshot: null });
  await flush();
  assert.equal(app.fetch.archivePuts().length, 1, "新保存应在预读取成功后发出 PUT");
  const put = app.fetch.archivePuts()[0];
  assert.equal(put.body.version, 0);
  assert.deepEqual(put.body.snapshot.order_ids, ["c1", "c2"]);
  assert.equal(put.body.snapshot.id_counter, 2);
  put.respond(200, { version: 1 });
  await save2;
  await flush();
  assert.equal(app.state.archiveVersion, 1);
  assert.equal(app.state.archiveDirty, false, "有效保存成功后清掉未存档标记");
  assert.equal(app.state.archiving, false);
  assert.equal(app.state.archiveError, null);
});

// --------------------------------------------------------------------------- //
// 保存期间改稿：允许；回执只确认发出 PUT 时的快照，此后的修改仍标为未存档
// --------------------------------------------------------------------------- //
test("保存期间改稿：回执只确认提交时的快照，此后修改仍标未存档", async () => {
  const app = loadApp();
  await importDoc(app, "1|第一条\n2|第二条 [[1]]");

  // 首次保存：预读取 → PUT 依次发出
  const saveDone = app.$el("#archive-save-btn").fire("click");
  await flush();
  app.fetch.archiveGets()[0].respond(200, { version: 0, snapshot: null });
  await flush();
  assert.equal(app.fetch.archivePuts().length, 1);
  const put = app.fetch.archivePuts()[0];

  // PUT 在途期间改稿：允许，且不影响已发出的请求
  app.touch();
  assert.equal(app.state.archiveDirty, true);

  // 回执只确认提交时的快照：版本知识推进，但此后的修改仍标为未存档
  put.respond(200, { version: 1 });
  await saveDone;
  await flush();
  assert.equal(app.state.archiveVersion, 1);
  assert.equal(app.state.archiveDirty, true, "保存期间的修改仍标为未存档");
  assert.match(app.state.archiveNotice, /提交时的快照/);
  assert.equal(app.state.archiving, false);
});
