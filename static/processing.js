// Folder submission and persisted conversion status. Image viewing stays in Files.
const selectedFolders = new Map();
let processingAllowed = false;
let submittingConversion = false;
let processingTimer = null;
let processingRequestActive = false;
let incomingRequestNumber = 0;
let inferenceAvailable = false;

function setInferenceCapability(inference) {
  inferenceAvailable = inference?.available === true;
  document.getElementById("cyclegan-option").disabled = !inferenceAvailable;
  document.getElementById("inference-model").textContent = inferenceAvailable
    ? "Default model: " + inference.model_name
    : (inference?.reason || "CycleGAN inference is not available yet.");
  updateConversionSelection();
}

function setProcessingUser(user) {
  processingAllowed = user.can_process === true;
  clearInterval(processingTimer);
  processingTimer = null;
  updateConversionSelection();
  if (processingAllowed) {
    loadProcessing();
    processingTimer = setInterval(() => {
      if (!document.hidden) loadProcessing();
    }, 3000);
  } else {
    setInferenceCapability(null);
    document.getElementById("processing-message").textContent =
      "This account is not permitted to submit or track conversions.";
    document.getElementById("processing-message").hidden = false;
    document.getElementById("processing-empty").hidden = true;
    document.getElementById("processing-jobs").replaceChildren();
    document.querySelector(".processing-count").textContent = "0";
  }
}

function updateConversionSelection() {
  const count = selectedFolders.size;
  document.getElementById("selection-count").textContent =
    count + (count === 1 ? " folder selected" : " folders selected");
  const generate = document.getElementById("generate-ome");
  const inference = document.getElementById("processing-operation").value === "cyclegan";
  generate.disabled = !processingAllowed || count === 0 || submittingConversion || (inference && !inferenceAvailable);
  generate.textContent = submittingConversion ? "Submitting…" : (inference ? "Generate virtual H&E" : "Generate OME-TIFF");
  document.getElementById("processing-operation").disabled = !processingAllowed || submittingConversion;
  document.getElementById("clear-selection").disabled = count === 0 || submittingConversion;
  document.querySelectorAll("[data-scan-folder]").forEach(checkbox => {
    checkbox.checked = selectedFolders.has(checkbox.dataset.scanFolder);
  });
}

function selectConversionFolder(path, checked) {
  if (checked) selectedFolders.set(path, path);
  else selectedFolders.delete(path);
  updateConversionSelection();
}

function showProcessingTab() {
  document.getElementById("processing-tab").click();
}

async function readConversionResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) showLoginModal();
    throw new Error(typeof data.detail === "string" ? data.detail : "Conversion request failed");
  }
  return data;
}

async function loadIncomingFolders(prefix = "") {
  const requestNumber = ++incomingRequestNumber;
  const message = document.getElementById("incoming-message");
  const list = document.getElementById("incoming-file-list");
  try {
    const response = await fetch("/api/incoming?" + new URLSearchParams({prefix}));
    const data = await readConversionResponse(response);
    if (requestNumber !== incomingRequestNumber) return;
    processingAllowed = data.can_submit;
    message.hidden = processingAllowed;
    message.textContent = "This account is not permitted to submit conversions.";
    renderBreadcrumbs(data.current_path, "incoming");
    list.replaceChildren();
    if (data.parent_prefix !== null) {
      list.appendChild(createRow("[DIR]", "..",
        () => loadIncomingFolders(data.parent_prefix), null, null, {isParentNav: true}));
    }
    for (const folder of data.directories) {
      const row = document.createElement("div");
      row.className = "row conversion-folder";
      const path = folder.path.replace(/\/$/, "");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.scanFolder = path;
      checkbox.disabled = !processingAllowed || !folder.selectable;
      checkbox.setAttribute("aria-label", "Select " + folder.name);
      checkbox.title = folder.reason;
      checkbox.addEventListener("change", () => selectConversionFolder(path, checkbox.checked));
      const details = document.createElement("div");
      details.className = "folder-details";
      const name = document.createElement("button");
      name.type = "button";
      name.className = "folder-link name-col";
      name.textContent = folder.name;
      name.title = "Browse " + folder.name;
      name.addEventListener("click", () => loadIncomingFolders(folder.path));
      const readiness = document.createElement("span");
      readiness.className = "folder-readiness";
      readiness.textContent = folder.reason;
      details.append(name, readiness);
      row.append(checkbox, details);
      list.appendChild(row);
    }
    const current = data.current_folder;
    const currentLabel = document.getElementById("incoming-current-folder");
    const currentCheckbox = document.getElementById("incoming-current-checkbox");
    currentLabel.hidden = !current.selectable;
    currentCheckbox.dataset.scanFolder = current.path;
    currentCheckbox.disabled = !processingAllowed;
    currentCheckbox.onchange = () => selectConversionFolder(current.path, currentCheckbox.checked);
    updateConversionSelection();
    filterIncomingFolders();
  } catch (error) {
    if (requestNumber !== incomingRequestNumber) return;
    message.textContent = error.message;
    message.hidden = false;
    list.replaceChildren();
    document.getElementById("incoming-current-folder").hidden = true;
    document.getElementById("incoming-no-results").hidden = true;
  }
}

function filterIncomingFolders() {
  const query = document.getElementById("incoming-search-box").value.trim().toLowerCase();
  let visible = 0;
  document.querySelectorAll("#incoming-file-list .conversion-folder").forEach(row => {
    row.hidden = !row.querySelector(".folder-link").textContent.toLowerCase().includes(query);
    if (!row.hidden) visible++;
  });
  const empty = document.getElementById("incoming-no-results");
  empty.textContent = query ? "No matching folders" : "No subfolders";
  empty.hidden = visible !== 0;
}

async function submitConversionFolders(folders, operation = "ome", retryJobId = null) {
  const payload = {folders};
  if (operation !== "ome") payload.operation = operation;
  if (retryJobId) payload.retry_job_id = retryJobId;
  const response = await fetch("/api/processing", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  return readConversionResponse(response);
}

document.getElementById("generate-ome").addEventListener("click", async () => {
  if (submittingConversion || !processingAllowed || selectedFolders.size === 0) return;
  submittingConversion = true;
  updateConversionSelection();
  try {
    await submitConversionFolders([...selectedFolders.keys()], document.getElementById("processing-operation").value);
    selectedFolders.clear();
    showProcessingTab();
  } catch (error) {
    const message = document.getElementById("incoming-message");
    message.textContent = error.message;
    message.hidden = false;
  } finally {
    submittingConversion = false;
    updateConversionSelection();
  }
});

document.getElementById("clear-selection").addEventListener("click", () => {
  selectedFolders.clear();
  updateConversionSelection();
});

const conversionStages = {
  queued: "Queued", checking_inputs: "Checking files",
  waiting_for_upload: "Waiting for upload to finish", loading: "Loading image",
  writing: "Writing OME-TIFF", validating: "Validating output",
  completed: "Completed", failed: "Failed",
  starting_gpu: "Starting inference worker", loading_model: "Loading CycleGAN model",
  inferencing: "Applying CycleGAN",
};

function renderConversionJobs(jobs) {
  const list = document.getElementById("processing-jobs");
  list.replaceChildren();
  document.querySelector(".processing-count").textContent =
    jobs.filter(job => ["queued", "running"].includes(job.status)).length;
  document.getElementById("processing-empty").hidden = jobs.length !== 0;
  for (const job of jobs) {
    const card = document.createElement("article");
    card.className = "conversion-job " + (["queued", "running", "completed", "failed"].includes(job.status) ? job.status : "");
    const name = document.createElement("h3");
    name.textContent = job.folder;
    const stage = document.createElement("p");
    stage.className = "job-stage";
    stage.textContent = conversionStages[job.stage] || job.stage;
    const times = document.createElement("p");
    times.className = "job-time";
    const start = Date.parse(job.started_at || job.created_at);
    const end = job.finished_at ? Date.parse(job.finished_at) : Date.now();
    const seconds = Math.max(0, Math.floor((end - start) / 1000));
    times.textContent = (job.status === "queued" ? "Waiting: " : "Elapsed: ") +
      Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
    card.append(name, stage, times);
    const operation = document.createElement("p");
    operation.className = "job-time";
    operation.textContent = job.operation === "cyclegan" ? "CycleGAN virtual H&E · " + job.inference.model_name : "OME-TIFF export";
    card.appendChild(operation);
    if (job.progress?.total_blocks) {
      const progress = document.createElement("p");
      progress.className = "job-time";
      progress.textContent = job.progress.completed_blocks + " / " + job.progress.total_blocks + " blocks processed";
      card.appendChild(progress);
    }
    if (job.error) {
      const error = document.createElement("p");
      error.className = "job-error";
      error.textContent = job.error;
      card.appendChild(error);
    }
    if (job.status === "completed" && job.output_path) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "conversion-secondary";
      open.textContent = "Open result";
      open.addEventListener("click", () => {
        const library = document.getElementById("catalog-tab");
        if (!library.hidden) library.click();
        else document.getElementById("files-tab").click();
        window.currentCatalogAsset = null;
        openImage(job.output_path);
      });
      card.appendChild(open);
    }
    if (job.status === "failed") {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "conversion-secondary";
      const waitingForGpu = job.operation === "cyclegan" && job.gpu_dispatch_started_at && !job.gpu_execution_finished;
      retry.disabled = Boolean(waitingForGpu);
      retry.textContent = waitingForGpu ? "Waiting for worker shutdown" : "Retry processing";
      retry.addEventListener("click", async () => {
        retry.disabled = true;
        try {
          await submitConversionFolders([job.folder], job.operation || "ome", job.operation === "cyclegan" ? job.id : null);
          await loadProcessing();
        } catch (error) {
          const message = document.getElementById("processing-message");
          message.textContent = error.message;
          message.hidden = false;
          retry.disabled = false;
        }
      });
      card.appendChild(retry);
    }
    list.appendChild(card);
  }
}

async function loadProcessing() {
  if (!processingAllowed || processingRequestActive) return;
  processingRequestActive = true;
  const message = document.getElementById("processing-message");
  try {
    const response = await fetch("/api/processing");
    const data = await readConversionResponse(response);
    message.hidden = true;
    setInferenceCapability(data.inference);
    renderConversionJobs(data.jobs);
  } catch (error) {
    message.textContent = error.message;
    message.hidden = false;
  } finally {
    processingRequestActive = false;
  }
}

document.getElementById("refresh-processing").addEventListener("click", loadProcessing);
document.getElementById("processing-operation").addEventListener("change", updateConversionSelection);
