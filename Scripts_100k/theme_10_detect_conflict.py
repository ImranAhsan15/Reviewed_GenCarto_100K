import arcpy
import pandas as pd
import os
import traceback
import sys
from common_utils import *

# # Writing Detection of Conflicts
def detect_write_conflicts(in_feature_loc, inputFCs, compareFCs, rev_workspace, partitions, map_name, symbology_file_path, val_dict, logger, working_gdb):
    
    arcpy.AddMessage('Starting conflicts detection.....')
    # values 'NEVER', 'NO_DISTANCE', 'ALL'
    # this value determines when we use symbology with no outline rather than using
    # representation symbology.  This only works for polygon layers.
    # NEVER - will always try to use represenation symbology
    # NO_DISTANCE - will only set symbology with no ouline when the search distance is 0
    # ALL - will always try to use symbology with no ouline.

    # Define environment variables (match original behavior)
    arcpy.env.overwriteOutput = True
    arcpy.env.addOutputsToMap = False
    arcpy.env.parallelProcessingFactor = "100%"
    arcpy.env.workspace = working_gdb

    USE_NO_OUTLINE = 'ALL'
    try:
        inLayers = []
        compareLayers = []
        comparison = {}

        arcpy.CheckOutExtension("datareviewer")

        total_conflict = 0

        # Set the reference scale and partitions
        arcpy.env.referenceScale = val_dict['Detect_reference_scale']
        arcpy.env.cartographicPartitions = partitions

        # Set spatial reference from first input FC
        fc = inputFCs[0]
        desc = arcpy.da.Describe(fc)
        sr = desc['spatialReference']
        arcpy.env.cartographicCoordinateSystem = sr

        # Decide symbology (match original)
        symbology = ""
        if USE_NO_OUTLINE == "ALL":
            symbology = "NO_OUTLINE"
        elif USE_NO_OUTLINE == "NO_DISTANCE":
            dist = val_dict['Detect_conflict_distance']
            if dist == '0':
                symbology = "NO_OUTLINE"

        inLayers = prepFcs(inputFCs, in_feature_loc, map_name, symbology_file_path, val_dict['Detect_expression'], symbology)
        if len(inLayers) >= 1:
            compareLayers = prepFcs(compareFCs, in_feature_loc, map_name, symbology_file_path, val_dict['Detect_expression'], symbology)
            outfcname = "detectconflict"

            for inlyr in inLayers:
                compareTo = []
                in_name = arcpy.da.Describe(inlyr)['name']

                for conflict_lyr_ID in compareLayers:
                    compared = False

                    # Skip if already compared in opposite order (match original logic)
                    compare_name = arcpy.da.Describe(conflict_lyr_ID)['name']
                    if compare_name in comparison:
                        vals = comparison[compare_name]
                        if in_name in vals:
                            arcpy.AddMessage("Already compared " + str(inlyr) + " to " + str(conflict_lyr_ID) + " skipping...")
                            compared = True

                    if not compared:
                        compareTo.append(str(compare_name))
                        arcpy.AddMessage("Comparing " + str(inlyr) + " to " + str(conflict_lyr_ID))

                        # Run DetectGraphicConflict (original: fixed name in current workspace)
                        outfc = arcpy.cartography.DetectGraphicConflict(inlyr, conflict_lyr_ID, outfcname, val_dict['Detect_conflict_distance'])
                        arcpy.AddMessage(arcpy.GetMessages())

                        # Repair and count (match original)
                        arcpy.management.RepairGeometry(outfc)
                        
                        number_conflict = int(arcpy.management.GetCount(outfc)[0])
                        arcpy.AddMessage(str(number_conflict) + " conflicts were found.")

                        if number_conflict >= 1:
                            error_count = write2Rev(outfc, rev_workspace, val_dict['Detect_reviewer_session'], str(val_dict['Detect_severity']))
                            if(error_count):
                                total_conflict = total_conflict + int(error_count)

                comparison[in_name] = compareTo
                inLayers.remove(inlyr)

        # Check extension back in (match original placement)
        arcpy.CheckInExtension("datareviewer")

        # Delete temp layers (match original)
        for lyr in inLayers:
            if arcpy.Exists(lyr):
                arcpy.management.Delete(lyr)
        for lyr in compareLayers:
            if arcpy.Exists(lyr):
                arcpy.management.Delete(lyr)
        return total_conflict
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Detect conflict error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Detect conflict', f'{e}\n')


def run_gdb_audit_and_validation(input_gdb, output_gdb_name, excel_file_name, logger, output_dir=None):
    """
    Audit GlobalID / editor-tracking requirements, evaluate validation rules,
    and write any validation errors to a new GDB and an Excel report.

    output_dir : where the report GDB and Excel are written. Defaults to the
    folder that contains input_gdb (os.getcwd() is unpredictable when the tool
    is run as a script tool from ArcGIS Pro).
    """
    try:
        base_dir = output_dir or os.path.dirname(input_gdb)
        output_gdb_path = os.path.join(base_dir, output_gdb_name)
        excel_full_path = os.path.join(base_dir, excel_file_name)

        arcpy.env.workspace = input_gdb
        arcpy.env.overwriteOutput = True

        # 1. Audit and enable requirements (GlobalID & editor tracking)
        for dirpath, dirnames, filenames in arcpy.da.Walk(input_gdb, datatype="FeatureClass"):
            for filename in filenames:
                fc_path = os.path.join(dirpath, filename)
                desc = arcpy.Describe(fc_path)

                if not desc.hasGlobalID:
                    arcpy.AddWarning(f"Requirement Missing: Adding Global IDs to {filename}")
                    arcpy.management.AddGlobalIDs(fc_path)

                if not desc.editorTrackingEnabled:
                    arcpy.AddWarning(f"Requirement Missing: Enabling Editor Tracking for {filename}")
                    arcpy.management.EnableEditorTracking(
                        fc_path, "created_user", "created_date",
                        "last_edited_user", "last_edited_date", "ADD_FIELDS", "UTC"
                    )

        # 2. Evaluate validation rules (populates the GDB system error tables).
        # Guarded: a GDB with no validation rules would otherwise abort the audit.
        arcpy.AddMessage("Executing EvaluateRules...")
        try:
            arcpy.management.EvaluateRules(input_gdb, "VALIDATION_RULES", None, "ASYNC")
        except Exception as e:
            arcpy.AddWarning(f"EvaluateRules did not run (no validation rules defined?): {e}")
            logger.warning(f"EvaluateRules did not run: {e}")

        # 3. Create output geodatabase
        if not arcpy.Exists(output_gdb_path):
            arcpy.AddMessage(f"Creating output GDB: {output_gdb_path}")
            arcpy.management.CreateFileGDB(base_dir, output_gdb_name)

        # 4. Process system error tables
        error_tables = [
            "GDB_ValidationPointErrors",
            "GDB_ValidationLineErrors",
            "GDB_ValidationPolyErrors",
            "GDB_ValidationObjectErrors",
        ]

        with pd.ExcelWriter(excel_full_path, engine='openpyxl') as writer:
            wrote_sheet = False
            for table_name in error_tables:
                source_table = os.path.join(input_gdb, table_name)
                if arcpy.Exists(source_table):
                    arcpy.management.Copy(source_table, os.path.join(output_gdb_path, table_name))

                    fields = [f.name for f in arcpy.ListFields(source_table) if f.type != 'Geometry']
                    data = [row for row in arcpy.da.SearchCursor(source_table, fields)]
                    pd.DataFrame(data, columns=fields).to_excel(writer, sheet_name=table_name[:31], index=False)
                    wrote_sheet = True
                else:
                    arcpy.AddMessage(f"System table {table_name} not found. Ensure validation is enabled.")

            # openpyxl requires at least one sheet; add a summary when no error
            # tables exist so the writer does not fail on close.
            if not wrote_sheet:
                pd.DataFrame({"info": ["No validation error tables found in the geodatabase."]}).to_excel(
                    writer, sheet_name="Summary", index=False)

        arcpy.AddMessage(f"Process Complete.\nGeodatabase: {output_gdb_path}\nExcel Log: {excel_full_path}")
        logger.info(f"Detect Conflict validation report written to {excel_full_path}")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"GDB audit and validation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('GDB audit and validation', f'{e}\n')
        raise

