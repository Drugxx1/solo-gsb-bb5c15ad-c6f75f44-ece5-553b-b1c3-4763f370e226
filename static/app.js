/* 小批量标签拼版工具 —— 前端逻辑（原生 JS，无构建） */
"use strict";

const $ = (id) => document.getElementById(id);

let lastPlan = null;     // 最近一次 /api/layout 返回的拼版结果
let lastParams = null;   // 对应的请求参数
let currentPage = 1;
let calibrationData = null;  // 已通过求解校验的校准录入（导出时随 /api/svg 发送）
let reprintMode = false;     // 原位补印模式开关
const reprintSel = new Map(); // 补印选择：页码 -> Set（0 基页内序号）；界面角标与 API 序号为 +1

/* ---------------- 标签行 ---------------- */

function addLabelRow(spec) {
  const data = spec || {
    name: "", width: 60, height: 30, count: 10, allow_rotate: false,
    numbering: null,
  };
  const num = data.numbering || { prefix: "", start: 1, pad: 3 };
  const block = document.createElement("div");
  block.className = "label-block";
  block.innerHTML = `
    <div class="label-row">
      <input type="text" class="l-name" placeholder="标签名称" value="${escapeAttr(data.name)}" />
      <input type="number" class="l-w" value="${data.width}" step="0.5" min="0.1" title="宽 mm" />
      <input type="number" class="l-h" value="${data.height}" step="0.5" min="0.1" title="高 mm" />
      <input type="number" class="l-c" value="${data.count}" step="1" min="1" title="份数" />
      <label class="rot-cell" title="允许旋转 90°">
        <input type="checkbox" class="l-r" ${data.allow_rotate ? "checked" : ""} />旋转
      </label>
      <label class="num-cell" title="启用连续流水号，编号印在标签内部">
        <input type="checkbox" class="l-num" ${data.numbering ? "checked" : ""} />编号
      </label>
      <button type="button" class="del" title="删除">✕</button>
    </div>
    <div class="numbering-row" ${data.numbering ? "" : "hidden"}>
      <label class="np-prefix">前缀
        <input type="text" class="n-prefix" value="${escapeAttr(num.prefix)}" placeholder="如 P-" maxlength="20" />
      </label>
      <label class="np-start">起始整数
        <input type="number" class="n-start" value="${num.start}" step="1" min="0" />
      </label>
      <label class="np-pad">补零位数
        <input type="number" class="n-pad" value="${num.pad}" step="1" min="0" max="12" />
      </label>
    </div>
  `;
  block.querySelector(".del").addEventListener("click", () => block.remove());
  block.querySelector(".l-num").addEventListener("change", (e) => {
    block.querySelector(".numbering-row").hidden = !e.target.checked;
  });
  $("label-rows").appendChild(block);
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

/* ---------------- 首张纸占用区行 ---------------- */

function addZoneRow(zone) {
  const data = zone || { x: 20, y: 20, width: 80, height: 40 };
  const row = document.createElement("div");
  row.className = "zone-row";
  row.innerHTML = `
    <span class="z-idx"></span>
    <input type="number" class="z-x" value="${data.x}" step="0.5" title="左上角 X（距纸左）mm" />
    <input type="number" class="z-y" value="${data.y}" step="0.5" title="左上角 Y（距纸顶）mm" />
    <input type="number" class="z-w" value="${data.width}" step="0.5" min="0.1" title="占用区宽 mm" />
    <input type="number" class="z-h" value="${data.height}" step="0.5" min="0.1" title="占用区高 mm" />
    <button type="button" class="del" title="删除">✕</button>
  `;
  row.querySelector(".del").addEventListener("click", () => {
    row.remove();
    refreshZoneIndex();
  });
  $("zone-rows").appendChild(row);
  refreshZoneIndex();
}

function refreshZoneIndex() {
  document.querySelectorAll(".zone-row").forEach((row, i) => {
    row.querySelector(".z-idx").textContent = `#${i + 1}`;
  });
}

/* ---------------- 参数收集 ---------------- */

function num(id) {
  return parseFloat($(id).value);
}

function collectParams() {
  const labels = [...document.querySelectorAll(".label-block")].map((block) => {
    const row = block.querySelector(".label-row");
    const spec = {
      name: row.querySelector(".l-name").value.trim(),
      width: parseFloat(row.querySelector(".l-w").value),
      height: parseFloat(row.querySelector(".l-h").value),
      count: parseInt(row.querySelector(".l-c").value, 10),
      allow_rotate: row.querySelector(".l-r").checked,
      numbering: null,
    };
    if (row.querySelector(".l-num").checked) {
      const nrow = block.querySelector(".numbering-row");
      spec.numbering = {
        prefix: nrow.querySelector(".n-prefix").value,
        start: parseInt(nrow.querySelector(".n-start").value, 10),
        pad: parseInt(nrow.querySelector(".n-pad").value, 10),
      };
    }
    return spec;
  });
  const occupied_zones = [...document.querySelectorAll(".zone-row")].map((row) => ({
    x: parseFloat(row.querySelector(".z-x").value),
    y: parseFloat(row.querySelector(".z-y").value),
    width: parseFloat(row.querySelector(".z-w").value),
    height: parseFloat(row.querySelector(".z-h").value),
  }));
  return {
    page_width: num("page-width"),
    page_height: num("page-height"),
    margin_top: num("margin-top"),
    margin_right: num("margin-right"),
    margin_bottom: num("margin-bottom"),
    margin_left: num("margin-left"),
    gap_x: num("gap-x"),
    gap_y: num("gap-y"),
    labels,
    occupied_zones,
  };
}

function validateParams(p) {
  const bad = (msg) => { throw new Error(msg); };
  for (const [name, v] of [
    ["纸宽", p.page_width], ["纸高", p.page_height],
  ]) if (!(v > 0)) bad(`${name}必须为正数。`);
  for (const z of p.occupied_zones) {
    if ([z.x, z.y, z.width, z.height].some((v) => !Number.isFinite(v))) {
      bad("占用区的 X / Y / 宽 / 高必须全部填写为数字。");
    }
    if (z.x < 0 || z.y < 0) bad("占用区左上角 X / Y 不能为负数。");
    if (z.width <= 0 || z.height <= 0) bad("占用区宽、高必须为正数。");
    if (z.x + z.width > p.page_width + 1e-6) {
      bad(`占用区越界：X + 宽 = ${+(z.x + z.width).toFixed(2)}mm，超过纸张宽度 ${p.page_width}mm。`);
    }
    if (z.y + z.height > p.page_height + 1e-6) {
      bad(`占用区越界：Y + 高 = ${+(z.y + z.height).toFixed(2)}mm，超过纸张高度 ${p.page_height}mm。`);
    }
  }
  for (const lab of p.labels) {
    if (!lab.numbering) continue;
    const n = lab.numbering;
    if (!Number.isInteger(n.start) || n.start < 0) {
      bad(`标签「${lab.name || "未命名"}」的编号起始整数必须是不小于 0 的整数。`);
    }
    if (!Number.isInteger(n.pad) || n.pad < 0 || n.pad > 12) {
      bad(`标签「${lab.name || "未命名"}」的补零位数必须是 0–12 之间的整数。`);
    }
    if (typeof n.prefix !== "string" || [...n.prefix].length > 20) {
      bad(`标签「${lab.name || "未命名"}」的编号前缀不能超过 20 个字符。`);
    }
  }
}

async function apiError(resp, fallback) {
  let detail;
  try { detail = (await resp.json()).detail; } catch { detail = null; }
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("；") || fallback;
  return detail || fallback;
}

async function fetchSvg(page, { showOccupied = false, withCalibration = false } = {}) {
  const body = {
    page,
    show_margins: $("show-margins").checked,
    show_annotations: $("show-annotations").checked,
    show_occupied: showOccupied,
    params: lastParams,
  };
  // 仅下载 / 打印携带校准；页面常规预览不带，坐标保持原样
  if (withCalibration) {
    const cal = activeCalibration();
    if (cal) body.calibration = cal;
  }
  const resp = await fetch("/api/svg", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(await apiError(resp, "SVG 生成失败"));
  return resp.text();
}

async function renderPage() {
  if (!lastPlan) return;
  const host = $("svg-host");
  try {
    // 预览显示灰色占用区（仅第一页有），与下载/打印的 SVG 坐标完全一致
    const svg = await fetchSvg(currentPage, { showOccupied: true });
    host.innerHTML = svg;
    if (reprintMode) applyReprintOverlay();
  } catch (err) {
    host.innerHTML =
      `<div class="svg-error"><strong>无法生成本页 SVG：</strong>${escapeHtml(err.message)}</div>`;
  }
  $("page-info").textContent = `${currentPage} / ${lastPlan.pages.length}`;
  $("prev-page").disabled = currentPage <= 1;
  $("next-page").disabled = currentPage >= lastPlan.pages.length;
  updateReprintStatus();
}

/* ---------------- 原位补印：预览点选与导出 ---------------- */

const SVG_NS = "http://www.w3.org/2000/svg";

function setReprintMode(on) {
  reprintMode = on;
  for (const id of ["reprint-hint", "reprint-status", "reprint-clear", "reprint-export"]) {
    $(id).hidden = !on;
  }
  if (on) applyReprintOverlay();
  else removeReprintOverlay();
  updateReprintStatus();
}

function applyReprintOverlay() {
  if (!lastPlan) return;
  const host = $("svg-host");
  const svg = host.querySelector("svg");
  const page = lastPlan.pages[currentPage - 1];
  if (!svg || !page) return;
  removeReprintOverlay();
  const sel = reprintSel.get(currentPage) || new Set();
  const layer = document.createElementNS(SVG_NS, "g");
  layer.setAttribute("class", "reprint-layer");
  page.labels.forEach((lab, idx) => {
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("class", `rp-hit${sel.has(idx) ? " sel" : ""}`);
    rect.setAttribute("x", lab.x);
    rect.setAttribute("y", lab.y);
    rect.setAttribute("width", lab.width);
    rect.setAttribute("height", lab.height);
    rect.dataset.idx = idx;
    const tag = document.createElementNS(SVG_NS, "text");
    tag.setAttribute("class", `rp-idx${sel.has(idx) ? " sel" : ""}`);
    tag.setAttribute("x", Math.min(lab.x + 0.9, lab.x + lab.width - 1));
    tag.setAttribute("y", lab.y + 2.4);
    tag.dataset.idx = idx;
    tag.textContent = `#${idx + 1}`;
    layer.appendChild(rect);
    layer.appendChild(tag);
  });
  svg.appendChild(layer);
}

function removeReprintOverlay() {
  document.querySelectorAll("#svg-host g.reprint-layer").forEach((g) => g.remove());
}

function toggleReprintSelection(page, idx) {
  let sel = reprintSel.get(page);
  if (!sel) { sel = new Set(); reprintSel.set(page, sel); }
  const on = !sel.has(idx);
  if (on) sel.add(idx); else sel.delete(idx);
  if (!sel.size) reprintSel.delete(page);
  // 只切换本页该序号元素的高亮，不重新请求 SVG（坐标不变）
  document
    .querySelectorAll(`#svg-host .reprint-layer [data-idx="${idx}"]`)
    .forEach((el) => el.classList.toggle("sel", on));
  updateReprintStatus();
}

function updateReprintStatus() {
  const el = $("reprint-status");
  if (!reprintMode || !lastPlan) { el.textContent = ""; return; }
  const page = lastPlan.pages[currentPage - 1];
  const sel = reprintSel.get(currentPage);
  const n = sel ? sel.size : 0;
  let other = 0;
  for (const [p, s] of reprintSel) if (p !== currentPage) other += s.size;
  el.innerHTML =
    `本页已选 <strong>${n}</strong> / ${page ? page.labels.length : 0} 个` +
    (other ? `（其他页共 ${other} 个，补印仅导出当前页）` : "");
}

async function exportReprintSvg() {
  if (!lastPlan) return;
  const sel = reprintSel.get(currentPage);
  if (!sel || !sel.size) {
    alert("补印选择为空：请先在本页预览中点击需要补印的标签（可点选多个），再导出本页补印 SVG。");
    return;
  }
  const body = {
    page: currentPage,
    indexes: [...sel].sort((a, b) => a - b).map((i) => i + 1), // API 序号从 1 开始
    show_annotations: $("show-annotations").checked,
    params: lastParams,
  };
  // 补印属于导出 / 打印动作：勾选校准补偿时同样携带校准数据
  try {
    const cal = activeCalibration();
    if (cal) body.calibration = cal;
  } catch (err) {
    alert(err.message);
    return;
  }
  try {
    const resp = await fetch("/api/reprint", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(await apiError(resp, "补印 SVG 生成失败"));
    downloadBlob(await resp.text(), `reprint-page-${currentPage}.svg`, "image/svg+xml");
  } catch (err) {
    alert(err.message);
  }
}

/* ---------------- 结果汇总 ---------------- */

function renderSummary(plan) {
  const el = $("summary");
  el.classList.remove("empty");
  const allPlaced = plan.total_placed === plan.total_requested;
  const zoneInfo = plan.occupied_zones.length
    ? `，首张纸含 <strong>${plan.occupied_zones.length}</strong> 个占用区（灰色）`
    : "";
  let html = `<div class="totals">
      共 <strong>${plan.total_requested}</strong> 个标签，
      排入 <strong class="ok">${plan.total_placed}</strong> 个，
      使用 <strong>${plan.pages.length}</strong> 张纸${zoneInfo}
      ${allPlaced ? "（全部排入 ✔）" : `，<strong style="color:var(--danger)">${plan.total_requested - plan.total_placed} 个未排入</strong>`}
    </div>
    <table>
      <thead><tr>
        <th>标签</th><th>规格 (mm)</th><th>需求</th><th>排入</th><th>其中旋转</th><th>未排入</th><th>本次编号范围</th>
      </tr></thead><tbody>`;
  for (const s of plan.summaries) {
    let range = "—";
    if (s.numbered) {
      if (s.number_first === null || s.number_last === null) {
        range = `<span class="muted-num">已启用，本次无排入（不消耗编号）</span>`;
      } else if (s.number_first === s.number_last) {
        range = `<span class="num-range">${escapeHtml(s.number_first)}</span>`;
      } else {
        range = `<span class="num-range">${escapeHtml(s.number_first)} – ${escapeHtml(s.number_last)}</span>`;
      }
    }
    html += `<tr class="${s.unplaced > 0 ? "unplaced" : ""}">
      <td>${escapeHtml(s.name || `#${s.type_index + 1}`)}</td>
      <td>${s.width} × ${s.height}</td>
      <td>${s.requested}</td>
      <td>${s.placed}</td>
      <td>${s.rotated}</td>
      <td>${s.unplaced > 0 ? s.unplaced : "—"}</td>
      <td class="num-cell-out">${range}</td>
    </tr>`;
  }
  html += "</tbody></table>";
  if (plan.warnings.length) {
    html += `<div class="warn"><strong>提示：</strong><ul>${
      plan.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")
    }</ul></div>`;
  }
  el.innerHTML = html;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- 打印位置校准 ---------------- */

function calRow(tag, cls, design) {
  const row = document.createElement("div");
  row.className = "cal-row";
  row.innerHTML = `
    <span class="z-idx">${tag}</span>
    <input type="number" class="${cls}-design" value="${design}" step="0.1" title="设计坐标 mm" />
    <input type="number" class="${cls}-measured" step="0.1" placeholder="实测" title="纸上实测坐标 mm" />
  `;
  return row;
}

function fillCalMarks(marks) {
  const hosts = [
    [$("cal-x-rows"), "X", "cx", marks.x_marks],
    [$("cal-y-rows"), "Y", "cy", marks.y_marks],
  ];
  for (const [host, tag, cls, designs] of hosts) {
    host.innerHTML = "";
    designs.forEach((d, i) => host.appendChild(calRow(`${tag}${i + 1}`, cls, d)));
  }
  invalidateCalibration("");
}

async function refreshCalMarks(note) {
  const w = num("page-width"), h = num("page-height");
  if (!(w > 0) || !(h > 0)) return;
  try {
    const resp = await fetch(
      `/api/calibration/marks?page_width=${w}&page_height=${h}`
    );
    if (!resp.ok) return;
    const marks = await resp.json();
    // 设计坐标未变化（如同纸张下再次点下载测试页）时保留已填实测值
    const cur = (sel) =>
      [...document.querySelectorAll(sel)].map((el) => parseFloat(el.value));
    const unchanged =
      JSON.stringify([marks.x_marks, marks.y_marks]) ===
      JSON.stringify([cur(".cx-design"), cur(".cy-design")]);
    if (!unchanged) fillCalMarks(marks);
    if (note) calNote(note);
  } catch { /* 后端不可用时保持表格原状 */ }
}

function collectCalibration() {
  const read = (sel) =>
    [...document.querySelectorAll(sel)].map((el) => parseFloat(el.value));
  const xd = read(".cx-design"), xm = read(".cx-measured");
  const yd = read(".cy-design"), ym = read(".cy-measured");
  if ([...xd, ...xm, ...yd, ...ym].some((v) => !Number.isFinite(v))) return null;
  return {
    x_lines: xd.map((d, i) => ({ design: d, measured: xm[i] })),
    y_lines: yd.map((d, i) => ({ design: d, measured: ym[i] })),
  };
}

function calResultHtml(cls, html) {
  const el = $("cal-result");
  el.className = `cal-result ${cls}`;
  el.innerHTML = html;
}
const calOk = (msg) => calResultHtml("ok", msg);
const calError = (msg) => calResultHtml("err", `<strong>无法校准：</strong>${escapeHtml(msg)}`);
const calNote = (msg) => calResultHtml("muted", escapeHtml(msg));

function invalidateCalibration(msg) {
  calibrationData = null;
  if (msg) calNote(msg);
}

function activeCalibration() {
  if (!$("cal-enabled").checked) return null;
  if (!calibrationData) {
    throw new Error(
      "已勾选应用校准，但尚无有效校准参数：请完整填写两组实测坐标后点击「② 计算 X / Y 校准参数」。"
    );
  }
  return calibrationData;
}

/* ---------------- 事件 ---------------- */

$("add-label").addEventListener("click", () => addLabelRow());
$("add-zone").addEventListener("click", () => addZoneRow());

$("preset").addEventListener("change", (e) => {
  if (!e.target.value) return;
  const [w, h] = e.target.value.split(",").map(parseFloat);
  $("page-width").value = w;
  $("page-height").value = h;
  refreshCalMarks("纸张预设已切换，设计坐标已更新；请重新打印测试页并测量。");
});
$("page-width").addEventListener("change", () =>
  refreshCalMarks("纸张尺寸已修改，设计坐标已更新；请重新打印测试页并测量。"));
$("page-height").addEventListener("change", () =>
  refreshCalMarks("纸张尺寸已修改，设计坐标已更新；请重新打印测试页并测量。"));

$("cal-download").addEventListener("click", async () => {
  const w = num("page-width"), h = num("page-height");
  if (!(w > 0) || !(h > 0)) { calError("请先填写有效的纸张尺寸。"); return; }
  await refreshCalMarks("");
  window.open(`/api/calibration/page?page_width=${w}&page_height=${h}`, "_blank");
  calNote("测试页已在新窗口打开：请 100% 缩放打印，测量后将读数填入实测列。");
});

$("cal-solve").addEventListener("click", async () => {
  const data = collectCalibration();
  if (!data) {
    calError("请完整填写两组刻线的设计坐标与纸上实测坐标。");
    return;
  }
  try {
    const resp = await fetch("/api/calibration/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      calibrationData = null;
      $("cal-enabled").checked = false;
      calError(await apiError(resp, "校准求解失败"));
      return;
    }
    const solved = await resp.json();
    calibrationData = data;
    $("cal-enabled").checked = true;
    calOk(
      "X 方向：缩放 <strong>" + solved.x.scale + "</strong>、偏移 "
      + "<strong>" + solved.x.offset + "</strong>mm；"
      + "Y 方向：缩放 <strong>" + solved.y.scale + "</strong>、偏移 "
      + "<strong>" + solved.y.offset + "</strong>mm。"
      + "下载 / 打印的 SVG 将按逆变换补偿标签轮廓、裁切线与标注。"
    );
  } catch (err) {
    calError(err.message);
  }
});

// 表格行整体随纸张动态重建，输入监听委托给固定的行容器
$("cal-x-rows").addEventListener("input", () =>
  invalidateCalibration("校准数据已修改，请重新「计算 X / Y 校准参数」。"));
$("cal-y-rows").addEventListener("input", () =>
  invalidateCalibration("校准数据已修改，请重新「计算 X / Y 校准参数」。"));

$("params-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  lastParams = collectParams();
  currentPage = 1;
  try {
    validateParams(lastParams);
    const resp = await fetch("/api/layout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastParams),
    });
    if (!resp.ok) {
      const detail = (await resp.json()).detail;
      throw new Error(Array.isArray(detail)
        ? detail.map((d) => d.msg).join("；")
        : detail);
    }
    lastPlan = await resp.json();
    // 参数已重新提交：旧页码 / 页内序号可能失效，清空全部补印选择
    reprintSel.clear();
    renderSummary(lastPlan);
    $("preview-wrap").hidden = false;
    await renderPage();
  } catch (err) {
    lastPlan = null;
    $("preview-wrap").hidden = true;
    $("summary").className = "summary";
    $("summary").innerHTML =
      `<div class="warn"><strong>参数错误：</strong>${escapeHtml(err.message)}</div>`;
  }
});

$("prev-page").addEventListener("click", async () => {
  if (currentPage > 1) { currentPage--; await renderPage(); }
});
$("next-page").addEventListener("click", async () => {
  if (lastPlan && currentPage < lastPlan.pages.length) {
    currentPage++; await renderPage();
  }
});

$("show-margins").addEventListener("change", renderPage);
$("show-annotations").addEventListener("change", renderPage);

// 原位补印：模式开关、预览点选（事件委托到固定的预览容器）、清空与导出
$("reprint-mode").addEventListener("change", (e) => setReprintMode(e.target.checked));
$("svg-host").addEventListener("click", (e) => {
  if (!reprintMode) return;
  const hit = e.target.closest ? e.target.closest(".rp-hit") : null;
  if (!hit) return;
  toggleReprintSelection(currentPage, parseInt(hit.dataset.idx, 10));
});
$("reprint-clear").addEventListener("click", () => {
  reprintSel.delete(currentPage);
  document
    .querySelectorAll("#svg-host .reprint-layer .sel")
    .forEach((el) => el.classList.remove("sel"));
  updateReprintStatus();
});
$("reprint-export").addEventListener("click", exportReprintSvg);

function downloadBlob(text, filename, type) {
  const blob = new Blob([text], { type });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

$("download-svg").addEventListener("click", async () => {
  try {
    const svg = await fetchSvg(currentPage, { withCalibration: true });
    downloadBlob(svg, `labels-page-${currentPage}.svg`, "image/svg+xml");
  } catch (err) {
    alert(err.message);
  }
});

$("download-all").addEventListener("click", async () => {
  try {
    activeCalibration();  // 提前校验校准勾选状态，避免中途失败
    for (let p = 1; p <= lastPlan.pages.length; p++) {
      const svg = await fetchSvg(p, { withCalibration: true });
      downloadBlob(svg, `labels-page-${p}.svg`, "image/svg+xml");
      await new Promise((r) => setTimeout(r, 150)); // 避免浏览器拦截批量下载
    }
  } catch (err) {
    alert(err.message);
  }
});

$("print-all").addEventListener("click", async () => {
  let svgs;
  try {
    activeCalibration();
    svgs = [];
    for (let p = 1; p <= lastPlan.pages.length; p++) {
      svgs.push(await fetchSvg(p, { withCalibration: true }));
    }
  } catch (err) {
    alert(err.message);
    return;
  }
  const w = lastPlan.page_width, h = lastPlan.page_height;
  const win = window.open("", "_blank");
  if (!win) { alert("浏览器拦截了打印窗口，请允许弹窗后重试。"); return; }
  win.document.write(`<!DOCTYPE html><html><head><meta charset="utf-8"><title>打印标签</title>
    <style>
      @page { size: ${w}mm ${h}mm; margin: 0; }
      html,body { margin:0; padding:0; }
      .sheet { width:${w}mm; height:${h}mm; overflow:hidden; page-break-after:always; }
      .sheet:last-child { page-break-after:auto; }
      svg { display:block; width:${w}mm; height:${h}mm; }
    </style></head><body>
    ${svgs.map((s) => `<div class="sheet">${s}</div>`).join("")}
    </body></html>`);
  win.document.close();
  win.focus();
  setTimeout(() => win.print(), 400);
});

/* ---------------- 初始数据 ---------------- */

addLabelRow({
  name: "品名标签", width: 60, height: 30, count: 20, allow_rotate: true,
  numbering: { prefix: "P-", start: 1, pad: 3 },
});
addLabelRow({ name: "箱贴", width: 100, height: 70, count: 6, allow_rotate: false });
addLabelRow({
  name: "小标", width: 40, height: 20, count: 50, allow_rotate: true,
  numbering: { prefix: "", start: 100, pad: 0 },
});

// 按默认纸张尺寸预填校准测试页的设计坐标（实测列留空）
refreshCalMarks("");

/* ---------------- 纸张库存与领料 ---------------- */

function genOrderNo() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const date = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`;
  const time = `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
  return `LL-${date}-${time}`;
}

function fmtDateTime(s) {
  if (!s) return "—";
  // 后端 ISO（本地时间，形如 2026-09-25T14:03:07）显示为 2026-09-25 14:03:07
  return s.replace("T", " ");
}

async function refreshStock() {
  const tbody = $("stock-rows");
  try {
    const resp = await fetch("/api/stock");
    if (!resp.ok) throw new Error("库存查询失败");
    const items = await resp.json();
    if (!items.length) {
      tbody.innerHTML =
        `<tr class="empty-row"><td colspan="4">尚无库存：请在上方按纸宽、纸高登记入库。</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map((it) => `
      <tr>
        <td>${it.paper_width}</td>
        <td>${it.paper_height}</td>
        <td class="${it.quantity <= 0 ? "zero" : ""}">${it.quantity}</td>
        <td>${fmtDateTime(it.updated_at)}</td>
      </tr>`).join("");
  } catch (err) {
    tbody.innerHTML =
      `<tr class="empty-row"><td colspan="4">库存加载失败：${escapeHtml(err.message)}</td></tr>`;
  }
}

async function refreshRequisitions() {
  const tbody = $("requisition-rows");
  try {
    const resp = await fetch("/api/requisitions?limit=10");
    if (!resp.ok) throw new Error("领料记录查询失败");
    const items = await resp.json();
    if (!items.length) {
      tbody.innerHTML =
        `<tr class="empty-row"><td colspan="6">尚无领料记录：确认领料后这里会显示最近 10 条。</td></tr>`;
      return;
    }
    tbody.innerHTML = items.map((r) => `
      <tr>
        <td>${escapeHtml(r.order_no)}${r.reused ? '<span class="reused-tag">重试返回原记录</span>' : ""}</td>
        <td>${r.paper_width} × ${r.paper_height}</td>
        <td>${r.pages_required}</td>
        <td>${r.labels_placed} / ${r.labels_requested}</td>
        <td>${r.stock_before} → ${r.stock_after}</td>
        <td>${fmtDateTime(r.created_at)}</td>
      </tr>`).join("");
  } catch (err) {
    tbody.innerHTML =
      `<tr class="empty-row"><td colspan="6">领料记录加载失败：${escapeHtml(err.message)}</td></tr>`;
  }
}

function showRequisitionResult(kind, html) {
  const box = $("requisition-result");
  box.className = `req-result ${kind}`;
  box.innerHTML = html;
}

async function registerStock() {
  const paper_width = parseFloat($("stock-width").value);
  const paper_height = parseFloat($("stock-height").value);
  const quantity = parseInt($("stock-qty").value, 10);
  if (!(paper_width > 0) || !(paper_height > 0)) {
    alert("纸宽、纸高必须为正数（毫米）。");
    return;
  }
  if (!Number.isInteger(quantity) || quantity <= 0) {
    alert("入库张数必须为正整数。");
    return;
  }
  const resp = await fetch("/api/stock", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_width, paper_height, quantity }),
  });
  if (!resp.ok) {
    alert(await apiError(resp, "库存登记失败"));
    return;
  }
  const item = await resp.json();
  showRequisitionResult(
    "ok",
    `已登记入库 <strong>${quantity}</strong> 张：纸型 <strong>${item.paper_width}×${item.paper_height}mm</strong>，` +
    `当前余量 <strong class="big">${item.quantity}</strong> 张。`
  );
  await refreshStock();
}

async function confirmRequisition() {
  const order_no = $("order-no").value.trim();
  if (!order_no) {
    alert("请填写领料单号（可用「换一个」生成）。");
    return;
  }
  // 用左侧表单的当前参数提交；服务端会重新计算拼版与实际页数
  const params = collectParams();
  try {
    validateParams(params);
  } catch (err) {
    showRequisitionResult("err", `<strong>参数错误：</strong>${escapeHtml(err.message)}`);
    return;
  }
  const box = $("requisition-result");
  box.className = "req-result muted";
  box.textContent = "正在由服务端重新计算拼版并核对库存…";
  let resp;
  try {
    resp = await fetch("/api/requisitions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_no, params }),
    });
  } catch (err) {
    showRequisitionResult("err", `<strong>请求失败：</strong>${escapeHtml(err.message)}`);
    return;
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    showRequisitionResult(
      "err",
      `<strong>领料未确认（${resp.status === 409 ? "冲突 / 库存不满足" : "参数错误"}）：</strong>` +
      `${escapeHtml(data.detail || "未知错误")}`
    );
    await Promise.all([refreshStock(), refreshRequisitions()]);
    return;
  }
  const tag = data.reused
    ? `<span class="reused-tag">同单号重试 · 未重复扣纸</span>`
    : '<span class="reused-tag" style="background:#d1fae5;color:#065f46">领料已确认 · 已扣纸</span>';
  showRequisitionResult(
    data.reused ? "reuse" : "ok",
    `<div>${tag}</div>` +
    `领料单 <strong>${escapeHtml(data.order_no)}</strong>：纸型 <strong>${data.paper_width}×${data.paper_height}mm</strong>，` +
    `服务端重算共 <strong class="big">${data.pages_required}</strong> 张纸，` +
    `标签排入 <strong>${data.labels_placed}/${data.labels_requested}</strong>；<br/>` +
    `余量 ${data.stock_before} → <strong class="big">${data.stock_after}</strong> 张。` +
    `<div class="muted">确认时间：${fmtDateTime(data.created_at)}</div>`
  );
  await Promise.all([refreshStock(), refreshRequisitions()]);
}

// 初始化与事件
$("order-no").value = genOrderNo();
$("order-regen").addEventListener("click", () => { $("order-no").value = genOrderNo(); });
$("stock-use-page").addEventListener("click", () => {
  $("stock-width").value = $("page-width").value;
  $("stock-height").value = $("page-height").value;
});
$("stock-register").addEventListener("click", registerStock);
$("requisition-confirm").addEventListener("click", confirmRequisition);

refreshStock();
refreshRequisitions();
