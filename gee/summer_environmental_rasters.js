/**
 * =====================================================================
 *  Summer surface-environment rasters for Maryland + Washington, DC
 * =====================================================================
 *
 *  Produces a co-registered stack of daytime summer environmental
 *  variables for the "green gap" affordable-housing study:
 *
 *      1. LST   - Land Surface Temperature (deg C, median)
 *      2. NDVI  - Normalized Difference Vegetation Index
 *      3. NDBI  - Normalized Difference Built-up Index
 *      4. NDWI  - Normalized Difference Water Index (for masking / QA)
 *      5. ALBEDO- Broadband shortwave albedo (Liang 2001)
 *
 *  Source:  Landsat 8 & 9 Collection 2 Level-2 (surface reflectance +
 *           surface temperature), USGS.
 *
 *  Design choices for a publication-quality workflow
 *  -------------------------------------------------
 *   * 3-year summer CLIMATOLOGY (not a single year) to damp the effect
 *     of anomalous weather in any one summer. June-August only.
 *   * Per-pixel QA_PIXEL + QA_RADSAT masking (cloud, shadow, cirrus,
 *     dilated cloud, saturated pixels).
 *   * Scene-level cloud filter, then pixel-level mask -> maximises the
 *     number of clear observations contributing to each median.
 *   * All indices computed per-scene BEFORE compositing, so the median
 *     is taken over indices (correct) rather than over a composite of
 *     bands (biased).
 *   * A single, shared study geometry, projection and scale for every
 *     export so the outputs are pixel-aligned and stackable.
 *   * Metadata logged to the console and attached to each image as
 *     properties, and a machine-readable manifest exported to Drive.
 *
 *  Outputs (GeoTIFF, 30 m, EPSG:26918) are intended to land in
 *      data/external/gee/
 *  of the green-gap-affordable-housing repository (Landsat is a
 *  third-party product -> "external"; see README project organization).
 *
 *  Run:  paste into https://code.earthengine.google.com, press Run,
 *        then start the export tasks from the "Tasks" tab.
 *
 *  Author: green-gap-affordable-housing
 * =====================================================================
 */

/* ============================ CONFIG ============================== */

var CONFIG = {

  // ---- Study period ----------------------------------------------
  // Housing snapshot is ~2022; use a +/-1 year window and keep only
  // June-August observations across all three summers.
  startYear: 2021,
  endYear:   2023,
  summerStartMonth: 6,   // June
  summerEndMonth:   8,   // August

  // ---- Scene-level quality gate ----------------------------------
  maxSceneCloudCover: 60,   // percent; pixel mask does the fine work

  // ---- Export grid (shared by every layer) -----------------------
  scale: 30,                // metres
  crs:   'EPSG:26918',      // UTM 18N - covers MD + DC
  maxPixels: 1e13,
  driveFolder: 'GEE_green_gap',

  // ---- What to export --------------------------------------------
  exportStack:      true,   // one multi-band GeoTIFF (analysis-ready)
  exportPerLayer:   true,   // one GeoTIFF per variable (inspection)
  exportManifest:   true,   // JSON sidecar with provenance
  exportClearCount: true,   // n clear observations per pixel (QA)

  // ---- Visualisation only (does not affect exports) --------------
  addMapLayers: true
};

// Convenience: inclusive date bounds for filterDate (end is exclusive).
CONFIG.startDate = CONFIG.startYear + '-01-01';
CONFIG.endDate   = (CONFIG.endYear + 1) + '-01-01';

// A stable tag used in file names and properties.
CONFIG.tag = 'MD_DC_summer_' + CONFIG.startYear + '_' + CONFIG.endYear;


/* ========================= STUDY AREA ============================ */

var states = ee.FeatureCollection('TIGER/2018/States');
var md = states.filter(ee.Filter.eq('NAME', 'Maryland'));
var dc = states.filter(ee.Filter.eq('NAME', 'District of Columbia'));

// Dissolve to a single multipolygon; keep both a Feature (for export
// region) and its geometry (for clipping / filtering).
var studyArea   = md.merge(dc).union().first().geometry();
var exportRegion = studyArea;   // alias for readability at call sites


/* =================== SCALING / BAND HELPERS ====================== */

// Collection 2 Level-2 scale factors (USGS).
var SR_MULT = 0.0000275,  SR_ADD = -0.2;     // surface reflectance
var ST_MULT = 0.00341802, ST_ADD = 149.0;    // surface temperature (K)

/**
 * Apply the official C2 L2 scale/offset to SR and ST bands and rename
 * the reflectance bands to common names so L8 and L9 (identical here,
 * but future-proof) can be treated uniformly.
 */
function scaleAndRename(image) {

  var sr = image.select(['SR_B2', 'SR_B3', 'SR_B4', 'SR_B5', 'SR_B6', 'SR_B7'])
                .multiply(SR_MULT).add(SR_ADD)
                .rename(['blue', 'green', 'red', 'nir', 'swir1', 'swir2']);

  var st = image.select('ST_B10')
                .multiply(ST_MULT).add(ST_ADD)   // -> Kelvin
                .subtract(273.15)                 // -> Celsius
                .rename('LST');

  // Physically plausible reflectance in [0, 1]; clamp mild negatives
  // that scaling can introduce over dark/wet surfaces.
  sr = sr.clamp(0, 1);

  return image.addBands(sr, null, true)
              .addBands(st, null, true)
              .copyProperties(image, image.propertyNames());
}


/* ========================= CLOUD MASK ============================ */

/**
 * QA_PIXEL / QA_RADSAT based mask for Collection 2 Level-2.
 * Removes: dilated cloud, cirrus, cloud, cloud shadow, and any
 * radiometrically saturated pixel.
 */
function maskClouds(image) {

  var qa = image.select('QA_PIXEL');

  var dilated = 1 << 1;
  var cirrus  = 1 << 2;
  var cloud   = 1 << 3;
  var shadow  = 1 << 4;

  var qaMask = qa.bitwiseAnd(dilated).eq(0)
    .and(qa.bitwiseAnd(cirrus).eq(0))
    .and(qa.bitwiseAnd(cloud).eq(0))
    .and(qa.bitwiseAnd(shadow).eq(0));

  // Any saturated band -> drop the pixel.
  var satMask = image.select('QA_RADSAT').eq(0);

  return image.updateMask(qaMask).updateMask(satMask);
}


/* ======================= SPECTRAL INDICES ======================== */

/**
 * Compute all per-scene indices. Must run AFTER scaleAndRename so band
 * names and units are correct, and AFTER maskClouds so cloudy pixels do
 * not contaminate the indices.
 */
function addIndices(image) {

  // Vegetation.
  var ndvi = image.normalizedDifference(['nir', 'red']).rename('NDVI');

  // Built-up (SWIR1 vs NIR): high over impervious surfaces.
  var ndbi = image.normalizedDifference(['swir1', 'nir']).rename('NDBI');

  // Water (green vs NIR): used to flag open water for optional masking.
  var ndwi = image.normalizedDifference(['green', 'nir']).rename('NDWI');

  // Broadband shortwave albedo, Liang (2001) narrow-to-broadband
  // coefficients for the Landsat OLI-equivalent bands.
  var albedo = image.expression(
    '0.356*blue + 0.130*red + 0.373*nir + 0.085*swir1 + 0.072*swir2 - 0.0018',
    {
      blue:  image.select('blue'),
      red:   image.select('red'),
      nir:   image.select('nir'),
      swir1: image.select('swir1'),
      swir2: image.select('swir2')
    }
  ).rename('ALBEDO');

  return image.addBands([ndvi, ndbi, ndwi, albedo]);
}


/* ==================== TEMPORAL FILTERING ========================= */

/**
 * Keep only June-August observations. filter(calendarRange) on 'month'
 * is applied after the coarse date bound so we span multiple summers.
 */
function summerFilter(collection) {
  return collection
    .filterDate(CONFIG.startDate, CONFIG.endDate)
    .filter(ee.Filter.calendarRange(
      CONFIG.summerStartMonth, CONFIG.summerEndMonth, 'month'))
    .filter(ee.Filter.lt('CLOUD_COVER', CONFIG.maxSceneCloudCover))
    .filterBounds(studyArea);
}


/* ==================== BUILD THE COLLECTION ======================= */

var l8 = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2');
var l9 = ee.ImageCollection('LANDSAT/LC09/C02/T1_L2');

var merged = summerFilter(l8.merge(l9))
  .map(maskClouds)     // 1. drop cloud/shadow/saturated pixels
  .map(scaleAndRename) // 2. physical units + common band names
  .map(addIndices);    // 3. per-scene indices

// Bands carried into the composite (drops raw SR_* / ST_* / QA_*).
var ANALYSIS_BANDS = ['LST', 'NDVI', 'NDBI', 'NDWI', 'ALBEDO'];

var analysis = merged.select(ANALYSIS_BANDS);


/* ======================= COMPOSITE =============================== */

// Median over the 3-summer stack of per-scene indices/temperature.
var composite = analysis.median()
  .clip(studyArea)
  .set({
    'system:time_start': ee.Date(CONFIG.startDate).millis(),
    study_tag:        CONFIG.tag,
    source:           'Landsat 8/9 C02 L2 (LC08_L2, LC09_L2)',
    period:           CONFIG.startYear + '-' + CONFIG.endYear + ' (Jun-Aug)',
    reducer:          'median',
    scene_cloud_max:  CONFIG.maxSceneCloudCover,
    scale_m:          CONFIG.scale,
    crs:              CONFIG.crs,
    albedo_method:    'Liang (2001) narrow-to-broadband'
  });

// Per-pixel count of clear observations feeding the median (QA layer):
// low counts => less reliable pixels.
var clearCount = analysis.select('LST').count()
  .rename('CLEAR_OBS').clip(studyArea).toInt16();


/* ==================== METADATA / LOGGING ========================= */

var sceneCount = merged.size();

print('================= Summer environmental rasters =================');
print('Study tag:', CONFIG.tag);
print('Period:', CONFIG.startYear + '-' + CONFIG.endYear + '  (Jun-Aug)');
print('Scene-level cloud filter (<):', CONFIG.maxSceneCloudCover, '%');
print('Contributing Landsat scenes:', sceneCount);
print('Analysis bands:', ANALYSIS_BANDS);
print('Export grid:', CONFIG.scale + ' m,', CONFIG.crs);
print('Composite (band names + properties):', composite);

// A compact, machine-readable manifest for the repo (provenance).
var manifest = ee.Feature(null, {
  study_tag:        CONFIG.tag,
  source:           'Landsat 8/9 Collection 2 Level-2',
  collections:      'LANDSAT/LC08/C02/T1_L2 ; LANDSAT/LC09/C02/T1_L2',
  period:           CONFIG.startYear + '-' + CONFIG.endYear,
  months:           'Jun-Aug',
  reducer:          'median (per-scene indices)',
  scene_cloud_max:  CONFIG.maxSceneCloudCover,
  n_scenes:         sceneCount,
  bands:            ANALYSIS_BANDS.join(','),
  scale_m:          CONFIG.scale,
  crs:              CONFIG.crs,
  albedo_method:    'Liang 2001 narrow-to-broadband',
  generated_utc:    ee.Date(Date.now()).format('YYYY-MM-dd HH:mm:ss')
});


/* ====================== VISUALISATION =========================== */

if (CONFIG.addMapLayers) {

  Map.centerObject(studyArea, 8);
  Map.addLayer(studyArea, {color: '000000'}, 'Study area', false);

  var lstVis = {min: 20, max: 45, palette: [
    '040274', '2c7bb6', 'abd9e9', 'ffffbf', 'fdae61', 'd7191c']};
  var ndviVis = {min: -0.1, max: 0.9, palette: [
    'ffffff', 'ce7e45', 'fcd163', 'c6ca02', '22cc04', '011301']};
  var ndbiVis = {min: -0.5, max: 0.5, palette: [
    '2c7bb6', 'ffffbf', 'd7191c']};
  var albedoVis = {min: 0.0, max: 0.4, palette: [
    '000000', '444444', '888888', 'cccccc', 'ffffff']};

  Map.addLayer(composite.select('LST'),    lstVis,    'LST (deg C)');
  Map.addLayer(composite.select('NDVI'),   ndviVis,   'NDVI', false);
  Map.addLayer(composite.select('NDBI'),   ndbiVis,   'NDBI', false);
  Map.addLayer(composite.select('ALBEDO'), albedoVis, 'Albedo', false);
  Map.addLayer(clearCount, {min: 0, max: 40, palette: [
    'd7191c', 'fdae61', 'ffffbf', 'a6d96a', '1a9641']},
    'Clear obs count', false);
}


/* ========================== EXPORTS ============================== */

// Shared export arguments so every file is on the identical grid.
function exportImage(image, name) {
  Export.image.toDrive({
    image:         image,
    description:   name,
    folder:        CONFIG.driveFolder,
    fileNamePrefix: name,
    region:        exportRegion,
    scale:         CONFIG.scale,
    crs:           CONFIG.crs,
    maxPixels:     CONFIG.maxPixels,
    formatOptions: {cloudOptimized: true}
  });
}

// 1. Analysis-ready multi-band stack (one file, all variables).
if (CONFIG.exportStack) {
  exportImage(composite.toFloat(), CONFIG.tag + '_stack');
}

// 2. One GeoTIFF per variable (easier visual inspection / QA).
if (CONFIG.exportPerLayer) {
  ANALYSIS_BANDS.forEach(function (band) {
    exportImage(composite.select(band).toFloat(),
                CONFIG.tag + '_' + band);
  });
}

// 3. Per-pixel clear-observation count (reliability layer).
if (CONFIG.exportClearCount) {
  exportImage(clearCount, CONFIG.tag + '_CLEAR_OBS');
}

// 4. Provenance manifest (JSON sidecar for the repo).
if (CONFIG.exportManifest) {
  Export.table.toDrive({
    collection:  ee.FeatureCollection([manifest]),
    description: CONFIG.tag + '_manifest',
    folder:      CONFIG.driveFolder,
    fileNamePrefix: CONFIG.tag + '_manifest',
    fileFormat:  'GeoJSON'
  });
}

print('Export tasks created. Open the "Tasks" tab and click Run on each.');
print('Suggested repo destination: data/external/gee/');
