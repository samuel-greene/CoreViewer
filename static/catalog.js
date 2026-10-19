let catalogUser = null;
let catalogPage = 1;
let catalogSelectedId = null;

async function configureCatalog(user) {
  catalogUser = user;
  const tab = document.getElementById("catalog-tab");
  const folders = document.getElementById("files-tab");
  try {
    const response = await fetch("/api/catalog/config");
    if (!response.ok) throw new Error("Catalog configuration could not be loaded");
    const config = await response.json();
    tab.hidden = !config.enabled || (!config.default && user.role !== "admin");
    folders.hidden = config.default && (user.role !== "admin" || !config.legacy_browser);
    document.getElementById("catalog-admin").hidden = user.role !== "admin";
    if (config.enabled) loadCatalogUploaders();
    if (config.enabled && config.default) {
      tab.click();
    } else {
      folders.click();
      if (config.enabled && user.role === "admin") loadCatalogStatus();
    }
  } catch (error) {
    tab.hidden = true;
    folders.hidden = false;
    folders.click();
  }
}

function catalogParams() {
  const params = new URLSearchParams({ page: String(catalogPage), page_size: "50" });
  const q = document.getElementById("catalog-query").value.trim();
  const from = document.getElementById("catalog-from").value;
  const through = document.getElementById("catalog-through").value;
  const verification = document.getElementById("catalog-verification").value;
  const uploader = document.getElementById("catalog-uploader").value;
  if (q) params.set("q", q);
  if (from) params.set("date_from", `${from}T00:00:00Z`);
  if (through) {
    const exclusive = new Date(`${through}T00:00:00Z`);
    exclusive.setUTCDate(exclusive.getUTCDate() + 1);
    params.set("date_to", exclusive.toISOString());
  }
  if (verification) params.set("verification", verification);
  if (uploader) params.set("uploader", uploader);
  return params;
}

function catalogDate(value, source) {
  if (!value) return "Date unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Date unknown";
  const label = source === "acquired" ? "Acquired" : source === "imported" ? "Imported" : "File modified";
  return `${label} ${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function catalogTimestamp(value) {
  if (!value) return "never";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "unknown" : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function catalogNode(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function renderCatalogRow(asset) {
  const row = catalogNode("div", "catalog-row row");
  const main = catalogNode("button", "catalog-row-main");
  main.type = "button";
  main.appendChild(catalogNode("span", "catalog-row-name", asset.filename));
  const fields = [asset.specimen_id && `Specimen ${asset.specimen_id}`,
                  asset.plan_id && `Plan ${asset.plan_id}`,
                  catalogDate(asset.date_key, asset.date_source),
                  `Uploader ${asset.uploader || "unknown"}`].filter(Boolean);
  main.appendChild(catalogNode("span", "catalog-row-meta", fields.join(" · ")));
  const status = catalogNode("span", `catalog-badge ${asset.status}`, asset.status.replace("_", " "));
  main.appendChild(status);
  main.onclick = () => {
    document.querySelectorAll(".row").forEach(item => item.classList.remove("active", "loading"));
    row.classList.add("loading");
    window.currentCatalogAsset = asset;
    openImage(asset.path, row);
    showCatalogDetail(asset.id);
  };
  row.appendChild(main);
  const actions = catalogNode("div", "catalog-row-actions");
  const detail = catalogNode("button", "conversion-secondary", "Details");
  detail.type = "button";
  detail.onclick = () => showCatalogDetail(asset.id);
  actions.appendChild(detail);
  const download = catalogNode("a", "download-btn", "Download");
  download.href = `/api/scans/${encodeURIComponent(asset.path)}/download`;
  download.download = asset.filename;
  actions.appendChild(download);
  row.appendChild(actions);
  return row;
}

async function loadCatalog() {
  if (document.getElementById("catalog-tab").hidden) return;
  const message = document.getElementById("catalog-message");
  const results = document.getElementById("catalog-results");
  message.textContent = "Searching…";
  try {
    const response = await fetch(`/api/catalog/search?${catalogParams()}`);
    if (response.status === 401) return showLoginModal();
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || "Catalog is unavailable");
    }
    const data = await response.json();
    results.replaceChildren(...data.items.map(renderCatalogRow));
    const first = data.total ? (catalogPage - 1) * data.page_size + 1 : 0;
    const last = Math.min(catalogPage * data.page_size, data.total);
    message.textContent = data.total ? `${first}–${last} of ${data.total} images` : "No images match this search";
    document.getElementById("catalog-page-label").textContent = `Page ${catalogPage}`;
    document.getElementById("catalog-prev").disabled = catalogPage <= 1;
    document.getElementById("catalog-next").disabled = last >= data.total;
    if (catalogUser?.role === "admin") loadCatalogStatus();
  } catch (error) {
    results.replaceChildren();
    message.textContent = `Catalog unavailable: ${error.message}`;
    document.getElementById("catalog-prev").disabled = true;
    document.getElementById("catalog-next").disabled = true;
  }
}

async function loadCatalogUploaders() {
  try {
    const response = await fetch("/api/catalog/uploaders");
    if (!response.ok) return;
    const users = await response.json();
    const select = document.getElementById("catalog-uploader");
    select.querySelectorAll("option[data-account]").forEach(option => option.remove());
    users.forEach(user => {
      const option = catalogNode("option", "", user.email);
      option.value = user.id;
      option.dataset.account = "true";
      select.appendChild(option);
    });
  } catch (_) {
    // Search remains usable with the Unknown filter.
  }
}

async function showCatalogDetail(id) {
  const panel = document.getElementById("catalog-detail");
  catalogSelectedId = id;
  panel.hidden = false;
  panel.textContent = "Loading record…";
  try {
    const response = await fetch(`/api/catalog/assets/${encodeURIComponent(id)}`);
    if (!response.ok) throw new Error("Could not load catalog record");
    const asset = await response.json();
    if (catalogSelectedId !== id) return;
    panel.replaceChildren();
    const heading = catalogNode("div", "catalog-detail-heading");
    heading.appendChild(catalogNode("h2", "", asset.filename));
    const close = catalogNode("button", "conversion-secondary", "Close");
    close.type = "button";
    close.onclick = () => { panel.hidden = true; catalogSelectedId = null; };
    heading.appendChild(close);
    panel.appendChild(heading);
    panel.appendChild(catalogNode("p", "catalog-detail-path", asset.path));
    const facts = [
      `Status: ${asset.status.replace("_", " ")}`,
      `Specimen: ${asset.specimen_id || "unknown"}`,
      `Plan: ${asset.plan_id || "unknown"}`,
      asset.date_source === "acquired" ? catalogDate(asset.date_key, "acquired") : null,
      catalogDate(asset.modified_at, "modified"),
      catalogDate(asset.imported_at, "imported"),
      `Dimensions: ${asset.width && asset.height ? `${asset.width} × ${asset.height}` : "unknown"}`,
      `Size: ${formatFileSize(asset.size_bytes)}`,
      `Tags: ${asset.tags.join(", ") || "none"}`,
      `Uploader: ${asset.uploader_id || "unknown"}`,
    ];
    facts.filter(Boolean).forEach(fact => panel.appendChild(catalogNode("p", "catalog-detail-fact", fact)));
    if (asset.index_error) panel.appendChild(catalogNode("p", "catalog-error", asset.index_error));
    if (catalogUser?.role === "admin") renderCatalogEditor(panel, asset);
  } catch (error) {
    panel.textContent = error.message;
  }
}

function renderCatalogEditor(panel, asset) {
  const form = catalogNode("form", "catalog-editor");
  const initial = {
    specimen_id: asset.specimen_id || "",
    plan_id: asset.plan_id || "",
    acquired_at: asset.acquired_at ? asset.acquired_at.slice(0, 16) : "",
    tags: asset.tags.join(", "),
  };
  const fields = [
    ["specimen_id", "Specimen ID", "text", initial.specimen_id],
    ["plan_id", "Plan ID", "text", initial.plan_id],
    ["acquired_at", "Acquired (UTC)", "datetime-local", initial.acquired_at],
    ["tags", "Tags (comma separated)", "text", initial.tags],
  ];
  fields.forEach(([name, labelText, type, value]) => {
    const label = catalogNode("label", "", labelText);
    const input = catalogNode("input", "");
    input.name = name;
    input.type = type;
    input.value = value;
    label.appendChild(input);
    form.appendChild(label);
  });
  const save = catalogNode("button", "conversion-primary", "Save corrections");
  save.type = "submit";
  form.appendChild(save);
  const feedback = catalogNode("p", "catalog-editor-feedback");
  form.appendChild(feedback);
  form.onsubmit = async event => {
    event.preventDefault();
    const formData = new FormData(form);
    const acquired = formData.get("acquired_at");
    const next = {
      specimen_id: String(formData.get("specimen_id")).trim() || null,
      plan_id: String(formData.get("plan_id")).trim() || null,
      acquired_at: acquired ? `${acquired}:00Z` : null,
      tags: String(formData.get("tags")).split(",").map(tag => tag.trim()).filter(Boolean),
    };
    const body = {};
    for (const key of ["specimen_id", "plan_id"]) {
      if (String(formData.get(key)).trim() !== initial[key]) body[key] = next[key];
    }
    if (acquired !== initial.acquired_at) body.acquired_at = next.acquired_at;
    if (String(formData.get("tags")).trim() !== initial.tags) body.tags = next.tags;
    if (!Object.keys(body).length) {
      feedback.textContent = "No changes to save.";
      return;
    }
    try {
      const response = await fetch(`/api/catalog/admin/assets/${encodeURIComponent(asset.id)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "Could not save corrections");
      }
      feedback.textContent = "Saved. Reverification may be required.";
      await loadCatalog();
      await showCatalogDetail(asset.id);
    } catch (error) {
      feedback.textContent = error.message;
    }
  };
  panel.appendChild(form);
  const buttons = catalogNode("div", "catalog-detail-actions");
  const verify = catalogNode("button", "conversion-secondary", "Mark verified");
  verify.type = "button";
  verify.onclick = () => catalogAction(asset.id, "verify");
  buttons.appendChild(verify);
  const retry = catalogNode("button", "conversion-secondary", "Retry indexing");
  retry.type = "button";
  retry.onclick = () => catalogAction(asset.id, "reindex");
  buttons.appendChild(retry);
  const correctedFields = Object.keys(asset.overrides || {});
  if (correctedFields.length) {
    const reset = catalogNode("button", "conversion-secondary", "Use extracted values");
    reset.type = "button";
    reset.onclick = async () => {
      try {
        const response = await fetch(`/api/catalog/admin/assets/${encodeURIComponent(asset.id)}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reset_fields: correctedFields }),
        });
        if (!response.ok) {
          const error = await response.json();
          throw new Error(error.detail || "Could not reset corrections");
        }
        await loadCatalog();
        await showCatalogDetail(asset.id);
      } catch (error) {
        feedback.textContent = error.message;
      }
    };
    buttons.appendChild(reset);
  }
  panel.appendChild(buttons);
  const original = catalogNode("details", "catalog-original");
  original.appendChild(catalogNode("summary", "", "Original extracted values and edit history"));
  original.appendChild(catalogNode("pre", "", JSON.stringify({
    extracted: asset.extracted, overrides: asset.overrides, audit: asset.audit,
  }, null, 2)));
  panel.appendChild(original);
}

async function catalogAction(id, action) {
  const message = document.getElementById("catalog-message");
  try {
    const response = await fetch(`/api/catalog/admin/assets/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.detail || `Could not ${action}`);
    }
    await loadCatalog();
    await showCatalogDetail(id);
  } catch (error) {
    message.textContent = error.message;
  }
}

async function loadCatalogStatus() {
  if (catalogUser?.role !== "admin") return;
  const status = document.getElementById("catalog-status");
  try {
    const response = await fetch("/api/catalog/admin/status");
    if (!response.ok) throw new Error("Index status unavailable");
    const data = await response.json();
    const run = data.last_run;
    status.textContent = run
      ? `Last successful scan: ${catalogTimestamp(data.last_successful_scan)} · Latest attempt: ${run.status} · ${run.seen} seen · ${run.changed} changed · ${data.index_errors} metadata errors · ${data.counts.missing || 0} missing`
      : "No catalog scan yet";
  } catch (error) {
    status.textContent = error.message;
  }
}

document.getElementById("catalog-search-form").addEventListener("submit", event => {
  event.preventDefault();
  catalogPage = 1;
  loadCatalog();
});
document.getElementById("catalog-reset").onclick = () => {
  document.getElementById("catalog-search-form").reset();
  catalogPage = 1;
  loadCatalog();
};
document.getElementById("catalog-prev").onclick = () => { catalogPage--; loadCatalog(); };
document.getElementById("catalog-next").onclick = () => { catalogPage++; loadCatalog(); };
document.getElementById("catalog-refresh-status").onclick = loadCatalogStatus;
