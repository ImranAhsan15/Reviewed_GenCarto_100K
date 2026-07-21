"""Create a small clipped copy of a geodatabase for fast debugging.

Builds a fresh <gdb_name>_clipped.gdb next to the original in three steps:
  1. Transfers the COMPLETE schema via an XML workspace document (schema-only
     export/import) - feature datasets, fields, domains, subtypes, attribute
     rules, contingent values, relationship classes all carried over.
  2. Clips every feature class with the supplied AOI polygon - in PARALLEL
     worker processes, each writing to its own temp GDB - then loads the
     clipped features into the matching (same-named, still empty) feature
     classes in the new GDB (sequentially: a file GDB is single-writer).
  3. Copies standalone table rows in full (no geometry to clip).

Building fresh - instead of copying the whole GDB and deleting rows - keeps
the output size proportional to the clipped data, because a file GDB never
returns disk space for deleted rows.

Usage (run with the ArcGIS Pro Python, e.g. propy.bat):
    python create_clipped_gdb.py <input_gdb> <clip_polygon_fc> [--overwrite] [--workers N]

    input_gdb        Path to the source file geodatabase (.gdb)
    clip_polygon_fc  Polygon feature class / shapefile with the debug AOI
    --overwrite      Replace <gdb_name>_clipped.gdb if it already exists
    --workers N      Parallel clip processes (default: CPU count - 1, max 6)

Close ArcGIS Pro (or make sure the source GDB is not being edited) before
running, otherwise schema export or clipping can fail on lock files.

Note: if a feature class has attribute rules, they also apply while the
clipped features are loaded (same as any edit against that schema).
"""
import multiprocessing
import os
import shutil
import sys
import time

import arcpy
import arcgisscripting


def msg(text):
    # AddMessage echoes to stdout in standalone runs and to the tool messages
    # when run inside ArcGIS Pro, so it covers both cases.
    arcpy.AddMessage(text)


def clip_worker(worker_id, rel_paths, input_gdb, clip_poly, temp_dir):
    """Clip a batch of feature classes into this worker's own temp GDB.
    Runs in a separate process. Returns [(rel_path, temp_fc|None, kept, error|None)]."""
    import arcpy as worker_arcpy  # each process needs its own arcpy session
    worker_arcpy.env.overwriteOutput = True
    worker_arcpy.env.parallelProcessingFactor = "0"  # processes are the parallelism

    gdb_name = f"clip_worker_{worker_id}.gdb"
    temp_gdb = os.path.join(temp_dir, gdb_name)
    if not worker_arcpy.Exists(temp_gdb):
        worker_arcpy.management.CreateFileGDB(temp_dir, gdb_name)

    results = []
    for rel in rel_paths:
        source_fc = os.path.join(input_gdb, rel)
        flat_name = rel.replace("\\", "_").replace("/", "_")
        out_fc = os.path.join(temp_gdb, flat_name)
        try:
            try:
                worker_arcpy.analysis.PairwiseClip(source_fc, clip_poly, out_fc)
            except Exception:
                # Some feature types (e.g. annotation) need the classic tool
                worker_arcpy.analysis.Clip(source_fc, clip_poly, out_fc)
            kept = int(worker_arcpy.management.GetCount(out_fc)[0])
            results.append((rel, out_fc if kept > 0 else None, kept, None))
        except Exception as e:
            results.append((rel, None, 0, str(e)))
    return results


def validate_inputs(input_gdb, clip_poly):
    """Run all input checks before any work starts. Returns a list of error
    strings (empty list = valid). Cheap checks only - no data is copied."""
    errors = []

    # -- Input geodatabase --
    if not input_gdb.lower().endswith(".gdb") or not arcpy.Exists(input_gdb):
        errors.append(f"Input geodatabase not found (or not a .gdb): {input_gdb}")
        return errors  # nothing else can be checked without it

    fcs = [os.path.join(dirpath, name)
           for dirpath, dirnames, filenames in arcpy.da.Walk(input_gdb, datatype="FeatureClass")
           for name in filenames]
    if not fcs:
        errors.append(f"Input geodatabase contains no feature classes: {input_gdb}")
    else:
        msg(f"Check: input GDB contains {len(fcs)} feature classes - OK")

    lock_files = [f for f in os.listdir(input_gdb) if f.endswith(".lock")]
    if lock_files:
        msg(f"WARNING: {len(lock_files)} .lock file(s) found in the input GDB. "
            "If ArcGIS Pro currently has it open, close Pro first or clipping may fail.")

    # -- Clip polygon --
    if not arcpy.Exists(clip_poly):
        errors.append(f"Clip polygon feature class not found: {clip_poly}")
        return errors

    clip_desc = arcpy.da.Describe(clip_poly)
    if clip_desc.get("shapeType") != "Polygon":
        errors.append(f"Clip features must be polygons, got: {clip_desc.get('shapeType')}")
        return errors
    msg("Check: clip input is a polygon feature class - OK")

    if int(arcpy.management.GetCount(clip_poly)[0]) == 0:
        errors.append("Clip polygon feature class has no features")
        return errors
    msg("Check: clip polygon has features - OK")

    clip_sr = clip_desc.get("spatialReference")
    if clip_sr is None or clip_sr.name in ("", "Unknown"):
        errors.append("Clip polygon has an unknown spatial reference - define its projection first")
        return errors

    # -- Spatial reference and overlap against the GDB data --
    if fcs:
        gdb_sr = arcpy.da.Describe(fcs[0]).get("spatialReference")
        if gdb_sr is not None and gdb_sr.factoryCode != clip_sr.factoryCode:
            msg(f"WARNING: clip polygon spatial reference ({clip_sr.name}) differs from the "
                f"geodatabase ({gdb_sr.name}). Clipping will reproject on the fly, but verify "
                "the result carefully - a wrong datum transformation can shift features.")

        # The clip area must actually overlap the data, otherwise the output
        # would be a fully empty GDB.
        clip_extent_poly = arcpy.da.Describe(clip_poly)["extent"].polygon
        overlaps = False
        for fc in fcs:
            desc = arcpy.da.Describe(fc)
            extent = desc.get("extent")
            if extent is None or extent.XMin is None:
                continue  # empty feature class
            fc_extent_poly = extent.polygon
            if fc_extent_poly.spatialReference.factoryCode != clip_sr.factoryCode:
                fc_extent_poly = fc_extent_poly.projectAs(clip_sr)
            if not clip_extent_poly.disjoint(fc_extent_poly):
                overlaps = True
                break
        if not overlaps:
            errors.append("Clip polygon does not overlap the extent of any feature class in the "
                          "input geodatabase - the clipped GDB would be empty. Check that the "
                          "polygon was drawn over the data area (and in the right coordinate system).")
        else:
            msg("Check: clip polygon overlaps the geodatabase data extent - OK")

    return errors


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    overwrite = "--overwrite" in sys.argv
    workers = None
    for i, a in enumerate(sys.argv):
        if a == "--workers" and i + 1 < len(sys.argv):
            workers = int(sys.argv[i + 1])
            args = [x for x in args if x != sys.argv[i + 1]]
    if len(args) != 2:
        print(__doc__)
        return 1
    if workers is None:
        workers = min(max((os.cpu_count() or 2) - 1, 1), 6)

    input_gdb = os.path.abspath(args[0])
    clip_poly = args[1]

    # --- Validate both inputs before any work starts ----------------------
    msg("Validating inputs ...")
    errors = validate_inputs(input_gdb, clip_poly)
    if errors:
        for e in errors:
            msg(f"ERROR: {e}")
        msg("Validation failed - nothing was copied or modified.")
        return 1
    msg("Validation passed.\n")

    gdb_dir = os.path.dirname(input_gdb)
    gdb_name = os.path.splitext(os.path.basename(input_gdb))[0]
    out_gdb = os.path.join(gdb_dir, f"{gdb_name}_clipped.gdb")

    if arcpy.Exists(out_gdb):
        if overwrite:
            msg(f"Deleting existing {out_gdb}")
            arcpy.management.Delete(out_gdb)
        else:
            msg(f"ERROR: {out_gdb} already exists. Re-run with --overwrite to replace it.")
            return 1

    arcpy.env.overwriteOutput = True
    start = time.time()

    # --- 1. Create the output GDB with the COMPLETE schema (no data) ------
    msg(f"Creating {out_gdb} ...")
    arcpy.management.CreateFileGDB(os.path.dirname(out_gdb), os.path.basename(out_gdb))

    schema_xml = os.path.join(gdb_dir, f"{gdb_name}_schema.xml")
    if os.path.exists(schema_xml):
        os.remove(schema_xml)
    msg("Exporting schema (feature datasets, domains, subtypes, attribute rules) ...")
    arcpy.management.ExportXMLWorkspaceDocument(input_gdb, schema_xml, "SCHEMA_ONLY")
    arcpy.management.ImportXMLWorkspaceDocument(out_gdb, schema_xml, "SCHEMA_ONLY")
    os.remove(schema_xml)
    msg("Schema transferred")

    # --- 2. Clip all feature classes in parallel worker processes ---------
    jobs = []          # rel paths of non-empty feature classes
    total_source = 0
    processed = failed = 0
    for dirpath, dirnames, filenames in arcpy.da.Walk(input_gdb, datatype="FeatureClass"):
        for name in filenames:
            source_fc = os.path.join(dirpath, name)
            rel = os.path.relpath(source_fc, input_gdb)
            src_count = int(arcpy.management.GetCount(source_fc)[0])
            total_source += src_count
            if src_count == 0:
                msg(f"  {rel}: source empty, left empty")
                processed += 1
            else:
                jobs.append((rel, src_count))

    src_counts = dict(jobs)
    # Distribute round-robin by size so workers get comparable loads
    jobs.sort(key=lambda j: j[1], reverse=True)
    batches = [[] for _ in range(min(workers, max(len(jobs), 1)))]
    for i, (rel, _cnt) in enumerate(jobs):
        batches[i % len(batches)].append(rel)

    temp_dir = os.path.join(gdb_dir, f"{gdb_name}_clip_tmp")
    os.makedirs(temp_dir, exist_ok=True)

    # When launched from inside ArcGIS Pro, sys.executable is ArcGISPro.exe;
    # point multiprocessing at the real python so workers can spawn.
    python_exe = os.path.join(sys.exec_prefix, "python.exe")
    if os.path.exists(python_exe):
        multiprocessing.set_executable(python_exe)

    msg(f"\nClipping {len(jobs)} feature classes with {len(batches)} parallel workers ...")
    clip_start = time.time()
    all_results = []
    if len(batches) == 1:
        all_results.extend(clip_worker(0, batches[0], input_gdb, clip_poly, temp_dir))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=len(batches)) as pool:
            futures = [pool.submit(clip_worker, i, batch, input_gdb, clip_poly, temp_dir)
                       for i, batch in enumerate(batches)]
            for future in as_completed(futures):
                all_results.extend(future.result())
    msg(f"Parallel clipping finished in {time.time() - clip_start:.0f} s")

    # --- Load results into the output GDB (single writer) -----------------
    msg("Loading clipped features into the output geodatabase ...")
    total_kept = 0
    for rel, temp_fc, kept, error in sorted(all_results):
        if error is not None:
            msg(f"  WARNING {rel}: could not clip ({error}); layer left empty (schema intact)")
            failed += 1
            continue
        if kept > 0:
            arcpy.management.Append(temp_fc, os.path.join(out_gdb, rel), "NO_TEST")
        total_kept += kept
        msg(f"  {rel}: {kept} of {src_counts.get(rel, '?')} features kept")
        processed += 1

    shutil.rmtree(temp_dir, ignore_errors=True)

    # --- 3. Copy standalone table rows in full (no geometry to clip) ------
    for dirpath, dirnames, filenames in arcpy.da.Walk(input_gdb, datatype="Table"):
        for name in filenames:
            src_table = os.path.join(dirpath, name)
            if int(arcpy.management.GetCount(src_table)[0]) > 0:
                arcpy.management.Append(src_table, os.path.join(out_gdb, name), "NO_TEST")
            msg(f"  table rows copied in full: {name}")

    # --- Finish -----------------------------------------------------------
    msg("Compacting output geodatabase ...")
    arcpy.management.Compact(out_gdb)

    elapsed = time.time() - start
    msg(f"\nDone in {elapsed:.0f} s")
    msg(f"Feature classes processed: {processed}, failed: {failed}")
    msg(f"Features: {total_kept} of {total_source} kept ({out_gdb})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
