/* 公告存档视图：通过 AstrBot 插件视图 bridge 读写后端存档接口。 */

const bridge = window.AstrBotPluginView;

const cardsEl = document.getElementById("cards");
const statusEl = document.getElementById("status");
const toastEl = document.getElementById("toast");

const LABELS = {
  all: "全部",
  client: "游戏版本更新",
  community: "Community Update",
  other: "其他官方公告",
};

let busy = false;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtDate(text) {
  if (!text) return "—";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? esc(text) : date.toLocaleString();
}

function toast(message, level = "ok") {
  toastEl.textContent = message;
  toastEl.className = `toast ${level}`;
  toastEl.hidden = false;
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => {
    toastEl.hidden = true;
  }, 5000);
}

function setBusy(value) {
  busy = value;
  document.body.classList.toggle("busy", value);
  document.querySelectorAll("button").forEach((btn) => {
    btn.disabled = value;
  });
}

function renderStatus(data) {
  const groups = (data.target_groups || []).join("、") || "未配置";
  const minutes = Math.round((data.poll_interval || 0) / 60);
  statusEl.textContent =
    `轮询间隔 ${minutes} 分钟 · 推送群 ${groups} · ` +
    `CM 群组 ${data.cm_group_name} · 已记录 ${data.seen_count} 个主题`;
}

function renderCards(items) {
  cardsEl.innerHTML = items
    .map((item) => {
      const body = item.has_archive
        ? `<a class="title" href="${esc(item.link)}" target="_blank" rel="noreferrer">${esc(item.title)}</a>
           <div class="meta">
             <span>主题 ID <b>${esc(item.topic_id)}</b></span>
             <span>发布者 <b>${esc(item.author)}</b></span>
             <span>发布时间 <b>${fmtDate(item.published)}</b></span>
           </div>
           <div class="meta">
             <span>图片 <b>${esc(item.images)}</b> 张</span>
             <span>原文 <b>${esc(item.raw_length)}</b> 字</span>
             <span>摘要 <b>${esc(item.summary_length)}</b> 字</span>
           </div>
           <details class="summary">
             <summary>摘要预览</summary>
             <pre>${esc(item.summary)}</pre>
           </details>`
        : `<div class="empty">暂无本地存档。点击「刷新」立即抓取论坛原文并生成摘要。</div>`;

      const disabled = busy ? " disabled" : "";
      return `<section class="card">
        <div class="card-head">
          <h2>${esc(item.label)}</h2>
          <button class="btn refresh" type="button" data-kind="${esc(item.kind)}"${disabled}>刷新</button>
        </div>
        ${body}
      </section>`;
    })
    .join("");
}

function apply(data) {
  renderStatus(data);
  renderCards(data.items || []);
}

async function load() {
  setBusy(true);
  try {
    apply(await bridge.apiGet("archive"));
  } catch (error) {
    statusEl.textContent = "读取存档失败";
    toast(`读取存档失败：${error.message}`, "err");
  } finally {
    setBusy(false);
  }
}

async function refresh(kind) {
  setBusy(true);
  toast(`正在刷新「${LABELS[kind]}」，需抓取论坛并调用 LLM，请稍候…`, "info");
  try {
    const data = await bridge.apiPost("archive/refresh", { kind });
    apply(data);
    const done = (data.refreshed || []).map((k) => LABELS[k]).join("、");
    const failed = (data.failed || []).map((k) => LABELS[k]).join("、");
    toast(
      failed ? `刷新完成：${done || "无"}；失败：${failed}` : `刷新完成：${done || "无"}`,
      failed ? "warn" : "ok",
    );
  } catch (error) {
    toast(`刷新失败：${error.message}`, "err");
  } finally {
    setBusy(false);
  }
}

function bindEvents() {
  document.getElementById("refresh-all").addEventListener("click", () => refresh("all"));
  cardsEl.addEventListener("click", (event) => {
    const btn = event.target.closest("button.refresh");
    if (btn) refresh(btn.dataset.kind);
  });
}

async function main() {
  if (!bridge) {
    statusEl.textContent = "未加载 AstrBot 视图桥接（bridge），请在 AstrBot WebUI 中打开本页面。";
    return;
  }
  bindEvents();
  await bridge.ready();
  document.title = bridge.t("views.archive-manager.title", "公告存档");
  await load();
}

main();
