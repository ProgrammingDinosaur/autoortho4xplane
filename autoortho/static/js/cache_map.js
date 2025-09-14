// Leaflet map for Cache Manager (cache-only logic)
(function () {
    const map = L.map('map', {minZoom: 5, maxZoom: 18}).setView([51.505, -0.09], 5);
    const BASE_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    const BASE_TILE_ATTR = '&copy; OpenStreetMap contributors';
    let baseLayer = L.tileLayer(BASE_TILE_URL, { attribution: BASE_TILE_ATTR }).addTo(map);

    const gridLinesLayer = L.layerGroup().addTo(map);
    const gridCellsLayer = L.layerGroup().addTo(map);
    const gridLabelsLayer = L.layerGroup().addTo(map);
    const subgridLayer = L.layerGroup().addTo(map);
    const selectedCells = new Set();
    const GRID_MIN_ZOOM_FOR_CELLS = 5;
    const tileEffectiveMaptypeCache = new Map();
    const cacheStatusByKey = new Map(); // key: "lat,lon" -> { total, cached }
    const cacheStatusInflight = new Set();
    let flushTimer = null;

    let availableTilesParsed = [];
    const availableTilesMap = new Map();

    if (typeof document !== 'undefined' && document.head) {
        const styleEl = document.createElement('style');
        styleEl.textContent = '.grid-label{color:#444;font:12px/1.2 Arial,Helvetica,sans-serif;font-weight:bold;pointer-events:none;display:flex;align-items:center;justify-content:center;width:100%;height:100%;padding:0 2px;}';
        document.head.appendChild(styleEl);
        const styleEl2 = document.createElement('style');
        styleEl2.textContent = '.cache-refresh{background:rgba(255,255,255,0.95);padding:4px;border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,0.2)}.cache-refresh button{border:1px solid #d0d7de;border-radius:4px;background:#f6f8fa;color:#24292f;cursor:pointer;padding:4px 8px;font:12px Arial,Helvetica,sans-serif}.cache-refresh button:hover{background:#eaeef2}';
        document.head.appendChild(styleEl2);
    }

    // Disable interactions not needed
    map.dragging.disable();
    if (map.boxZoom && map.boxZoom.disable) map.boxZoom.disable();
    if (map.doubleClickZoom && map.doubleClickZoom.disable) map.doubleClickZoom.disable();
    if (map.getContainer()) map.getContainer().addEventListener('contextmenu', function (ev) { ev.preventDefault(); });

    // Right-click + hold panning
    let mouseButtonDown = null;
    let rightPanning = false;
    let lastPanPoint = null;
    map.on('mousedown', function (e) {
        mouseButtonDown = e.originalEvent && typeof e.originalEvent.button === 'number' ? e.originalEvent.button : null;
        if (mouseButtonDown === 2) {
            rightPanning = true;
            lastPanPoint = e.containerPoint;
            if (e.originalEvent && e.originalEvent.preventDefault) e.originalEvent.preventDefault();
        }
    });
    map.on('mousemove', function (e) {
        if (rightPanning && lastPanPoint) {
            const dx = lastPanPoint.x - e.containerPoint.x;
            const dy = lastPanPoint.y - e.containerPoint.y;
            if (dx !== 0 || dy !== 0) {
                map.panBy([dx, dy], { animate: false });
                lastPanPoint = e.containerPoint;
            }
        }
    });
    map.on('mouseup', function (e) {
        if (rightPanning && e.originalEvent && e.originalEvent.button === 2) {
            rightPanning = false;
            lastPanPoint = null;
        }
        mouseButtonDown = null;
    });

    // Helpers
    function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
    function normalizeLon(lon) { let x = lon; while (x > 180) x -= 360; while (x < -180) x += 360; return x; }
    function cellKey(latDeg, lonDeg) { return latDeg + ',' + lonDeg; }
    function parseCoordTile(coord) { if (typeof coord !== 'string') return null; const m = coord.match(/^([+-]\d{2})([+-]\d{3})$/); if (!m) return null; const lat = parseInt(m[1], 10); const lon = parseInt(m[2], 10); if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null; return [lat, lon]; }

    function refreshCacheStatus() {
        try { cacheStatusByKey.clear(); } catch (_e) {}
        try { cacheStatusInflight.clear(); } catch (_e2) {}
        // Rebuild to show 'loading' and refetch visible tiles
        rebuildGrid();
    }

    // Fetchers
    async function fetchTileEffectiveMaptype(latDeg, lonDeg) {
        const key = cellKey(latDeg, lonDeg);
        if (tileEffectiveMaptypeCache.has(key)) return tileEffectiveMaptypeCache.get(key);
        try {
            const resp = await fetch(`/tile_maptype?lat=${latDeg}&lon=${lonDeg}&resolve_dflt=true`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const eff = (data && (data.effective_maptype || data.maptype)) ? (data.effective_maptype || data.maptype) : 'DFLT';
            tileEffectiveMaptypeCache.set(key, eff); return eff;
        } catch (_e) { tileEffectiveMaptypeCache.set(key, 'DFLT'); return 'DFLT'; }
    }
    async function fetchTileCacheStatus(latDeg, lonDeg) {
        let mt = 'DFLT';
        try { mt = await fetchTileEffectiveMaptype(latDeg, lonDeg); } catch (_e) {}
        try {
            const resp = await fetch(`/cache_status?lat=${latDeg}&lon=${lonDeg}&maptype=${encodeURIComponent(mt)}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const total = (data && typeof data.total === 'number') ? data.total : -1;
            const cached = (data && typeof data.cached === 'number') ? data.cached : -1;
            return { total: total, cached: cached };
        } catch (_e2) { return { total: -1, cached: -1 }; }
    }

    function styleGray(isMain) { return { color: '#9e9e9e', fillColor: '#bdbdbd', fillOpacity: 0.2, weight: isMain ? 5 : 1, opacity: 0.6 }; }
    function styleYellow(isMain) { return { color: '#f9a825', fillColor: '#f9a825', fillOpacity: 0.35, weight: isMain ? 5 : 2, opacity: 0.95 }; }
    function styleGreen(isMain) { return { color: '#2e7d32', fillColor: '#2e7d32', fillOpacity: 0.35, weight: isMain ? 5 : 2, opacity: 0.95 }; }

    function rebuildGrid() {
        gridLinesLayer.clearLayers(); gridCellsLayer.clearLayers(); gridLabelsLayer.clearLayers();
        subgridLayer.clearLayers();
        const b = map.getBounds(); let south = Math.floor(clamp(b.getSouth(), -90, 90)); let north = Math.ceil(clamp(b.getNorth(), -90, 90)); let west = Math.floor(clamp(b.getWest(), -180, 180)); let east = Math.ceil(clamp(b.getEast(), -180, 180));
        let eastForLines = east; if (east < west) eastForLines = east + 360;
        for (let lat = south; lat <= north; lat++) { const p1 = [lat, normalizeLon(west)]; const p2 = [lat, normalizeLon(eastForLines)]; L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer); }
        for (let lon = west; lon <= eastForLines; lon++) { const p1 = [clamp(south, -90, 90), normalizeLon(lon)]; const p2 = [clamp(north, -90, 90), normalizeLon(lon)]; L.polyline([p1, p2], { color: '#888', weight: 1, opacity: 0.5, interactive: false }).addTo(gridLinesLayer); }

        if (map.getZoom() >= GRID_MIN_ZOOM_FOR_CELLS && availableTilesParsed.length) {
            const northCells = Math.min(north, 89); const eastCells = eastForLines - 1;
            for (let i = 0; i < availableTilesParsed.length; i++) {
                const lat = availableTilesParsed[i].lat; const lon = availableTilesParsed[i].lon; if (lat < south || lat > northCells) continue;
                let lonCandidate = lon; if (east < west && lon < west) lonCandidate = lon + 360; if (lonCandidate < west || lonCandidate > eastCells) continue;
                const ll = [lat, normalizeLon(lon)]; const ur = [lat + 1, normalizeLon(lon + 1)]; const key = availableTilesParsed[i].key || cellKey(lat, normalizeLon(lon));
                const rect = L.rectangle([ll, ur], styleGray(false));
                rect.on('click', function () {
                    if (rect._clickTimeout) clearTimeout(rect._clickTimeout);
                    rect._clickTimeout = setTimeout(function () {
                        const wasSelected = selectedCells.has(key); if (wasSelected) selectedCells.delete(key); else selectedCells.add(key); rect._clickTimeout = null;
                    }, 220);
                });
                rect.addTo(gridCellsLayer);

                const centerLat = lat + 0.5; const centerLon = normalizeLon(lon + 0.5);
                const pLL = map.latLngToLayerPoint(L.latLng(lat, normalizeLon(lon))); const pUR = map.latLngToLayerPoint(L.latLng(lat + 1, normalizeLon(lon + 1)));
                const wPx = Math.max(1, Math.abs(pUR.x - pLL.x)); const hPx = Math.max(1, Math.abs(pUR.y - pLL.y));
                const labelMarker = L.marker([centerLat, centerLon], { icon: L.divIcon({ className: 'grid-label', html: 'loading', iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }), interactive: false }).addTo(gridLabelsLayer);
                // Use cached status if available; otherwise fetch once
                const applyStatus = function(total, cached) {
                    labelMarker.setIcon(L.divIcon({ className: 'grid-label', html: '', iconSize: [wPx, hPx], iconAnchor: [wPx / 2, hPx / 2] }));
                    const allCached = total > 0 && cached === total; const anyNotCached = total >= 0 && cached < total && cached >= 0; const noneCachedOrNone = total === -1 || cached === -1;
                    if (allCached) rect.setStyle(styleGreen(false)); else if (anyNotCached) rect.setStyle(styleYellow(false)); else if (noneCachedOrNone) rect.setStyle(styleGray(false));
                    // Draw subgrid if we have any presence in DB
                    if (!noneCachedOrNone && map.getZoom() >= 9) {
                        drawSubgrid(lat, normalizeLon(lon), 16, 16, allCached ? 'green' : (anyNotCached ? 'yellow' : 'red'));
                    }
                };
                const cachedEntry = cacheStatusByKey.get(key);
                if (cachedEntry) {
                    applyStatus(cachedEntry.total || 0, cachedEntry.cached || 0);
                } else if (!cacheStatusInflight.has(key)) {
                    cacheStatusInflight.add(key);
                    fetchTileCacheStatus(lat, normalizeLon(lon)).then(function (agg) {
                        const total = agg && typeof agg.total === 'number' ? agg.total : 0; const cached = agg && typeof agg.cached === 'number' ? agg.cached : 0;
                        cacheStatusByKey.set(key, { total: total, cached: cached });
                        cacheStatusInflight.delete(key);
                        applyStatus(total, cached);
                    }).catch(function () { cacheStatusInflight.delete(key); });
                }
            }
        }
    }

    function drawSubgrid(baseLat, baseLon, rows, cols, status) {
        const color = status === 'green' ? '#2e7d32' : (status === 'yellow' ? '#f9a825' : '#d32f2f');
        // Each lat/lon tile is 1x1 degree, so build a simple rows x cols grid inside it
        const dLat = 1.0 / rows;
        const dLon = 1.0 / cols;
        for (let r = 0; r < rows; r++) {
            for (let c = 0; c < cols; c++) {
                const cellLL = [baseLat + r * dLat, normalizeLon(baseLon + c * dLon)];
                const cellUR = [baseLat + (r + 1) * dLat, normalizeLon(baseLon + (c + 1) * dLon)];
                L.rectangle([cellLL, cellUR], { color: color, weight: 1, opacity: 0.8, fillOpacity: 0.25, fillColor: color, interactive: false }).addTo(subgridLayer);
            }
        }
    }

    // Debounce
    let rebuildTimer = null; function scheduleRebuild() { if (rebuildTimer) clearTimeout(rebuildTimer); rebuildTimer = setTimeout(rebuildGrid, 100); }
    map.on('moveend', scheduleRebuild); map.on('zoomend', scheduleRebuild);

    // Load tiles
    (function () { fetch('/available_tiles').then(function (resp) { if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.json(); }).then(function (data) {
        const tilesObj = (data && data.tiles && typeof data.tiles === 'object') ? data.tiles : {}; availableTilesParsed = []; availableTilesMap.clear();
        try { for (const coord in tilesObj) { if (!Object.prototype.hasOwnProperty.call(tilesObj, coord)) continue; const parsed = parseCoordTile(coord); if (!parsed) continue; const lat = parsed[0]; const lonRaw = parsed[1]; const lon = normalizeLon(lonRaw); const k = cellKey(lat, lon); const info = tilesObj[coord] || {}; availableTilesMap.set(k, info); availableTilesParsed.push({ lat: lat, lon: lon, key: k, info: info }); } } catch (_e) {}
        scheduleRebuild(); }).catch(function () {}); })();

    // Refresh control
    const RefreshControl = L.Control.extend({
        onAdd: function () {
            const container = L.DomUtil.create('div', 'cache-refresh');
            const btn = L.DomUtil.create('button', '', container);
            btn.type = 'button';
            btn.textContent = 'Refresh';
            L.DomEvent.disableClickPropagation(container);
            btn.addEventListener('click', function (ev) { ev.preventDefault(); refreshCacheStatus(); });
            return container;
        }
    });
    (new RefreshControl({ position: 'topright' })).addTo(map);

    // Memory flush API
    function flushMapMemory() { try { gridLinesLayer.clearLayers(); gridCellsLayer.clearLayers(); gridLabelsLayer.clearLayers(); } catch (_e) {} try { if (baseLayer && baseLayer._tiles) { Object.keys(baseLayer._tiles).forEach(function (k) { delete baseLayer._tiles[k]; }); } } catch (_e2) {} }
    function scheduleFlushIn(ms) { if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; } flushTimer = setTimeout(function () { flushMapMemory(); }, Math.max(0, ms || 60000)); }
    function cancelFlush() { if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; } }
    if (typeof window !== 'undefined') { window.AOMap = window.AOMap || {}; window.AOMap.flushNow = flushMapMemory; window.AOMap.flushIn = scheduleFlushIn; window.AOMap.cancelFlush = cancelFlush; window.AOMap.reloadTiles = function () { location.reload(); }; }

    rebuildGrid();
})();


