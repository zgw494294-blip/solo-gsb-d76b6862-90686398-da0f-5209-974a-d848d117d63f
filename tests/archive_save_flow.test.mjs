// 校稿存档「保存」流程的前端回归测试：首次保存的预读取（GET /api/archive）与随后
// PUT 的请求归属。
//
// 覆盖：
//  1. 预读取延迟期间「重新导入」，旧 GET 成功返回后：不发 PUT，不改变新稿的版本、
//     未存档标记、横幅提示，且不解除新保存的等待状态（换稿后再次保存）。
//  2. 预读取延迟期间确认「恢复存档」，旧 GET 成功返回后：同样作废，不发 PUT、
//     不覆盖恢复稿及其存档状态；恢复（installArchive 推进世代）使旧保存动作失效。
//  3. 预读取延迟期间「重新导入」，旧 GET 失败返回后：不弹失败提示、不发 PUT、
//     不改变新稿任何存档状态。
//  4. PUT 在途期间改稿：回执只确认发出当刻的快照（PUT 载荷不含改动），
//     成功后版本推进但仍提示未存档；保存期间允许改稿。
//  5. 预读取仍属于同一草稿时成功 / 失败 / 成功后再保存的常规链路（防回归）。
//
// 运行：node --test tests/（或 npm test），仅依赖 jsdom，不启动真实后端。

import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { JSDOM } from "jsdom";

const here = path.dirname(fileURLToPath(import.meta.url));
const appJs = readFileSync(path.join(here, "..", "app", "static", "app.js"), "utf8");
const indexHtml = readFileSync(path.join(here, "..", "app", "static", "index.html"), "utf8");

// --------------------------------------------------------------------------- //
// 轻量服务端 / DOM 夹具
// --------------------------------------------------------------------------- //
function parseInternalBodies(text) {
  // 与后端 parse_import 的最小对应：仅处理测试用的合法文本「N|正文（[[数字]]）」
  const lines = text.replace(/\r\n/g, "\n").trimEnd().split("\n");
  return lines.map((line, i) => {
    const num = i + 1;
    const body = line.split("|").slice(1).join("|").replace(/\[\[(\d+)\]\]/g, "[[c$1]]");
    return { id: `c${num}`, original: num, body, ref_ids: [] };
  });
}

function previewItems(orderIds, clauses) {
  const pos = new Map(orderIds.map((id, i) => [id, i + 1]));
  const byId = new Map(clauses.map((c) => [c.id, c]));
  return orderIds.map((id, i) => {
    const text = (byId.get(id).body || "").replace(/\[\[c(\d+)\]\]/g, (m, n) =>
      pos.has(`c${n}`) ? `[[${pos.get(`c${n}`)}]]` : `[[原${n}?]]`,
    );
    return { id, original: Number(id.slice(1)), display: i + 1, text, broken: [] };
  });
}

function makeSnapshot(clauses, idCounter) {
  const orderIds = clauses.map((c) => c.id);
  const snapClauses = clauses.map((c) => ({ id: c.id, body: c.body }));
  return {
    baseline: { order_ids: orderIds.slice(), clauses: snapClauses.map((c) => ({ ...c })) },
    order_ids: orderIds,
    clauses: snapClauses,
    id_counter: idCounter,
  };
}

// 模拟 fetch 的 Response（jsdom 不提供 Response；app.js 只用到 .ok/.status/.json()）
class FakeResponse {
  constructor(status, data) {
    this.status = status;
    this.ok = status >= 200 && status < 300;
    this._data = data;
  }
  async json() {
    return this._data;
  }
}

async function loadHarness() {
  const dom = new JSDOM(indexHtml, {
    url: "http://localhost:8000/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  const { window } = dom;

  const log = []; // { method, path, body? }
  // 挂起请求队列：saveGet 为首次保存的预读取；restoreGet 为恢复流程的读取；
  // puts 为挂起的 PUT。各自独立，模拟真实乱序返回。
  const saveGets = [];
  const restoreGets = [];
  const puts = [];
  const server = { version: 0, snapshot: null };

  window.confirm = () => true;
  window.alert = () => {};

  window.fetch = (url, opts = {}) => {
    const method = opts.method || "POST";
    const pathname = String(url);
    const payload = opts.body ? JSON.parse(opts.body) : null;

    const respond = (status, data) => Promise.resolve(new FakeResponse(status, data));

    if (method === "GET" && pathname.endsWith("/api/archive")) {
      const entry = {
        method,
        path: pathname,
        resolve: null,
        reject: null,
        promise: null,
      };
      entry.promise = new Promise((resolve, reject) => {
        entry.resolve = (data) => resolve(new FakeResponse(200, data));
        entry.reject = (err) => reject(err);
      });
      // 保存的预读取挂起等待；恢复流程的读取默认即时返回（可经 opts 语义区分：
      // 恢复读取由 restoreArchive 发起——这里用调用来源无法区分，改为全局开关）
      const queue = fetchCtx.nextGetQueue;
      fetchCtx.nextGetQueue = saveGets; // 默认回到“保存预读取挂起”
      queue.push(entry);
      log.push(entry);
      if (queue === restoreGets) {
        entry.resolve({ version: server.version, snapshot: server.snapshot });
      }
      return entry.promise;
    }
    if (method === "PUT" && pathname.endsWith("/api/archive")) {
      log.push({ method, path: pathname, body: payload });
      const entry = {
        resolve: null,
        promise: null,
        payload,
      };
      entry.promise = new Promise((resolve) => {
        entry.resolve = () => {
          if (payload.version !== server.version) {
            resolve(
              new FakeResponse(409, {
                detail: { errors: ["存档版本冲突"], version: server.version },
              }),
            );
            return;
          }
          server.version += 1;
          server.snapshot = payload.snapshot;
          resolve(new FakeResponse(200, { version: server.version }));
        };
      });
      if (fetchCtx.holdPuts) puts.push(entry);
      else queueMicrotask(() => entry.resolve());
      return entry.promise;
    }
    if (method === "POST" && pathname.endsWith("/api/import")) {
      log.push({ method, path: pathname });
      return respond(200, { clauses: parseInternalBodies(payload.text) });
    }
    if (method === "POST" && pathname.endsWith("/api/preview")) {
      log.push({ method, path: pathname, body: payload });
      return respond(200, {
        items: previewItems(payload.order_ids, payload.clauses),
        has_broken: false,
      });
    }
    return Promise.reject(new Error(`unexpected request: ${method} ${pathname}`));
  };

  // 下一个 GET /api/archive 进入哪个队列；PUT 是否挂起
  const fetchCtx = { nextGetQueue: saveGets, holdPuts: false };

  const flush = async (ticks = 1) => {
    for (let i = 0; i < ticks; i += 1) {
      await new Promise((r) => window.setTimeout(r, 0));
    }
  };

  const $ = (sel) => window.document.querySelector(sel);
  const els = {
    importText: $("#import-text"),
    importBtn: $("#import-btn"),
    archiveStatus: $("#archive-status"),
    saveBtn: $("#archive-save-btn"),
    restoreBtn: $("#archive-restore-btn"),
    restoreImportBtn: $("#archive-restore-import-btn"),
    banner: $("#archive-banner"),
    reimportBtn: $("#reimport-btn"),
  };

  const click = async (el) => {
    el.dispatchEvent(new window.Event("click", { bubbles: true, cancelable: true }));
    await flush();
  };
  const importDoc = async (text) => {
    els.importText.value = text;
    await click(els.importBtn);
    await flush(2);
  };
  const reimport = async () => click(els.reimportBtn);
  const clickSave = async () => click(els.saveBtn);

  // 本地立即改稿（上移 / 下移）：touch + 重发预览
  const moveCard = async (id, dir) => {
    const btn = window.document
      .getElementById(`card-${id}`)
      .querySelector(`[data-action="${dir}"]`);
    await click(btn);
  };

  // 放行最早一次“保存预读取”
  const settleSaveGet = async (result) => {
    const entry = saveGets.shift();
    assert.ok(entry, "没有挂起的保存预读取 GET /api/archive");
    entry.resolve(result);
    await flush(3);
  };
  const rejectSaveGet = async (err) => {
    const entry = saveGets.shift();
    assert.ok(entry, "没有挂起的保存预读取 GET /api/archive");
    entry.reject(err || new TypeError("Failed to fetch"));
    await flush(3);
  };
  // 下一个 GET 由恢复流程发起并即时返回（读 server 当前槽内容）
  const nextGetIsRestore = () => {
    fetchCtx.nextGetQueue = restoreGets;
  };
  const releasePut = async () => {
    const entry = puts.shift();
    assert.ok(entry, "没有挂起的 PUT /api/archive");
    entry.resolve();
    await flush(3);
    return entry.payload;
  };

  const archiveRequests = () => log.filter((r) => r.path.endsWith("/api/archive"));
  const methods = () => archiveRequests().map((r) => r.method);
  const lastPut = () => archiveRequests().filter((r) => r.method === "PUT").at(-1);

  // 执行 app.js（经 DOM 事件驱动）
  window.eval(appJs);

  return {
    window,
    els,
    server,
    flush,
    fetchCtx,
    click,
    importDoc,
    reimport,
    clickSave,
    moveCard,
    settleSaveGet,
    rejectSaveGet,
    nextGetIsRestore,
    releasePut,
    archiveRequests,
    methods,
    lastPut,
    pendingSaveGets: () => saveGets.length,
    pendingPuts: () => puts.length,
  };
}

const DOC_A = ["1|甲方在七日内交付设备。", "2|乙方按 [[1]] 验收后付款。"].join("\n");
const DOC_B = ["1|争议提交仲裁。", "2|适用中国法律。"].join("\n");

let h;
beforeEach(async () => {
  h = await loadHarness();
});
afterEach(() => {
  h.window.close();
});

// --------------------------------------------------------------------------- //
// 回归：换稿后迟到的预读取（GET）
// --------------------------------------------------------------------------- //
test("预读取延迟期间重新导入：旧 GET 成功后不发 PUT、不改新稿状态", async () => {
  const { els } = h;
  await h.importDoc(DOC_A);

  await h.clickSave(); // 首次保存 → 预读取挂起（世代 1）
  assert.equal(h.pendingSaveGets(), 1);
  assert.equal(els.saveBtn.textContent, "存档中…");

  await h.reimport(); // 世代 2：在途态复位，旧 GET 仍挂起
  await h.importDoc(DOC_B);
  assert.equal(h.pendingSaveGets(), 1, "旧 GET 仍未返回，不能产生新请求");
  assert.deepEqual(h.methods(), ["GET"]);
  assert.equal(els.archiveStatus.textContent, "尚未保存存档");
  assert.equal(els.saveBtn.textContent, "保存存档"); // 新稿不在等待态
  assert.equal(els.banner.hidden, true);

  // 旧 GET（属于已作废草稿）成功返回：不得触发 PUT，也不得触碰新稿任何存档状态
  await h.settleSaveGet({ version: 0, snapshot: null });
  assert.equal(h.pendingSaveGets(), 0);
  assert.deepEqual(h.methods(), ["GET"], "迟到读取后不得补发 PUT");
  assert.equal(els.archiveStatus.textContent, "尚未保存存档");
  assert.equal(els.saveBtn.textContent, "保存存档");
  assert.equal(els.banner.hidden, true);
});

test("预读取延迟期间重新导入并确认恢复存档：旧 GET 成功后作废，不发 PUT、不覆盖恢复稿", async () => {
  const { els, server } = h;
  // 服务端槽内已有存档（版本 5，恢复稿正文与当前草稿不同）
  const restoredClauses = [
    { id: "c1", body: "甲方应在三日内交付设备。" },
    { id: "c2", body: "乙方验收后付款。" },
  ];
  server.version = 5;
  server.snapshot = makeSnapshot(restoredClauses, 2);

  await h.importDoc(DOC_A);
  await h.clickSave(); // 首次保存的预读取挂起（世代 1）
  assert.equal(h.pendingSaveGets(), 1);

  // 读取延迟期间「重新导入」（世代 2，在途态复位），随后在导入页确认「恢复存档」：
  // 恢复的读取即时返回（版本 5 + 快照，confirm 已默认放行 → installArchive，世代 3）
  await h.reimport();
  h.nextGetIsRestore();
  await h.click(els.restoreImportBtn);
  assert.equal(h.pendingSaveGets(), 1, "旧保存预读取仍挂起");
  // 恢复稿已安装：版本 5、与存档一致（非未存档），无横幅
  assert.equal(els.archiveStatus.textContent, "已存档（版本 5）");
  assert.equal(els.banner.hidden, true);
  assert.match(
    h.window.document.getElementById("card-c1").textContent,
    /三日内/,
    "恢复稿正文已整份安装",
  );

  // 旧保存的预读取迟到成功：必须作废
  await h.settleSaveGet({ version: 0, snapshot: null });
  assert.deepEqual(h.methods(), ["GET", "GET"], "恢复读取 + 旧预读取；不得补发 PUT");
  assert.equal(els.archiveStatus.textContent, "已存档（版本 5）", "恢复稿版本知识不被旧读取覆盖");
  assert.equal(els.saveBtn.textContent, "保存存档");
  assert.equal(els.banner.hidden, true);
  assert.match(
    h.window.document.getElementById("card-c1").textContent,
    /三日内/,
    "恢复稿不被旧保存流程改动",
  );

  // 恢复稿上再次保存：独立走自己的 PUT（基于恢复得到的版本 5）
  h.fetchCtx.holdPuts = true;
  h.clickSave();
  await h.flush(2);
  assert.equal(h.pendingPuts(), 1);
  const payload = await h.releasePut();
  assert.equal(payload.version, 5, "再次保存基于恢复得到的版本，而非旧预读取的 0");
  assert.equal(server.version, 6);
  assert.equal(els.archiveStatus.textContent, "已存档（版本 6）");
});

test("预读取延迟期间重新导入：旧 GET 失败后不弹失败提示、不改新稿状态", async () => {
  const { els } = h;
  await h.importDoc(DOC_A);
  await h.clickSave();
  assert.equal(h.pendingSaveGets(), 1);

  await h.reimport();
  await h.importDoc(DOC_B);
  assert.equal(els.banner.hidden, true);

  await h.rejectSaveGet(); // 旧 GET 断网失败
  assert.deepEqual(h.methods(), ["GET"], "失败的迟到读取不得触发 PUT");
  assert.equal(els.archiveStatus.textContent, "尚未保存存档");
  assert.equal(els.saveBtn.textContent, "保存存档");
  assert.equal(els.banner.hidden, true, "旧读取失败不得在新稿上弹提示");
});

test("换稿后再次保存：新保存独立走 GET→PUT，旧读取收尾不解除其等待状态", async () => {
  const { els } = h;
  await h.importDoc(DOC_A);
  await h.clickSave(); // 旧预读取挂起（世代 1）

  await h.reimport();
  await h.importDoc(DOC_B);
  await h.settleSaveGet({ version: 0, snapshot: null }); // 旧 GET 迟到成功：作废
  assert.equal(els.banner.hidden, true);
  assert.deepEqual(h.methods(), ["GET"]);

  // 新稿首次保存：自己的预读取 → 成功后立即 PUT（空槽版本 0）
  await h.clickSave();
  assert.equal(h.pendingSaveGets(), 1);
  assert.equal(els.saveBtn.textContent, "存档中…");
  await h.settleSaveGet({ version: 0, snapshot: null });
  assert.deepEqual(h.methods(), ["GET", "GET", "PUT"]);
  assert.equal(h.lastPut().body.version, 0);
  assert.equal(els.archiveStatus.textContent, "已存档（版本 1）");
  assert.equal(els.banner.hidden, true);
});

// --------------------------------------------------------------------------- //
// 回归：PUT 只确认发出当刻的快照；保存期间允许改稿
// --------------------------------------------------------------------------- //
test("PUT 在途期间改稿：载荷为发出当刻快照，成功后后续修改仍标未存档", async () => {
  const { els, server, fetchCtx } = h;
  await h.importDoc(DOC_A);

  // 先完整完成一次保存（预读取即时放行），取得版本知识 1
  await h.clickSave();
  await h.settleSaveGet({ version: 0, snapshot: null });
  assert.equal(server.version, 1);
  assert.equal(els.archiveStatus.textContent, "已存档（版本 1）");

  // 改稿：c2 上移（本地立即生效，rev+1）
  await h.moveCard("c2", "up");
  assert.equal(els.archiveStatus.textContent, "有未存档的修改");

  // 第二次保存：已有版本知识 → 直接 PUT；挂起 PUT，观察“发出当刻”的载荷
  fetchCtx.holdPuts = true;
  h.clickSave(); // 不 await 完成：PUT 在途
  await h.flush(2);
  assert.equal(h.pendingPuts(), 1);
  assert.equal(els.saveBtn.textContent, "存档中…");

  // 保存期间仍允许改稿：c2 下移恢复原顺序（rev 再 +1，晚于发出当刻）
  await h.moveCard("c2", "down");
  assert.equal(els.saveBtn.textContent, "存档中…", "保存期间改稿不解除保存等待态");

  const payload = await h.releasePut();
  // 发出 PUT 当刻草稿为 [c2, c1]；期间的后续改动 [c1, c2] 不得进入载荷
  assert.deepEqual(payload.snapshot.order_ids, ["c2", "c1"]);
  assert.equal(server.version, 2);
  assert.equal(els.archiveStatus.textContent, "有未存档的修改", "PUT 当刻之后的修改仍未存档");
});

// --------------------------------------------------------------------------- //
// 防回归：预读取仍属同一草稿时的常规链路
// --------------------------------------------------------------------------- //
test("同草稿预读取失败提示、成功后 GET→PUT 正常保存", async () => {
  const { els, server } = h;
  await h.importDoc(DOC_A);

  // 预读取失败（未换稿）：横幅提示、草稿不变、可重试
  await h.clickSave();
  await h.rejectSaveGet();
  assert.deepEqual(h.methods(), ["GET"]);
  assert.equal(els.banner.hidden, false);
  assert.match(els.banner.textContent, /失败/);
  assert.equal(els.saveBtn.textContent, "保存存档");

  // 再次保存：预读取成功 → PUT 正常完成，未存档标记清除
  await h.clickSave();
  await h.settleSaveGet({ version: 0, snapshot: null });
  assert.deepEqual(h.methods(), ["GET", "GET", "PUT"]);
  assert.equal(server.version, 1);
  assert.equal(els.archiveStatus.textContent, "已存档（版本 1）");
  assert.equal(els.banner.hidden, true);
});
