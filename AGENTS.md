# AGENTS.md

## Project Context

This project contains a Python script for finding cadastral parcels in Croatia by surface area, using the official Croatian cadastral DKP ATOM download feed.

The current target use case is:

* Cadastral municipality: **k.o. Sesvetski Kraljevec**
* MBKO: **325392**
* Area range: **700–800 m²**
* Output:

  * CSV with parcel area, Google Maps link, and property description
  * GPKG for GIS inspection in QGIS

The project intentionally avoids the WFS `bbox` approach because it is fragile, rate/size-limited, and returned HTTP 400 errors during testing. The preferred approach is downloading the DKP ZIP for a full cadastral municipality through the ATOM feed and processing the included GML/XML layers locally.

## Primary Script

Expected main script name:

```text
katastar_pretraga_po_kvadraturi_maps_description.py
```

The script should:

1. Find the DKP ZIP for the configured cadastral municipality using the ATOM feed.
2. Download and extract the ZIP.
3. Read all vector layers from GML/XML files.
4. Auto-detect the cadastral parcel layer.
5. Filter parcels by area.
6. Add a Google Maps link using a representative point inside each parcel polygon.
7. Add `property_description` by spatially joining likely land-use/building/object layers.
8. Export:

   * CSV for spreadsheet use
   * GPKG for QGIS inspection

## Important User Requirements

The CSV must include:

* Parcel ID / parcel number column if detected
* `area_used_m2`
* `area_official_m2`
* `area_geometry_m2`
* `google_maps_url`
* `property_description`

The CSV must not include:

* `geometry_wkt`
* `centroid_y_3765`
* `map_point_lat`
* `map_point_lon`
* `centroid_lat`
* `centroid_lon`
* `centroid_x_3765`

The GPKG may keep geometry and helper columns because it is intended for GIS inspection.

## Data Source Strategy

Use ATOM/DKP ZIP downloads by cadastral municipality.

Do not default back to WFS unless explicitly requested.

Reason:

| Approach              | Status                     |
| --------------------- | -------------------------- |
| ATOM ZIP by k.o.      | Preferred                  |
| WFS `bbox`            | Avoid by default           |
| Manual OSS map search | Only for verification      |
| QGIS                  | Good for visual inspection |

## Coordinate Handling

Use EPSG:3765 internally:

```text
HTRS96 / Croatia TM
```

For Google Maps URLs:

1. Generate a representative point from the parcel geometry.
2. Transform it to EPSG:4326.
3. Build the URL:

```text
https://www.google.com/maps?q={lat},{lon}
```

Use `representative_point()` rather than centroid because it is guaranteed to be inside the parcel polygon. Centroids can fall outside irregular parcel geometries.

## Property Description Logic

The `property_description` column should be created by scanning non-parcel layers for likely description, land-use, building, object, or usage columns.

Relevant concepts include:

* house
* building
* garage
* yard
* field
* garden
* orchard
* meadow
* pasture
* road/path
* land use / način uporabe
* object / zgrada

The exact GML schema can vary, so matching should be broad and defensive.

The script should spatially join filtered parcels with description-like layers using `intersects`.

For polygon layers, calculate intersection area when possible and include it in the description item as:

```text
intersect_area_m2=...
```

The description output may be noisy. Do not present it as legally authoritative.

## Legal / Accuracy Notes

Do not imply that the output is legally authoritative.

Always treat the generated CSV/GPKG as a scouting/filtering tool.

For any purchase, development, ownership, zoning, or legal decision, the parcel must be manually verified in:

* OSS
* cadastral records
* land registry / zemljišna knjiga
* relevant local zoning/planning documents

## Dependencies

Expected Python dependencies:

```text
requests
pandas
geopandas
shapely
pyproj
lxml
pyogrio
```

Optional but sometimes useful:

```text
rtree
```

## Coding Style

Use clear, defensive Python.

Prefer:

* explicit config variables at the top
* pure helper functions
* readable print progress
* safe ZIP extraction
* robust XML parsing
* broad but inspectable layer scoring
* CSV output optimized for humans
* GPKG output optimized for GIS

Avoid:

* hardcoding temporary paths
* relying on one exact GML schema
* silently swallowing critical errors
* returning massive unreadable CSV columns unless requested
* adding WFS logic unless specifically requested

## Expected Output Paths

```text
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.csv
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.gpkg
```

## Troubleshooting Guidance

If ZIP discovery fails:

1. Check `TARGET_MBKO`.
2. Check `TARGET_KO_NAME`.
3. Inspect debug XML files in:

```text
katastar_output/_debug/
```

If no parcel layer is detected:

1. Print all readable vector layers.
2. Inspect layer names, row counts, geometry types, and columns.
3. Adjust `score_parcel_layer()`.

If `property_description` is empty:

1. Inspect printed candidate description layers.
2. Adjust `description_columns()`.
3. Adjust `looks_like_description_layer()`.
4. Open the GPKG in QGIS and inspect available layers manually.

If area results look wrong:

1. Compare `area_official_m2` and `area_geometry_m2`.
2. Try:

```python
AREA_SOURCE = "geometry"
```

or:

```python
AREA_SOURCE = "official"
```

3. Verify against OSS manually.

## Development Principle

This project is for fast parcel scouting, not legal truth.

Optimize for:

1. Repeatable filtering
2. Inspectable output
3. Easy CSV usage
4. QGIS verification
5. Minimal manual clicking in OSS
