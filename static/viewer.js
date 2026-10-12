const scanSelect = document.querySelector("#scan-select");
const statusText = document.querySelector("#status");

const viewer = OpenSeadragon({
  id: "viewer",
  prefixUrl: "https://cdn.jsdelivr.net/npm/openseadragon@5.0.1/build/openseadragon/images/",
  showNavigator: true,
  navigatorPosition: "BOTTOM_RIGHT",
  animationTime: 0.4,
  blendTime: 0.1,
  maxZoomPixelRatio: 2,
  visibilityRatio: 0.5,
  constrainDuringPan: true,
});

function setStatus(message, isError = false) {
  statusText.textContent = message;
  statusText.classList.toggle("error", isError);
}

async function requestJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (_) {
      // The server did not return JSON.
    }
    throw new Error(message);
  }
  return response.json();
}

async function openScan(scanId) {
  if (!scanId) return;
  setStatus("Reading image metadata…");

  try {
    const info = await requestJson(`/api/scans/${encodeURIComponent(scanId)}/info`);

    // OpenSeadragon uses Deep Zoom levels: maxLevel is full resolution and each
    // preceding level is half-sized. The backend maps each requested Deep Zoom
    // level to the closest suitable native TIFF page/SUBIFD.
    const tileSource = new OpenSeadragon.TileSource({
      width: info.width,
      height: info.height,
      tileSize: info.tile_size,
      tileOverlap: 0,
      minLevel: 0,
      maxLevel: info.osd_max_level,
    });
    tileSource.getTileUrl = (level, x, y) =>
      `/api/scans/${encodeURIComponent(scanId)}/tiles/${level}/${x}/${y}.jpg`;

    viewer.open(tileSource);
    setStatus(`${info.width.toLocaleString()} × ${info.height.toLocaleString()} · ${info.pyramid_levels} native level(s)`);
  } catch (error) {
    viewer.close();
    setStatus(error.message, true);
  }
}

async function loadScans() {
  try {
    const scans = await requestJson("/api/scans");
    scanSelect.replaceChildren();

    if (scans.length === 0) {
      scanSelect.add(new Option("No TIFF files found", ""));
      setStatus("Place a .tif or .tiff file in viewer/scans");
      return;
    }

    for (const scan of scans) {
      scanSelect.add(new Option(scan.name, scan.id));
    }
    scanSelect.disabled = false;
    await openScan(scanSelect.value);
  } catch (error) {
    scanSelect.replaceChildren(new Option("Could not load scans", ""));
    setStatus(error.message, true);
  }
}

scanSelect.addEventListener("change", () => openScan(scanSelect.value));
viewer.addHandler("open-failed", (event) => setStatus(event.message || "Image tiles could not be opened", true));
loadScans();
