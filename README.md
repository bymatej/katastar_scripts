# README.md

## Katastar Parcel Area Search

Python tool for finding Croatian cadastral parcels by surface area and exporting them with direct Google Maps links.

The current default configuration searches:

```text
k.o. Sesvetski Kraljevec
MBKO: 325392
Area: 700–800 m²
```

The script downloads official DKP cadastral data through the ATOM feed, reads the included GML/XML files, detects the parcel layer, filters parcels by area, adds a Google Maps link, and tries to describe what is on the property using land-use/building/object layers.

## Why This Exists

The public OSS cadastral map is useful when you already know a parcel number, but it does not provide a convenient search like:

```text
List all parcels in Sesvetski Kraljevec between 700 and 800 m².
```

This script does that locally.

## What It Produces

The script exports:

```text
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.csv
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.gpkg
```

### CSV

Use this for spreadsheet review.

Important columns:

| Column                 | Description                                                |
| ---------------------- | ---------------------------------------------------------- |
| Parcel ID column       | Auto-detected parcel number / cadastral reference if found |
| `area_used_m2`         | Area used for filtering                                    |
| `area_official_m2`     | Official-like area attribute if detected                   |
| `area_geometry_m2`     | Area calculated from parcel geometry                       |
| `google_maps_url`      | Direct Google Maps link to a point inside the parcel       |
| `property_description` | Joined land-use/building/object description data           |

The CSV intentionally excludes noisy coordinate and geometry helper columns.

### GPKG

Use this in QGIS for proper GIS inspection.

The GPKG keeps actual geometry and is better for checking parcel shape and nearby context.

#### Install qgis
```
sudo apt update && sudo apt install qgis
```
```
sudo apt update && sudo apt install fonts-open-sans
```
After opening the file in QGIS Desktop add a background map (OpenStreetMap):  
1. In the menu on the left side, find the **Browser** panel.
2. Scroll down and locate the **XYZ Tiles** item.
3. Click on the arrow next to it to expand it.
4. Double-click on **OpenStreetMap** (or drag and drop it onto the map).
5. **Important**: If OpenStreetMap covers your lines, click on OpenStreetMap in the bottom-left **Layers** panel and drag it with your mouse *below* your "sesvetski kraljevec" layer.


## Installation

Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

Upgrade pip:

```bash
python -m pip install --upgrade pip
```

Install dependencies:

```bash
python -m pip install requests pandas geopandas shapely pyproj lxml pyogrio
```

If spatial joins fail or your environment complains about spatial indexes, also install:

```bash
python -m pip install rtree
```

## Usage

Run:

```bash
python katastar_pretraga_po_kvadraturi_maps_description.py
```

After completion, open:

```text
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.csv
```

or load the GPKG into QGIS:

```text
katastar_output/sesvetski_kraljevec_700_800_google_maps_description.gpkg
```

## Main Configuration

Edit these variables at the top of the script:

```python
TARGET_KO_NAME = "SESVETSKI KRALJEVEC"
TARGET_MBKO = "325392"

AREA_MIN_M2 = 700
AREA_MAX_M2 = 800
```

### Area Source

```python
AREA_SOURCE = "auto"
```

Available options:

| Value        | Meaning                                                     |
| ------------ | ----------------------------------------------------------- |
| `"auto"`     | Use official-like area if detected, otherwise geometry area |
| `"official"` | Use only official-like area attribute                       |
| `"geometry"` | Use area calculated from parcel geometry                    |

Recommended default:

```python
AREA_SOURCE = "auto"
```

If results look suspicious, compare `area_official_m2` and `area_geometry_m2`.

## Google Maps Links

The script creates a Google Maps URL using a point inside the parcel polygon:

```text
https://www.google.com/maps?q={lat},{lon}
```

It uses `representative_point()`, not centroid.

Reason:

| Method                   | Pros                                 | Cons                               |
| ------------------------ | ------------------------------------ | ---------------------------------- |
| `representative_point()` | Guaranteed inside the parcel polygon | Not always visually centered       |
| Centroid                 | Geometric center                     | Can fall outside irregular parcels |

For scouting, `representative_point()` is the better default.

## Property Description

The script tries to build a `property_description` column by spatially joining filtered parcels with non-parcel layers that look like:

* land-use layers
* building layers
* object layers
* description layers
* “način uporabe” layers

The output may contain values such as:

```text
source=... | intersect_area_m2=... | NAZIV=...
```

or similar, depending on the actual GML column names.

Expected concepts may include:

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
* land use

The exact output depends on what is present in the DKP ZIP for the selected cadastral municipality.

## Limitations

This is a scouting tool, not a legal source of truth.

| Limitation                          | Explanation                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------- |
| Parcel address is not guaranteed    | Empty parcels often do not have addresses                                       |
| `property_description` can be noisy | GML schemas vary and spatial joins can include overlapping features             |
| Google Maps pin is approximate      | It points inside the parcel, not necessarily to an entrance or official address |
| Area source can differ              | Official-like area and geometry-calculated area may not match exactly           |
| Auto layer detection can fail       | GML layer names and schemas may vary                                            |

Before making any serious decision, verify the parcel manually in:

* OSS
* cadastral records
* zemljišna knjiga
* QGIS
* local spatial/zoning plans

## Troubleshooting

### ZIP not found

Check:

```python
TARGET_KO_NAME = "SESVETSKI KRALJEVEC"
TARGET_MBKO = "325392"
```

If it fails, inspect debug files:

```text
katastar_output/_debug/
```

### No results found

Try widening the area range:

```python
AREA_MIN_M2 = 600
AREA_MAX_M2 = 900
```

Or try a different area source:

```python
AREA_SOURCE = "geometry"
```

### Property description is empty

Possible causes:

* The DKP ZIP has no useful building/land-use layer.
* The relevant fields have unexpected column names.
* The parcel is empty land.
* Description layer detection needs tuning.

Inspect printed layer candidates in the terminal output.

### CSV has strange columns

The script exports all non-excluded parcel attributes after the preferred columns. This is intentional so useful cadastral fields are not accidentally lost.

If the CSV is too wide, restrict `csv_other_cols` in `export_results()`.

## Recommended Workflow

1. Run the script.
2. Open the CSV.
3. Sort/filter by `area_used_m2`.
4. Open promising `google_maps_url` values.
5. Open the GPKG in QGIS for geometry inspection.
6. Verify interesting parcels manually in OSS and land registry records.

## Pros / Cons

| Approach             | Pros                                          | Cons                                   |
| -------------------- | --------------------------------------------- | -------------------------------------- |
| This script          | Fast bulk filtering, repeatable, CSV-friendly | Requires Python/GIS dependencies       |
| OSS manual search    | Official familiar interface                   | Bad for bulk search                    |
| QGIS manual workflow | Good visual control                           | Less convenient for repeated filtering |
| WFS `bbox`           | Useful for small map queries                  | Fragile, server limits, HTTP 400 risk  |

## Confidence

| Item                                                 | Confidence |
| ---------------------------------------------------- | ---------: |
| ATOM download approach is the right default          |       0.93 |
| Google Maps link generation is reliable              |       0.98 |
| Area filtering is technically sound                  |       0.91 |
| Property description may need schema-specific tuning |       0.80 |
| Output still requires legal/manual verification      |       0.99 |
