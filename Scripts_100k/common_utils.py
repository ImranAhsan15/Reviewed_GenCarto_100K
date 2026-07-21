
import arcpy
import arcgisscripting
import traceback
import math
import os
import shutil
import time
import sys
import logging
import arcpy.reviewer
from datetime import datetime
import glob
from get_param_vals import Validator, LayerNames
from collections import defaultdict
from pathlib import Path
import csv

def validate_user_inputs(theme_type, params, logger):
    """Validate the tool's user-supplied parameters before any processing
    starts, so a missing or wrong path is reported up front (all problems at
    once) instead of failing part-way through a theme.

    params: dict with keys in_feature_loc, excel_file, hierarchy_file,
            symbology_file_path, vst_workspace, out_workspace.
    Returns a list of error strings (empty list = all inputs valid).
    """
    errors = []

    def need_gdb(label, val):
        if not val or not str(val).strip():
            errors.append(f"{label} is not set.")
        elif not arcpy.Exists(val):
            errors.append(f"{label} was not found: {val}")

    def need_file(label, val):
        if not val or not str(val).strip():
            errors.append(f"{label} is not set.")
        elif not os.path.isfile(val):
            errors.append(f"{label} file was not found: {val}")

    def need_folder(label, val):
        if not val or not str(val).strip():
            errors.append(f"{label} is not set.")
        elif not os.path.isdir(val):
            errors.append(f"{label} folder was not found: {val}")

    if not theme_type or not str(theme_type).strip():
        errors.append("Theme type is not selected.")

    # Required for every theme
    need_gdb("Input geodatabase", params.get("in_feature_loc"))
    need_file("Configuration Excel file", params.get("excel_file"))

    # Theme-specific inputs (only the parameters each theme actually uses)
    if theme_type in ('2-Transportation Generalization', '8-Apply Carto Symbology'):
        need_file("Hierarchy file", params.get("hierarchy_file"))
    if theme_type in ('8-Apply Carto Symbology', '9a-Resolve Conflict for Lines',
                      '9b-Resolve Conflict for Polygons'):
        need_folder("Symbology (Style file) folder", params.get("symbology_file_path"))
    if theme_type == '8-Apply Carto Symbology':
        need_gdb("VST workspace (Product Library geodatabase)", params.get("vst_workspace"))
    if theme_type == '11-Load Data into CARTO50K':
        need_gdb("Output workspace", params.get("out_workspace"))

    if errors:
        header = f"Input validation failed for theme '{theme_type}'. Please correct the following and run again:"
        arcpy.AddError(header)
        logger.error(header)
        for e in errors:
            arcpy.AddError(f"  - {e}")
            logger.error(f"  - {e}")
    else:
        logger.info("Input validation passed: all required user inputs are present.")
    return errors

def error_msgs(log_dir):
    try:
        # Current datetime
        current_time = time.time()
        # Log directory
        log_file = f"{log_dir}\\GenCarto50k_{time.strftime('%Y%m%d', time.localtime(current_time))}.log"
        
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)

        # Configure logging
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y%m%dT%H%M%S')
        
        # Create a logger instance
        logger = logging.getLogger()
        return logger
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Logger error message error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def has_features(fc):
    with arcpy.da.SearchCursor(fc, ['OID@']) as cursor:
        return next(cursor, None) is not None  #True is there is at least one feature
    
def count_features(fc):
    return len([row for row in arcpy.da.SearchCursor(fc, ["OID@"])])

def simplified_msgs(error_method, custom_message):
    try:
        # Simplified log file
        s_log_dir = os.path.dirname(arcpy.env.scratchGDB)
        # Current datetime
        current_date = datetime.now().strftime('%Y%m%d')
        # Log directory
        log_file = f"{s_log_dir}\\GenCarto50k_Simplified_Error_Log_{current_date}.csv"
        with open(log_file, 'a') as f:
            f.write(f'{datetime.now().strftime("%Y%m%dT%H%M%S")} Error for {error_method}\n')
            f.write('{0}'.format(custom_message))
            f.close()

    except Exception as e:
            tb = traceback.format_exc()
            error_message = f"Simplified message error: {e}\nTraceback details:\n{tb}"
            arcpy.AddError(error_message)

_EXTENSION_DISPLAY_NAMES = {
    "Foundation": "Production Mapping",
    "datareviewer": "Data Reviewer",
    "Spatial": "Spatial Analyst",
}

def check_required_licenses(logger, extensions=(), product=None):
    """Verify the product level and extension licenses a theme needs before any
    processing starts, so a missing license stops the run in seconds instead of
    failing hours into a theme.

    product    : arcpy.CheckProduct token, e.g. 'ArcInfo' for Advanced.
    extensions : iterable of arcpy.CheckExtension codes, e.g. ('Foundation',).
    Raises RuntimeError if anything is unavailable.
    """
    if product:
        status = arcpy.CheckProduct(product)
        if status not in ("AlreadyInitialized", "Available"):
            msg = (f"Required ArcGIS Pro license level '{product}' is not available (status: {status}). "
                   "The cartography tools in this pipeline need the Advanced license. "
                   "Check your license level in ArcGIS Pro under Settings > Licensing, then run again.")
            arcpy.AddError(msg)
            logger.error(msg)
            raise RuntimeError(msg)
        logger.info(f"Product license '{product}' verified (status: {status})")

    for ext in extensions:
        display = _EXTENSION_DISPLAY_NAMES.get(ext, ext)
        status = arcpy.CheckExtension(ext)
        if status == "Available":
            arcpy.CheckOutExtension(ext)
            logger.info(f"Extension '{display}' ({ext}) checked out")
        else:
            msg = (f"Required extension license '{display}' ({ext}) is not available (status: {status}). "
                   "Enable it in ArcGIS Pro under Settings > Licensing (or ask your license administrator "
                   "to assign it), then run again.")
            arcpy.AddError(msg)
            logger.error(msg)
            raise RuntimeError(msg)

# fc_dict keys that hold queries/expressions/field names rather than feature
# class names - excluded from the feature class name validation below.
_NON_FC_NAME_KEYS = {
    'not_include_fields', 'railway_sql', 'collapse_sql', 'sql', 'express_list',
    'query_list', 'field_list', 'road_query', 'bridge_query', 'veg_field_values',
}

def validate_config_fc_names(fc_dict, in_workspace, logger):
    """Compare every feature class name referenced by the Excel configuration
    (and the common_layer_names sheet) against the feature classes that exist
    in the geodatabase. Any name that matches nothing - usually a typo in the
    Excel file - is reported as a warning and written to the log BEFORE any
    processing starts. Returns the list of (config key, name) mismatches."""
    gdb_fc_names = {name for dirpath, dirnames, filenames
                    in arcpy.da.Walk(in_workspace, datatype=['FeatureClass'])
                    for name in filenames}

    def name_exists(name):
        # The pipeline matches config names as substrings of feature class
        # names/paths, so mirror that here.
        return any(name in fc_name for fc_name in gdb_fc_names)

    problems = []
    for key, value in fc_dict.items():
        if key in _NON_FC_NAME_KEYS or value is None:
            continue
        if isinstance(value, dict):
            names = list(value.keys())
        elif isinstance(value, (list, tuple)):
            names = value
        else:
            names = [value]
        for name in names:
            name = str(name).strip().strip("'\"")
            if not name:
                continue
            if not name_exists(name):
                problems.append((key, name))

    # Names from the common_layer_names sheet are matched the same way
    try:
        for lyr_key, lyr_name in resolve_lyr().items():
            if lyr_name is None or not str(lyr_name).strip():
                continue
            if not name_exists(str(lyr_name).strip()):
                problems.append((f"common_layer_names:{lyr_key}", str(lyr_name).strip()))
    except RuntimeError:
        pass  # layer name resolver not initialized

    if problems:
        # Details go to the log file only; the tool messages get a single
        # one-line pointer so the run is not flooded with warnings.
        logger.warning(f"Configuration check: {len(problems)} feature class name(s) in the Excel "
                       f"configuration match no feature class in {in_workspace}. "
                       "Check the Excel file for typos - these features will be skipped by the tools that use them:")
        for key, name in problems:
            logger.warning(f"  [{key}] '{name}' not found in geodatabase")
        arcpy.AddMessage(f"Configuration check: {len(problems)} unmatched feature class name(s) - see the log file for the list")
    else:
        logger.info("Configuration check: all feature class names in the Excel configuration match the geodatabase")
    return problems

def get_fcs(in_workspace, dataset_name, logger):
    """ gets a list of all feature classes in a database, includes feature
    classes inside and outside of the feature datasets"""
    # Set environment
    arcpy.env.overwriteOutput = True
    try:
        if dataset_name == 'Topo':
            fc_classes = [os.path.join(dirpaths, filename) for dirpaths, dirnames, filenames in arcpy.da.Walk(in_workspace, datatype=['FeatureClass']) 
                          for filename in filenames if dirpaths.endswith(dataset_name)]
        else:
            fc_classes = [os.path.join(dirpaths, filename) for dirpaths, dirnames, filenames in arcpy.da.Walk(in_workspace, datatype=['FeatureClass']) 
                          for filename in filenames if dirpaths.endswith('.gdb')]
        return fc_classes

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'Create feature classes error: {e}\nTraceback details:\n{tb}'
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Create feature classes', f'{exc_value}\n')
        # Re-raise: callers immediately sort/iterate this list, so continuing
        # with None only produces a misleading TypeError downstream.
        raise

def backup_data(src, dest, logger, ignore=None):
    '''
    Recursively back up data from src to dest.
    '''
    try:
        # shutil.copy2(src, dest)
        if os.path.isdir(src):       
            if not os.path.isdir(dest):
                os.makedirs(dest)
            files = os.listdir(src)
            if ignore is not None:
                ignored = ignore(src, files)
            else:
                ignored = set()
            for f in files:
                if f not in ignored:
                    backup_data(os.path.join(src, f), os.path.join(dest, f), logger, ignore)
        else:
            if not src.endswith(".lock"): 
                shutil.copy2(src, dest)

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'Backup data error: {e}\nTraceback details:\n{tb}'
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Backup data', f'{exc_value}\n')

def get_fields(featureclass, not_include_fields, logger):
    # Set environment variables
    arcpy.env.overwriteOutput = True
    try:
        desc = arcpy.da.Describe(featureclass)
        length_field = desc['lengthFieldName']

        field_names = []

        fields = arcpy.ListFields(featureclass)
        for field in fields:
            if field.type not in not_include_fields:
                if field.name != length_field:
                    if 'user' not in field.name:
                        field_names.append(field.name)

        return field_names
    
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Get fields error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Get fields', f'{exc_value}\n')

def find_dangles(split_layer, update_field, working_gdb, logger):
    ''' for some reason, feature vertices to points with the dangles option
    returns 0 for dangles with the line layer.  This function mimics what the
    dangles option does'''
    # Set environment variables
    arcpy.env.overwriteOutput = True
    try:
        arcpy.AddMessage("Find Dangles")
        arcpy.management.SelectLayerByAttribute(split_layer, "NEW_SELECTION", f'{update_field} = 1')
        arcpy.management.FeatureVerticesToPoints(split_layer, "DangleVertex", "DANGLE")
        count_dangle = int(arcpy.management.GetCount("DangleVertex")[0])
        if count_dangle == 0:
            arcpy.management.FeatureVerticesToPoints(split_layer, "DangleVertex", "BOTH_ENDS")
            point_lyr = arcpy.management.MakeFeatureLayer("DangleVertex", f"{working_gdb}\\Dangle_lyr")

            # A dangle is an end point that is not coincident (within the XY
            # tolerance) with any other end point. Bucket the points by
            # coordinate so each point is only compared against points in its
            # own and neighbouring cells instead of every other point.
            sr = arcpy.da.Describe("DangleVertex")['spatialReference']
            tolerance = getattr(sr, 'XYTolerance', 0.001) or 0.001
            points = []
            buckets = defaultdict(list)
            with arcpy.da.SearchCursor(point_lyr, ['OID@', 'SHAPE@XY']) as cursor:
                for oid, (x, y) in cursor:
                    points.append((oid, x, y))
                    buckets[(int(x // tolerance), int(y // tolerance))].append((oid, x, y))

            dangle_ids = []
            for oid, x, y in points:
                cell_x, cell_y = int(x // tolerance), int(y // tolerance)
                match = False
                for nx in (cell_x - 1, cell_x, cell_x + 1):
                    for ny in (cell_y - 1, cell_y, cell_y + 1):
                        for other_oid, ox, oy in buckets.get((nx, ny), ()):
                            if other_oid != oid and math.hypot(x - ox, y - oy) <= tolerance:
                                match = True
                                break
                        if match:
                            break
                    if match:
                        break
                if not match:
                    dangle_ids.append(str(oid))

            if len(dangle_ids) >= 1:
                where_clause = f"OBJECTID IN ({', '.join(dangle_ids)})"
                arcpy.management.SelectLayerByAttribute(point_lyr, "NEW_SELECTION", where_clause)
        else:
            point_lyr = arcpy.management.MakeFeatureLayer("DangleVertex", f"{working_gdb}\\Dangle_lyr")

        return point_lyr
    
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Find dangles error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Find dangles', f'{exc_value}\n')

def create_new_geo(temp_fc, has_z):
    """builds a new geometry from the temp_poly lines"""
    try:
        geometries = [row[0] for row in arcpy.da.SearchCursor(temp_fc, ['SHAPE@'])]
        # geometries = arcpy.management.CopyFeatures(temp_fc, arcpy.Geometry())
        # Break the geometry down into each part
        geo_array = arcpy.Array()

        for geometry in geometries:
            for part in geometry:
                # Define an empty array for each part of the geometry
                array = arcpy.Array()

                # Loop through each vertex in the part
                for pnt in part:
                    # If there is a vertex
                    if pnt:
                        if has_z:
                            array.append(arcpy.Point(pnt.X, pnt.Y, pnt.Z))
                        else:
                            array.append(arcpy.Point(pnt.X, pnt.Y))
                    # If there is not a vertex this means there is a new part
                    else:
                        # Add the part to the new geometry array and clear the array for the part
                        geo_array.add(array)
                        array = arcpy.Array()

                # Add the final part to the geometry array
                geo_array.add(array)

        return geo_array
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Create new geo error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)



# def rebuild_features(line_lyr, output_lyr, shape_type, unique_ids, left_field, working_gdb, right_field):
#     """ reconstructs polygon geometries from a line feature class that has
#     left and right polygon id values """
#     # Set environment
#     arcpy.env.overwriteOutput = 1
#     arcpy.env.workspace = working_gdb

#     try:
#         dup_lyr = arcpy.management.MakeFeatureLayer(line_lyr, "dup_lyr")

#         # Get information about geometry of output (spatial ref and if has Z)
#         spatial_ref = arcpy.da.Describe(output_lyr)['spatialReference']
#         has_z = arcpy.da.Describe(output_lyr)['hasZ']

#         oid_field = arcpy.da.Describe(output_lyr)['OIDFieldName']
#         name = arcpy.da.Describe(output_lyr)['name']

#         for id_value in unique_ids:
            
#             arcpy.AddMessage("Creating new geometry for feature id " + str(id_value))
#             new_size = 0
#             # Select the generalized lines relating to an input feature
#             query = left_field + " = " + str(id_value)
#             if shape_type == "Polyline":
#                 arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query)
#             if shape_type == "Polygon":
#                 query = query + " OR " + right_field + " = " + str(id_value)
#                 arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query)

#                 # If a line has the same value for both the left and right ID
#                 # This means that there is a feature that overlaps the original
#                 # feature.  These lines and any identical lines need to be
#                 # removed from the selection.
#                 remove_query = left_field + " = " + str(id_value) + " AND " + right_field + " = " + str(id_value)
#                 arcpy.management.SelectLayerByAttribute(dup_lyr, "NEW_SELECTION", remove_query)
#                 arcpy.management.SelectLayerByLocation(line_lyr, "ARE_IDENTICAL_TO", dup_lyr, None, "REMOVE_FROM_SELECTION")
#             # Dissolve the line features to create one or more closed lines
#             temp_fc = arcpy.management.Dissolve(line_lyr, f"{working_gdb}\\Temp_FC", "", "", "SINGLE_PART", "DISSOLVE_LINES")

#             # Update the geometries in the output feature class
#             query = oid_field + " = " + str(id_value)
#             with arcpy.da.UpdateCursor(output_lyr, ['OID@', 'SHAPE@'], query) as cursor:
#                 for row in cursor:

#                     array = create_new_geo(temp_fc, has_z)
#                     # Create the new polygon or line geometries
#                     if shape_type == "Polygon":
#                         #Create a geometry array from the dissolved line
#                         new_geo = arcpy.Polygon(array, spatial_ref, has_z)
#                         arcpy.AddMessage("New Area " + str(new_geo.area))
#                         new_size = new_geo.area
#                     elif shape_type == "Polyline":
#                         new_geo = arcpy.Polyline(array, spatial_ref, has_z)
#                         arcpy.AddMessage("New Length " + str(new_geo.length))
#                         new_size = new_geo.length

#                     # If the feature is too small to be a valid geometry,
#                     # Delete the simiplified feature
#                     if new_size == 0 or new_size == None:
#                         arcpy.AddWarning("Size of generalized feature " + str(row[0])
#                         + " is too small, feature will be deleted.")
#                         cursor.deleteRow()

#                     # Otherwise update the geometry of the output feature
#                     elif new_size > 0:
#                         arcpy.AddMessage("Updating row")
#                         row[1] = new_geo
#                         cursor.updateRow(row)
#         # Delete temp files
#         clean_list = [f"{working_gdb}\\Temp_FC"]
#         arcpy.management.Delete(clean_list)

#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Rebuild features error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)
#     except arcpy.ExecuteError:
#         arcpy.AddError("ArcPy Error Message: {0}".format(arcpy.GetMessages(2)))


# Mahmud vai's rebuild_features - modified in the next function definition
def rebuild_features_(line_lyr, output_lyr, shape_type, unique_ids, left_field, working_gdb, right_field):
    """ reconstructs polygon geometries from a line feature class that has
    left and right polygon id values """
    # Set environment
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = arcpy.env.scratchGDB
    scratch = arcpy.env.scratchGDB
    try:
        dup_lyr = arcpy.management.MakeFeatureLayer(line_lyr, "dup_lyr")

        # Get information about geometry of output (spatial ref and if has Z)
        spatial_ref = arcpy.da.Describe(output_lyr)['spatialReference']
        has_z = arcpy.da.Describe(output_lyr)['hasZ']

        oid_field = arcpy.da.Describe(output_lyr)['OIDFieldName']
        name = arcpy.da.Describe(output_lyr)['name']

        # Select the generalized lines relating to an input feature
        if len(unique_ids) > 1:
            arcpy.AddMessage("Creating new geometry")
            query = f"{left_field } IN {tuple(unique_ids)}"
            query_p =f"{query} OR {right_field} IN {tuple(unique_ids)}"
        else:
            arcpy.AddMessage("Creating new geometry")
            query = f"{left_field } = {unique_ids[0]}"
            query_p =f"{query} OR {right_field} = {unique_ids[0]}"

        if shape_type == "Polyline":
            arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query)
        if shape_type == "Polygon":
            arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query_p)

            # If a line has the same value for both the left and right ID
            # This means that there is a feature that overlaps the original
            # feature.  These lines and any identical lines need to be
            # removed from the selection.
            if len(unique_ids) > 1:
                remove_query = f"{left_field } IN {tuple(unique_ids)} AND {right_field} IN {tuple(unique_ids)}"
            else:
                remove_query = f"{left_field } = {unique_ids[0]} AND {right_field} = {unique_ids[0]}"
            arcpy.management.SelectLayerByAttribute(dup_lyr, "NEW_SELECTION", remove_query)
            arcpy.management.SelectLayerByLocation(line_lyr, "ARE_IDENTICAL_TO", dup_lyr, None, "REMOVE_FROM_SELECTION")

        # Dissolve the line features to create one or more closed lines
        fields_not_required_for_dislve = ["OBJECTID", "Shape", "created_user", "created_date", "last_edited_user", "last_edited_date", "InLine_FID", "MaxSimpTol", "MinSimpTol",
                                     "Shape_Length", "Shape_Area"] 
        fields_list_selected_fcs = [field.name for field in (arcpy.da.Describe(line_lyr))['fields'] if field.name not in fields_not_required_for_dislve
                               and ("FID_" not in field.name and "LEFT_FID" not in field.name and "RIGHT_FID" not in field.name)]
        temp_fc = arcpy.analysis.PairwiseDissolve(line_lyr, f"{scratch}\\Temp_FC", fields_list_selected_fcs, "", "SINGLE_PART")

        # Create the new polygon or line geometries
        if shape_type == "Polygon":
            #Create a geometry array from the dissolved line
            arcpy.management.FeatureToPolygon(temp_fc, "new_geo")
            arcpy.analysis.PairwiseDissolve("new_geo", "final_new_geo", "", "", "SINGLE_PART")
        elif shape_type == "Polyline":
            arcpy.management.FeatureToLine(temp_fc, "new_geo", attributes = "ATTRIBUTES")
            arcpy.analysis.PairwiseDissolve("new_geo", "final_new_geo", fields_list_selected_fcs, "", "SINGLE_PART")

        # Update the geometries in the output feature class
        if len(unique_ids) > 1:
            query = f"{oid_field } IN {tuple(unique_ids)}"
        else:
            query = f"{oid_field } = {unique_ids[0]}"
        # Create a layer from the output feature class to update
        arcpy.management.MakeFeatureLayer(output_lyr, "output_lyr")
        # Select the features to be updated
        arcpy.management.SelectLayerByAttribute("output_lyr", "NEW_SELECTION", query)
        # Delete the selected features
        arcpy.management.DeleteFeatures("output_lyr")
        # Append the new geometry with attributes to the selected features
        arcpy.management.Append("final_new_geo", "output_lyr", "NO_TEST")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Rebuild features error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def rebuild_features(line_lyr, output_lyr, shape_type, unique_ids, left_field, working_gdb, right_field):
    """ reconstructs polygon geometries from a line feature class that has
    left and right polygon id values """
    # Set environment
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = arcpy.env.scratchGDB
    scratch = arcpy.env.scratchGDB
    # arcpy.AddMessage(f"output_lyr rebld: {output_lyr}")
    try:
        dup_lyr = arcpy.management.MakeFeatureLayer(line_lyr, "dup_lyr")

        # Get information about geometry of output (spatial ref and if has Z)
        spatial_ref = arcpy.da.Describe(output_lyr)['spatialReference']
        has_z = arcpy.da.Describe(output_lyr)['hasZ']

        oid_field = arcpy.da.Describe(output_lyr)['OIDFieldName']
        name = arcpy.da.Describe(output_lyr)['name']

        # Select the generalized lines relating to an input feature
        if len(unique_ids) > 1:
            arcpy.AddMessage("Creating new geometry")
            query = f"{left_field } IN {tuple(unique_ids)}"
            query_p =f"{query} OR {right_field} IN {tuple(unique_ids)}"
        else:
            arcpy.AddMessage("Creating new geometry")
            query = f"{left_field } = {unique_ids[0]}"
            query_p =f"{query} OR {right_field} = {unique_ids[0]}"
        arcpy.AddMessage(f"query: {query}")
        if shape_type == "Polyline":
            arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query)
        if shape_type == "Polygon":
            arcpy.management.SelectLayerByAttribute(line_lyr, "NEW_SELECTION", query_p)

            # If a line has the same value for both the left and right ID
            # This means that there is a feature that overlaps the original
            # feature.  These lines and any identical lines need to be
            # removed from the selection.
            if len(unique_ids) > 1:
                remove_query = f"{left_field } IN {tuple(unique_ids)} AND {right_field} IN {tuple(unique_ids)}"
            else:
                remove_query = f"{left_field } = {unique_ids[0]} AND {right_field} = {unique_ids[0]}"
            arcpy.management.SelectLayerByAttribute(dup_lyr, "NEW_SELECTION", remove_query)
            arcpy.management.SelectLayerByLocation(line_lyr, "ARE_IDENTICAL_TO", dup_lyr, None, "REMOVE_FROM_SELECTION")

        # Dissolve the line features to create one or more closed lines
        fields_not_required_for_dislve = ["OBJECTID", "Shape", "created_user", "created_date", "last_edited_user", "last_edited_date", "InLine_FID", "MaxSimpTol", "MinSimpTol",
                                     "Shape_Length", "Shape_Area"]
        fields_list_selected_fcs = [field.name for field in (arcpy.da.Describe(line_lyr))['fields'] if field.name not in fields_not_required_for_dislve
                               and ("FID_" not in field.name and "LEFT_FID" not in field.name and "RIGHT_FID" not in field.name)]
        temp_fc = arcpy.analysis.PairwiseDissolve(line_lyr, f"{scratch}\\Temp_FC", fields_list_selected_fcs, "", "SINGLE_PART")

         # Create the new polygon or line geometries
        if shape_type == "Polygon":
            #Create a geometry array from the dissolved line
            arcpy.management.FeatureToPolygon(temp_fc, "new_geo")
            arcpy.analysis.PairwiseDissolve("new_geo", "final_new_geo", "", "", "SINGLE_PART")
        elif shape_type == "Polyline":
            arcpy.management.FeatureToLine(temp_fc, "new_geo", attributes = "ATTRIBUTES")
            arcpy.analysis.PairwiseDissolve("new_geo", "final_new_geo", fields_list_selected_fcs, "", "SINGLE_PART")

        # Update the geometries in the output feature class
        if len(unique_ids) > 1:
            query = f"{oid_field } IN {tuple(unique_ids)}"
        else:
            query = f"{oid_field } = {unique_ids[0]}"
        # Create a layer from the output feature class to update
        arcpy.management.MakeFeatureLayer(output_lyr, "output_lyr")
        
        fms = arcpy.FieldMappings()
        fms.addTable(output_lyr)
        # Select the features to be updated
        arcpy.management.SelectLayerByAttribute("output_lyr", "NEW_SELECTION", query)
        arcpy.analysis.SpatialJoin(output_lyr, "final_new_geo" , "TEMP_OUT_SPATIALJOIN", field_mapping=fms)
       
        # arcpy.management.DeleteField("TEMP_OUT_SPATIALJOIN", output_lyr_required_fields, "KEEP_FIELDS")
        # # Delete the selected features
        arcpy.management.DeleteFeatures(output_lyr)
        # # Append the new geometry with attributes to the selected features
        arcpy.management.Append("TEMP_OUT_SPATIALJOIN", output_lyr, "NO_TEST")
        

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Rebuild features error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def get_fcs_as_dict(in_workspace, dataset=""):
    """ gets a list of all feature classes in a database, includes feature
    classes inside and outside of the feature datasets"""
    try:
        fcs = []
        fc_dict = {}

        if dataset != "":
            in_workspace = str(in_workspace + "\\" + dataset)
        walk = arcpy.da.Walk(in_workspace, datatype="FeatureClass")
        for dirpath, dirnames, filenames in walk:
            for filename in filenames:
                fc_class = dirpath + "\\" + filename
                fcs.append(fc_class)
                fc_dict[filename.upper()] = fc_class

        return fc_dict
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Get fcs as dict error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def parse_file(file_path, params):
    arcpy.AddMessage('Hierarchy file parsing.....')
    try:
        # Open the text file
        num_params = len(params)
        hier_file = open(file_path, 'r')

        for line in hier_file.readlines():
            record = line.split(',')
            cnt = 0
            while cnt < num_params:
                params[cnt].append(record[cnt])
                cnt += 1
        return params
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Parse file error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def populate_hierarchy(hierarchy_file, workspace, field_name, working_gdb):
    """ Build a query to find all features with the same name as this record"""
    arcpy.AddMessage("Start Populate Hierarchy")
    try:
        # Set environment
        arcpy.env.overwriteOutput = True
        arcpy.env.workspace = working_gdb

        # Set local variables
        fcs = []
        queries = []
        values = []
        params = [fcs, queries, values]

        fcs, queries, values = parse_file(hierarchy_file, params)
        fc_dict = get_fcs_as_dict(workspace, dataset="")
        cnt = 0
        for fc in fcs:
            fc = fc.upper()
            if fc in fc_dict:
                fc_path = fc_dict[fc]
                sql = queries[cnt]
                val = int(values[cnt])
                selected_fcs = arcpy.management.SelectLayerByAttribute(fc_path, "NEW_SELECTION", sql)
                count = int(arcpy.management.GetCount(selected_fcs).getOutput(0))
                if count >= 1:
                    arcpy.SetProgressorLabel(f'Populating Hierarchy: Updating {str(count)} features with value {str(val)}')
                    # arcpy.AddMessage("Updating " + str(count) + " features with value " + str(val))
                    arcpy.management.CalculateField(fc_path, field_name, val, "PYTHON3")
                cnt += 1
        arcpy.AddMessage("Ended Populating Hierarchy")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Populate hierarchy error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

"""Newly added updated function for hierarchy population"""

def parse_file_new(file_path):
    arcpy.AddMessage("Hierarchy file parsing.....")
    rows = []
    try:
        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            for row_num, row in enumerate(reader, start=1):
                if not row or len(row) < 3:
                    continue
                    
                fc = row[0].strip()
                query = row[1].strip()
                val = row[2].strip()

                if not fc:
                    continue
                rows.append((fc, query, int(val), row_num))
                
        return rows

    except Exception as e:
        tb = traceback.format_exc()
        arcpy.AddMessage(f"Parse file error: {e}\nTraceback details:\n{tb}")
        return []

def populate_hierarchy_new(hierarchy_file, workspace, field_name, working_gdb):
    """ Build a query to find all features with the same name as this record and populate the Hierarchy field"""
    arcpy.AddMessage("Start Populate Hierarchy")
    try:
        # Set environment
        arcpy.env.overwriteOutput = True
        arcpy.env.workspace = working_gdb
        
        rule_set = parse_file_new(hierarchy_file)

        fc_dict = get_fcs_as_dict(workspace, dataset="")

        cnt = 0
        for element in rule_set:
            fc = element[0]
            query = element[1]
            value = element[2]

            fc_upper = fc.upper()
            if fc_upper in fc_dict:
                fc_path = fc_dict[fc_upper]
        
                feature_layer = arcpy.management.MakeFeatureLayer(fc_path, f"{fc_upper}_lyr_{cnt}")
                selected_fcs = arcpy.management.SelectLayerByAttribute(feature_layer, "NEW_SELECTION", query)
                count = int(arcpy.management.GetCount(selected_fcs).getOutput(0))
                arcpy.AddMessage(f"Selected Count: {count}")
                if count >= 1:
                    arcpy.SetProgressorLabel(f'Populating Hierarchy: Updating {str(count)} features with value {str(value)}')
                    arcpy.AddMessage("Updating " + str(count) + " features with value " + str(value))
                    arcpy.management.CalculateField(selected_fcs, field_name, value, "PYTHON3")
                    arcpy.management.SelectLayerByAttribute(feature_layer, "CLEAR_SELECTION")
                cnt += 1
            else:
                arcpy.AddMessage(f"{fc} not found in the GDB")
        arcpy.AddMessage("Ended Populating Hierarchy")
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Populate hierarchy error: {e}\nTraceback details:\n{tb}"
        arcpy.AddMessage(error_message)

"""Newly added updated functions for hierarchy population"""


def flag_loops(input_line, working_gdb, hier_field):
    arcpy.AddMessage('Starting flag loopings.....')
    # Define environment variables
    arcpy.env.overwriteOutput = True

    try:
        mbrs = arcpy.management.MinimumBoundingGeometry(input_line, f"{working_gdb}\\mbrs", "RECTANGLE_BY_AREA")
        mbr_geos = {}
        with arcpy.da.SearchCursor(mbrs, ['ORIG_FID', 'SHAPE@']) as cursor:
            for row in cursor:
                mbr_geos[row[0]] = row[1]

        convex_geos = {}
        convex = arcpy.management.MinimumBoundingGeometry(input_line, f"{working_gdb}\\convex", "CONVEX_HULL")
        with arcpy.da.SearchCursor(convex, ['ORIG_FID', 'SHAPE@']) as cursor:
            for row in cursor:
                convex_geos[row[0]] = row[1]

        arcpy.AddMessage(str(len(mbr_geos)) + " : " + str(len(convex_geos))) 

        with arcpy.da.UpdateCursor(input_line, (['SHAPE@', hier_field, 'OID@'])) as cursor:
            arcpy.SetProgressorLabel(f'Flagging {input_line} temporarily')
            for row in cursor:
                geo = row[0]
                start_pt = geo.firstPoint
                start_geo = arcpy.PointGeometry(start_pt)
                end_pt = geo.lastPoint

                length = geo.length
                dist = start_geo.distanceTo(end_pt)

                if dist == 0:
                    row[1] = 0
                    cursor.updateRow(row)

                elif dist <= (length/5):
                    arcpy.AddMessage("loop")
                    mbr = mbr_geos[row[2]]
                    if row[2] in convex_geos:
                        convex_geo = convex_geos[row[2]]
                        sym_diff = mbr.symmetricDifference(convex_geo)
                        mbr_area = mbr.area
                        sym_area = sym_diff.area
                        diff_area = (mbr_area / sym_area) if sym_area else float("inf")
                        if diff_area < 8:
                            arcpy.AddMessage("flag")
                            row[1] = 0
                            cursor.updateRow(row)
        # Delete temp files
        arcpy.management.Delete([f"{working_gdb}\\mbrs", f"{working_gdb}\\convex"])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Merge parallel powerlines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def delete_dangles(hydro_lyr, dangles, seg_length, compare_fcs, working_gdb):
    # Set environment variable
    arcpy.env.workspace = working_gdb
    try:
        # Use a spatial join to join the dangle points to the
        # hydro feature layer where the Boundary Touches
        # The purpose of this is to denote which lines are dangles
        # so that only dangles get deleted, and not small segments
        # that make up part of a bigger segment that aren't dangles
        arcpy.AddMessage("Creating spatial join between hydro lines and dangle points...")
        hydro_sj = arcpy.analysis.SpatialJoin(hydro_lyr, dangles, "hydro_sj", match_option="BOUNDARY_TOUCHES").getOutput(0)

        # Get TARGET_FID value only where Join_Count > 0
        # (where there is a spatial join, meaning that line
        # is a dangle)
        arcpy.AddMessage("Retrieving dangles shorter than " + str(seg_length) + " Meters...")
        targ_fids = []
        count = 0
        with arcpy.da.SearchCursor(hydro_sj, ["TARGET_FID"], "Join_Count > 0") as cur:
            for row in cur:
                # Store each Target FID value in a list
                targ_fids.append(str(row[0]))
        arcpy.AddMessage(str(len(targ_fids)) + " features found...")
        if len(targ_fids) >= 1:
            # Convert list of Target FID values to a SQL statement
            value_str = ", ".join(str(v) for v in targ_fids)
            where = f"OBJECTID IN ({value_str})"

            # Select hydro layer using where clause of Target FID values
            arcpy.AddMessage("Selecting hydro features to delete...")
            arcpy.management.SelectLayerByAttribute(hydro_lyr, "NEW_SELECTION", where)
            if compare_fcs:
                for fc in compare_fcs:
                    arcpy.management.SelectLayerByLocation(hydro_lyr, "INTERSECT", fc, "", "REMOVE_FROM_SELECTION")

            count = int(arcpy.management.GetCount(hydro_lyr).getOutput(0))
            arcpy.AddMessage(str(count))
            # Delete selected features from hydro layer
            arcpy.AddMessage("Deleting hydro features that are dangles and less than " + str(seg_length) + " Meters...")
            arcpy.management.DeleteFeatures(hydro_lyr)

        return count
    
    except Exception as e:
            tb = traceback.format_exc()
            error_message = f"Delete dangles error: {e}\nTraceback details:\n{tb}"
            arcpy.AddError(error_message)

def process_fc(in_fc, fcs, fc_paths):
    """prep features classes and convert to line if necessary"""
    try:
        desc = arcpy.da.Describe(str(in_fc))
        arcpy.AddMessage(" ... Prepping " + desc['name'])
        arcpy.env.workspace = arcpy.env.scratchGDB
        if int(arcpy.management.GetCount(in_fc)[0]) >= 1:
            if desc['shapeType'] == "Polygon":
                out = desc['name'] + "_temp"
                out_lines = arcpy.management.PolygonToLine(in_fc, out, "IDENTIFY_NEIGHBORS")
                fcs.append(out_lines)
                fc_paths[out] = desc['catalogPath']
                arcpy.AddMessage(f"out lines are: {out_lines}")
            else:
                out = desc['name']
                fcs.append(str(in_fc))
                fc_paths[out] = desc['catalogPath']
                arcpy.AddMessage(desc['catalogPath'])

        return fcs, fc_paths
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Process fc error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def unique_query(output_layer, main_field, left_field, right_field):
    """ build a string of the object IDs for the features that were modified
    and need to be rebuilt"""
    try:
        query_out = ""
        # Determine which features need to be rebuilt
        # select the records that had the geometry updated
        query = main_field + " <> -1 AND " + main_field + " <> 0"
        # Get a list of the left IDS for these records
        unique_ids = [row[0] for row in arcpy.da.SearchCursor(output_layer, [left_field, main_field], query)]
        if right_field:
            # Get a list of the right IDs for these records
            unique_ids.extend([row[0] for row in arcpy.da.SearchCursor(output_layer, [right_field, main_field], query)])
        unique_ids = list(set(unique_ids))

        # Remove 0 and -1 from the list, these represent no features
        if -1 in unique_ids:
            unique_ids.remove(-1)
        if 0 in unique_ids:
            unique_ids.remove(0)
        if None in unique_ids:
            unique_ids.remove(None)

        if len(unique_ids) < 1:
            arcpy.AddWarning("Unable to find any features to update")
            unique_str = "(" + str(unique_ids)
            unique_str =  unique_str.replace("[","")
            unique_str =  unique_str.replace("]","")
            unique_str = unique_str + ")"
            query_out = left_field + " in " + unique_str
            if right_field:
                query_out = query_out + " OR " + right_field + " in " + unique_str

        arcpy.AddMessage("Selecting " + str(len(unique_ids)) + " features")

        return str(query_out), unique_ids
    
    except Exception as e:
            tb = traceback.format_exc()
            error_message = f"Unique query error: {e}\nTraceback details:\n{tb}"
            arcpy.AddError(error_message)

def find_id_fields(name, fields):
    """ determines the count of which field contains the feature class ids"""
    try:
        cnt = 0
        id_cnt = -1
        # Normalize the name: uppercase and replace dots with underscores
        norm_name = name.upper().replace('.', '_')
        # Normalize the base name (portion after any dots)
        base_name = name.split('.')[-1].upper()

        # Determine the fields that store the left and right ID
        for field in fields:
            norm_field = field.name.upper()
            if norm_name in norm_field or base_name in norm_field:
                # The fields proceeding the L and R fields will contain the feature class name
                id_cnt = cnt
            cnt += 1

        return id_cnt
    
    except Exception as e:
            tb = traceback.format_exc()
            error_message = f"Find ID fields error: {e}\nTraceback details:\n{tb}"
            arcpy.AddError(error_message)


# def feature2point_bldg(inFc, point_fc, min_size, delete_input, one_point, unique_field, working_gdb, sql_bldg):
#     # Set the workspace
#     arcpy.env.overwriteOutput = True

#     try:
#         # Use Describe object and get Shape Area field
#         desc = arcpy.da.Describe(inFc)
#         if desc['shapeType'] == 'Polygon':
#             size_field  = desc['areaFieldName']
#             oidField = desc['OIDFieldName']
#             unique_delimit = arcpy.AddFieldDelimiters(inFc, unique_field)
#             selectionCriteria = ''
#             if min_size:
#                 selectionCriteria = f"{size_field} < {min_size}"
#             if sql_bldg:
#                 if selectionCriteria !="":
#                     selectionCriteria = f"{selectionCriteria} AND ({sql_bldg})"
#                 else:
#                     selectionCriteria = sql_bldg

#             arcpy.AddMessage(selectionCriteria)
#             arcpy.management.MakeFeatureLayer(inFc, "SmallFeatures", selectionCriteria)

#             count = int(arcpy.management.GetCount("SmallFeatures").getOutput(0))
#             if count >= 1:
#                 if not one_point:
#                     # Convert polygon to point
#                     arcpy.AddMessage("Converting " + str(count) + " to point")
#                     arcpy.management.FeatureToPoint("SmallFeatures", f"{working_gdb}\\points")
#                     # Append point with output feature
#                     arcpy.AddMessage("Adding point to output feature class")
#                     arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

#                     if arcpy.Exists(f"{working_gdb}\\points"):
#                         arcpy.management.Delete(f"{working_gdb}\\points")
#                     # Delete the features in the polygon feature class
#                     if delete_input:
#                         arcpy.AddMessage( "Deleting features from " + inFc)
#                         arcpy.management.DeleteFeatures("SmallFeatures")
#                         arcpy.management.Delete("SmallFeatures")
#                 else:
#                     convertOIDs = []
#                     deleteOIDs = []
#                     arcpy.AddMessage("Determining which feature to convert")
#                     values = [row[0] for row in arcpy.da.SearchCursor("SmallFeatures", unique_field)]
#                     uniqueValues = set(values)
#                     uniqueValues.discard("")
#                     uniqueValues.discard(" ")
#                     uniqueValues.discard(None)
#                     for val in uniqueValues:
#                         arcpy.AddMessage(val)
#                         val = val.replace("'", "''")
#                         postfix  = f"ORDER BY {size_field} DESC"
#                         whereClause = f"{unique_delimit} = '{val}'"
#                         arcpy.AddMessage(whereClause)
#                         arcpy.management.SelectLayerByAttribute("SmallFeatures", "NEW_SELECTION", whereClause)
#                         oids = [row[0] for row in arcpy.da.SearchCursor("SmallFeatures", ['OID@', size_field, unique_delimit], sql_clause = (None, postfix))]
#                         arcpy.AddMessage("Convert feature oid " + str(oids[0]))
#                         convertOIDs.append(oids[0])
#                         deleteOIDs.append(oids)
#                     arcpy.AddMessage("Converting largest features to points")
#                     convert_layer = arcpy.management.MakeFeatureLayer(inFc, "convert_lyr")
#                     for oid in convertOIDs:
#                         where = oidField + " = " + str(oid)
#                         arcpy.management.SelectLayerByAttribute(convert_layer, "ADD_TO_SELECTION", where)
#                     count = int(arcpy.management.GetCount(convert_layer).getOutput(0))
#                     if count >= 1:
#                         arcpy.management.FeatureToPoint(convert_layer, f"{working_gdb}\\points")
#                         arcpy.AddMessage("Adding point to output feature class")
#                         arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

#                         if arcpy.Exists(f"{working_gdb}\\points"):
#                             arcpy.management.Delete(f"{working_gdb}\\points")

#                     arcpy.AddMessage(f"Converting features with no value in {unique_field}")
#                     where = f"{unique_delimit} IS NULL OR {unique_delimit} = ''"
#                     if sql_bldg:
#                         where = f"{where} AND ({sql_bldg})"
#                     arcpy.AddMessage(where)
#                     arcpy.management.SelectLayerByAttribute(convert_layer, "NEW_SELECTION", where)

#                     arcpy.AddMessage(where)
#                     count = int(arcpy.management.GetCount(convert_layer).getOutput(0))
#                     if count >= 1:
#                         arcpy.management.FeatureToPoint(convert_layer, f"{working_gdb}\\points")
#                         arcpy.AddMessage("Adding point to output feature class")
#                         arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

#                         if arcpy.Exists(f"{working_gdb}\\points"):
#                             arcpy.management.Delete(f"{working_gdb}\\points")
#                     if delete_input:
#                         arcpy.management.SelectLayerByAttribute("SmallFeatures", "CLEAR_SELECTION")
#                         arcpy.AddMessage( "deleting features from " + inFc)
#                         arcpy.management.DeleteFeatures("SmallFeatures")
#                         arcpy.management.Delete("SmallFeatures")
#             else:
#                 arcpy.AddMessage("No features meet criteria to be converted to point.")

#             clean_list = ["convert_lyr", f"{working_gdb}\\points", "SmallFeatures"]
#             # Delete temp files
#             arcpy.management.Delete(clean_list)

#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Feature to point for building error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)

def merge_parallel_roads(line_fc, Where_clause, Merge_Field, Merge_Distance, update, Change_road_type, working_gdb):        
    # Set environment variables
    arcpy.env.overwriteOutput = True
    try:
        if update:
            if Change_road_type and ":" in Change_road_type:
                splitvals = Change_road_type.split(":")
                val = str(splitvals[0])
                Change_road_type = val.strip()

        # Outputs:
        split = f"{working_gdb}\\splitroads"
        unsplit = f"{working_gdb}\\unsplitout"
        mergeOut = f"{working_gdb}\\mergeout"
        # Field Name
        field_name = "MDR_Type"

        Merge_crit = str(Merge_Distance) + " Meters"

        road_lyr = arcpy.management.MakeFeatureLayer(line_fc, "roadlyr", Where_clause)
        count1 = int(arcpy.management.GetCount(road_lyr).getOutput(0))
        null_query = str(Merge_Field) + " IS NOT NULL"
        arcpy.management.SelectLayerByAttribute(road_lyr, "NEW_SELECTION" , null_query)
        ##count = int(arcpy.management.GetCount(road_lyr).getOutput(0))

        if not has_features(road_lyr):
            arcpy.AddWarning("Some features have Null values in the " + Merge_Field + " field.  These features will not be processed.")

        if has_features(road_lyr):
            # Split Line At Vertices - split to avoid merging entire roadways, only want segments
            arcpy.AddMessage("Splitting lines at vertices...")
            arcpy.management.SplitLine(road_lyr, split)

            #Add MDR field to query on
            arcpy.AddMessage("Adding MDR field...")
            arcpy.management.AddField(split, field_name, 'LONG', '#', '#', '#', '#', 'NULLABLE', 'NON_REQUIRED', '#')



            # Merge Divided Roads - merges based on a provided distance
            arcpy.AddMessage("Merge divided roads...")
            arcpy.cartography.MergeDividedRoads(split, Merge_Field, Merge_crit, mergeOut, "")
            arcpy.management.RepairGeometry(mergeOut)
            mergelyr = arcpy.management.MakeFeatureLayer(mergeOut, "mergelayer", "", "", "")



            #Selet the features in the merge that represent new geometry and add to split features
            arcpy.management.SelectLayerByAttribute(mergelyr, "NEW_SELECTION", "MDR_Type = 1")
            merge_cnt = int(arcpy.management.GetCount(mergelyr).getOutput(0))
            if merge_cnt >= 1:
                # Calculate the new type of transportation
                if update:
                    arcpy.AddMessage("Calculate field to the new type...")
                    arcpy.management.CalculateField(mergelyr, Merge_Field, Change_road_type, 'VB', '#')


                #Unsplit the features
                arcpy.AddMessage("Dissolving lines at vertices...")

                arcpy.management.UnsplitLine(mergeOut, unsplit, [Merge_Field])
                arcpy.management.RepairGeometry(mergeOut)
                #Unsplit drops all the attributes, so replace the geometry of the
                # features in the split layer with the merged geometry.
                with arcpy.da.SearchCursor(unsplit, ['oid@', 'SHAPE@']) as cursor:
                    for row in cursor:
                        geo = row[1]
                        arcpy.management.SelectLayerByLocation(mergelyr, "WITHIN", geo, "", "NEW_SELECTION")
                        cnt = 0
                        with arcpy.da.UpdateCursor(mergelyr, ['oid@', 'SHAPE@']) as up_cursor:
                            for up_row in up_cursor:
                                if cnt == 0:
                                    #replace the geometry for the first record
                                    up_row[1] = geo
                                    up_cursor.updateRow(up_row)
                                    cnt += 1
                                else:
                                    #delete the other rows
                                    up_cursor.deleteRow()



                #Compare the results of the unsplit to the original feature class
                arcpy.AddMessage("Finding features in input that will be replaced")
                updategeo = [row[0] for row in arcpy.da.SearchCursor(mergeOut, 'SHAPE@')]
                orig_geos = [row[0] for row in arcpy.da.SearchCursor(road_lyr, 'SHAPE@')]
                ids = []
                with arcpy.da.UpdateCursor(road_lyr, ['oid@', 'SHAPE@']) as cursor:
                    for row in cursor:
                        geo = row[1]
                        match = False
                        cnt = 0
                        for geom in updategeo:
                            if geom and geo:
                                print(geom)
                                print(geo)
                                if geo.equals(geom):
                                    match = True

                        if not match:
                            arcpy.AddMessage(str(row[0]) + " no match deleteing")
                            cursor.deleteRow()

                arcpy.AddMessage("Finding merged features to add to input feature class")

                with arcpy.da.SearchCursor(mergeOut, ['oid@', 'SHAPE@']) as cursor:
                    for row in cursor:
                        geo = row[1]
                        match = False
                        cnt = 0
                        for geom in orig_geos:
                            if geom and geo:
                                if geo.equals(geom):
                                    match = True

                        if not match:
                            ids.append(str(row[0]))

                if len(ids) >= 1:
                    where = "OBJECTID = "
                    where += " OR OBJECTID = ".join(ids)
                    arcpy.AddMessage(where)

                    arcpy.management.SelectLayerByAttribute(mergelyr, "NEW_SELECTION", where)
                    count = int(arcpy.management.GetCount(mergelyr).getOutput(0))
                    if count >= 1:
                        arcpy.AddMessage("Adding " + str(count) + " merged features to input.")
                        arcpy.management.Append(mergelyr, road_lyr, "NO_TEST")
            else:
                arcpy.AddMessage("No features were close enough to be merged.")
        else:
            arcpy.AddMessage("No features to merge.")
        # Clean up the merge output
        arcpy.management.RepairGeometry(line_fc)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Merge parallel roads error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def get_fcs_load_data(in_workspace, wksp_type):
    try:
        # Set dict and list
        fcs_dict = {}
        fc_name_list = []

        # Set environment variables
        arcpy.env.workspace = in_workspace
        # Get dataset name
        dataset_name = ''
        datasets = arcpy.ListDatasets(dataset_name)
        if len(datasets) == 1:
            dataset_name = datasets[0]
        else:
            arcpy.AddMessage("No dataset is applicable")
        # Get feature classes name
        fc_classes = arcpy.ListFeatureClasses("", "")

        for fc in fc_classes:
            desc = arcpy.da.Describe(fc)
            if wksp_type == "RemoteDatabase":
                fullname = arcpy.ParseTableName(fc, in_workspace)
                database, owner, featureclass = fullname.split(",")
                fcname = featureclass.strip()
                fc_name_list.append(fcname)
                fc_path = os.path.join(in_workspace, fc)
                fcs_dict[fcname] = fc_path
            else:
                fcname = desc['name']
                fc_name_list.append(fcname)
                fc_path = os.path.join(in_workspace, fc)
                fcs_dict[fcname] = fc_path
        return fc_name_list, fcs_dict
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Get fcs load data error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def split_fcs_load_data(in_workspace):
    try:
        split_table = in_workspace + "\\Split_features"
        split_list = []
        if arcpy.Exists(split_table):
            split_list = [s_row[0] for s_row in arcpy.da.SearchCursor(split_table, "feature_classes")]

        return split_list
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Split fcs load data error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def prepFcs(detec_conflict_fc_list, database_path, map_name, symbology_file_path, query='', symbology=''):
    try:
        arcpy.env.workspace = "memory"
        layer_list = []
        # Get current map
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]
        all_layers = maps.listLayers()
      
        
        detec_conflict_fc_list =  [lyr for lyr in all_layers for fc in detec_conflict_fc_list if lyr.name in fc and not lyr.isGroupLayer ]
        for lyr in detec_conflict_fc_list:
            arcpy.AddMessage(f"Prepping: {lyr.name}")

        return detec_conflict_fc_list

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Prep fcs error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def write2Rev(conflict_fc, rev_workspace, rev_session, severity):
    try:
        arcpy.env.workspace = "memory"

        Fields = arcpy.ListFields(conflict_fc)
        count = 1
        # Follow original: take the first two FID_* fields as-is
        for field in Fields:
            if "FID_" in field.name:
                if count == 1:
                    inField = field.name
                    inFC = inField.replace("FID_", "")
                    count += 1
                elif count == 2:
                    outField = field.name
                    outFC = outField.replace("FID_", "")
                    outFC = outFC.replace("_1", "")

        # Write features to Reviewer (keep original message text/spacing)
        arcpy.AddMessage("Writing conficts to Reviewer Table")
        review_status = "Graphic conflict with " + str(outFC)
        arcpy.reviewer.WriteToReviewerTable(rev_workspace, rev_session, conflict_fc, inField, inFC, review_status, "", "", severity)
        # Echo GP tool messages
        arcpy.AddMessage(arcpy.GetMessages())
        return count

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Write to rev error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def hide_blgs_under_built_up_area(fc_list, build_up_area_fcs, express_val_mx, express_val_mn, field_name, search_distance, query, map_name = None):
    dynamic_fc_names = resolve_lyr()
    try:
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        active_map = aprx.activeMap
        if(map_name):
            active_map = aprx.listMaps(map_name)[0]
        fc_layers = active_map.listLayers()
        # Get feature classes
        build_up_area_fcs = list(filter(str.strip, build_up_area_fcs))
        build_up_area_fcs = [fc for built in build_up_area_fcs for fc in fc_list if str(built) in fc and str(built) not in {dynamic_fc_names.Town_Built_up_A, dynamic_fc_names.Generalised_Buildings_A}]
        b_town_built_up_A = [fc for fc in fc_layers if dynamic_fc_names.Town_Built_up_A in fc.name][0]
        # Get feature class and make feature layer
        # b_town_built_up_A = arcpy.management.MakeFeatureLayer(b_town_built_up_A, "b_town_built_up_A")
        # b_town_built_up_A = maps.listLayers("BJ0073_Town_Built_up_A")
        b_generalised_buildings_A = [fc for fc in fc_layers if dynamic_fc_names.Generalised_Buildings_A in fc.name][0]
        # b_generalised_buildings_A = arcpy.management.MakeFeatureLayer(b_generalised_buildings_A, "BJ0500_Generalised_Buildings_A")
        # b_generalised_buildings_A = maps.listLayers( "BJ0500_Generalised_Buildings_A")
        b_Buildings_P = [fc for fc in fc_list for key in [dynamic_fc_names.Residential_Building_P,
                                                          dynamic_fc_names.Industrial_Building_P,
                                                          dynamic_fc_names.Educational_Building_P,
                                                          dynamic_fc_names.Building_Of_Worship_P] if key in fc]
        for b_Building_fc in b_Buildings_P:
            # layer_name = arcpy.da.Describe(b_Building_fc)['name']
            #b_Buildings_P_lyr = arcpy.management.MakeFeatureLayer(b_Building_fc, layer_name)
            b_Buildings_P_lyr = [fc for fc in fc_layers if os.path.basename(b_Building_fc) in fc.name][0]
            # Get all field names from the current feature class
            all_fields = [f.name for f in arcpy.ListFields(b_Building_fc)]

            # Extract the field from the query string
            query_field = query.split()[0]

            if query_field not in all_fields:
                continue

            # Feature selection for B_Buildings_P
            selected_fcs_gen = arcpy.management.SelectLayerByAttribute(b_Buildings_P_lyr, 'NEW_SELECTION', query)

            # Calculate Field
            arcpy.management.CalculateField(in_table=b_Buildings_P_lyr, field=field_name, expression=express_val_mn, expression_type='PYTHON3')
        # arcpy.AddMessage(f"search_distance: {search_distance} and b_town_built_up_A: {b_town_built_up_A}")
        if has_features(b_town_built_up_A) and has_features(b_generalised_buildings_A):
            # Merge both reference layers once, so each building class needs a
            # single select + calculate instead of one round per reference layer
            arcpy.management.SelectLayerByAttribute(b_town_built_up_A, "CLEAR_SELECTION")
            arcpy.management.SelectLayerByAttribute(b_generalised_buildings_A, "CLEAR_SELECTION")
            hide_ref_merged = r"memory\hide_ref_merged"
            arcpy.management.Merge([b_town_built_up_A, b_generalised_buildings_A], hide_ref_merged)
            for fc in build_up_area_fcs:
                if has_features(fc):
                    fc_name = arcpy.da.Describe(fc)["name"]
                    arcpy.AddMessage(f'Processing: "{fc_name}" for hiding buildings under built-up area')
                    matches = [fc for fc in fc_layers if fc.name in fc_name and not fc.isGroupLayer]
                    if matches:
                        fc_lyr = matches[0]
                    else:
                        arcpy.AddWarning(f'Layer: "{fc_name}" is in the GDB, but Could not find a layer for "{fc_name}" in the Map Layers. Skipping this feature class.')
                        continue
                    # Feature selection against merged built-up references
                    arcpy.management.SelectLayerByLocation(fc_lyr, 'INTERSECT', hide_ref_merged, search_distance, 'NEW_SELECTION')
                    # Calculate Field
                    arcpy.management.CalculateField(in_table=fc_lyr, field=field_name, expression=express_val_mx, expression_type='PYTHON3')
                    arcpy.management.SelectLayerByAttribute(fc_lyr, "CLEAR_SELECTION")
            arcpy.management.Delete(hide_ref_merged)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Hide buildings under built-up area error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def apply_symbology(feature_layer, symbology_field_in, symbology_file_path, map_name, fc_name):
    try:
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]
        # Add layers into map
        lyr = maps.addDataFromPath(feature_layer)
        # Getting layrx files
        lyrx = [k for k in glob.glob(os.path.join(symbology_file_path, "*.lyrx"))]
        # Applying symbology
        for symbology_layer in lyrx:
            if fc_name in symbology_layer:
                    symbology_layerx = maps.addDataFromPath(symbology_layer)
                    in_symbology = symbology_layerx.symbology
                    lyr.symbology = in_symbology
                    maps.removeLayer(symbology_layerx)
        # Save the project
        aprx.save()
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Apply symbology error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def resolve_conflicts_points_polygon(fc_list, input_building_layers, input_barrier_layers, bb_lyr_ex, bb_lyr_ex_his, hierarchy_field, invisibility_field, symbology_file_path, ref_scale, 
                                     minimum_size, bld_gap, working_gdb, map_name):
    try:
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]
        fc_layers = maps.listLayers()
        
        #Clear selection from all layers
        for lyr in fc_layers:
            if lyr.isFeatureLayer and not lyr.isGroupLayer:
                try:
                    arcpy.management.SelectLayerByAttribute(lyr, "CLEAR_SELECTION")
                except arcgisscripting.ExecuteError as e:
                    arcpy.AddWarning(f"Warning: could not clear selection from '{lyr.name}' - {e}. --> Check this layer in the map whether the datasorce is broken or any other mismatch for this layer in both GDB and Map.")
                    continue
        arcpy.AddMessage("Cleared selection from all valid layers")

        # Set Environment
        arcpy.env.overwriteOutput = 1
        arcpy.env.workspace = working_gdb
        # Get feature classes
        input_building_layers = list(filter(str.strip, input_building_layers))
        input_building_layers = [fc for bld_lyr in input_building_layers for fc in fc_layers if str(bld_lyr) in fc.name]
        input_barrier_layers = list(filter(str.strip, input_barrier_layers))
        input_barrier_layers = [fc for bar_lyr in input_barrier_layers for fc in fc_layers if str(bar_lyr) in fc.name]
        
        # Set bolean
        bolean = "false"
        building_list = []
        barrier_list = []

        # Make barrier feature list
        for br_lyr in input_barrier_layers:
            barrier_list.append(br_lyr.name)
       
        # Make building feature list
        for b_lyr in input_building_layers:
            building_list.append(b_lyr.name)

        # Get the map layers. Only real feature layers with a working data
        # source can be queried - opening a cursor on a broken layer fails
        # with SystemError, so filter those out and report them instead.
        wanted_names = set(building_list) | set(barrier_list)
        valid_layers = []
        for lyr in fc_layers:
            if not lyr.isFeatureLayer or lyr.isGroupLayer or str(lyr.name) not in wanted_names:
                continue
            if lyr.isBroken:
                arcpy.AddWarning(f"Layer '{lyr.name}' has a broken data source and will be skipped. Repair the layer in map '{map_name}' so its features are included in conflict resolution.")
                continue
            valid_layers.append(lyr)
        fc_building_layers = [lyr for lyr in valid_layers if str(lyr.name) in building_list and has_features(lyr)]
        fc_barrier_layers = [lyr for lyr in valid_layers if str(lyr.name) in barrier_list and has_features(lyr)]
        in_barriers = [[com_layer, bolean, f"{bld_gap} Meters"] for com_layer in fc_barrier_layers]

        # Set the reference scale
        arcpy.env.referenceScale = ref_scale
        arcpy.AddMessage(f".....Reference Scale set to: {ref_scale}")
        
        # Execute Resolve Building Conflicts
        if len(fc_building_layers) > 0:
            # Only feed buildings that are still visible: features already hidden
            # (invisibility = 1, e.g. under built-up areas) cannot conflict with
            # anything, and excluding them shrinks the tool's working set.
            for b_lyr in fc_building_layers:
                if invisibility_field in [f.name for f in arcpy.ListFields(b_lyr)]:
                    arcpy.management.SelectLayerByAttribute(b_lyr, "NEW_SELECTION", f"{invisibility_field} <> 1 OR {invisibility_field} IS NULL")
            arcpy.AddMessage(".....Starting Resolve Building Conflict")
            arcpy.cartography.ResolveBuildingConflicts(fc_building_layers, invisibility_field, in_barriers, f"{bld_gap} Meters", f"{minimum_size} Meters", hierarchy_field)
            arcpy.AddMessage(".....Completed Resolve Building Conflict")
            for b_lyr in fc_building_layers:
                arcpy.management.SelectLayerByAttribute(b_lyr, "CLEAR_SELECTION")
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Resolve conflicts for point and polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def sort_layers_keep_top_group_order(m, layers):
    """
    Keep TOP-LEVEL group order as in TOC,
    but sort all layers within each top-level group alphabetically (ignore subgroups).
    """

    def top_group_key(lyr):
        parts = str(lyr.longName).split("\\")
        # If inside a group, parts[0] is the top-level group name.
        # If not in any group, treat as root group "".
        return parts[0] if len(parts) > 1 else ""

    # Determine top-level group order from the TOC (first appearance)
    group_rank = {}
    r = 0
    for lyr in m.listLayers():
        g = top_group_key(lyr)
        if g not in group_rank:
            group_rank[g] = r
            r += 1

    # Sort: top-level group order first, then layer name
    return sorted(
        layers,
        key=lambda lyr: (
            group_rank.get(top_group_key(lyr), 10**9),
            str(lyr.name).lower(),
        ),
    )

def align_points(point_fcs, align_fcs, distance, orient, ref_scale, hierarchy_field, symbology_file_path, orient_f, working_gdb, map_name):  #changed 2026-01-27
    try:
        # Set Environment
        arcpy.env.workspace = working_gdb 

        # Set the reference scale to 1:50,000
        arcpy.env.referenceScale = ref_scale
        # Set spatial reference
        sr = arcpy.da.Describe(point_fcs[0])['spatialReference']
        arcpy.env.cartographicCoordinateSystem = sr

        points_fc_list = []
        align_fcs_list = []

        # Create layers list for all input feature classes
        arcpy.AddMessage("Creating layers for points")
        for Point_fc in point_fcs:
            fc_name = arcpy.da.Describe(Point_fc)['name']
            points_fc_list.append(fc_name)

        # Create layers list for all allign feature classes
        arcpy.AddMessage("Creating layers for lines and polys")
        for align_fc in align_fcs:
            fc_name = arcpy.da.Describe(align_fc)['name']
            align_fcs_list.append(fc_name)
        
        # Get the map layers
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]

        # Get the feature layer
        fc_layers = maps.listLayers()
        fc_layers = make_unique_layers(fc_layers, map_name)
        pt_lyrs = [lyr for lyr in fc_layers for fc in points_fc_list if str(lyr.name) == fc]      
        align_lyrs = [lyr for lyr in fc_layers for fc in align_fcs_list if str(lyr.name) == fc]

        # --- NEW: sort the align lyrs lists alphabetically by layer name (case-insensitive) ---
        sorted_align_lyrs = sort_layers_keep_top_group_order(maps, align_lyrs)
        # Skip empty align layers: aligning against nothing still costs a full tool run
        sorted_align_lyrs = [lyr for lyr in sorted_align_lyrs if has_features(lyr)]

        for algn_lyr in sorted_align_lyrs:
            arcpy.AddMessage("Align layers to be used: " + str(algn_lyr.name))
        # --------------------------------------------------------------------------

        if len(pt_lyrs) >= 1 and len(sorted_align_lyrs) >= 1:
            for pt_lyr in pt_lyrs:
                if not has_features(pt_lyr):
                    arcpy.AddMessage("Skipping " + str(pt_lyr) + " - no features to align")
                    continue
                arcpy.management.CalculateField(pt_lyr, f"{orient_f}", "None", "PYTHON3")
                for align_lyr in sorted_align_lyrs:
                    arcpy.management.SelectLayerByAttribute(pt_lyr, "NEW_SELECTION", f"{orient_f} IS NULL") 
                    arcpy.AddMessage("Aligning " + str(pt_lyr) + " to " + str(align_lyr))
                    arcpy.cartography.AlignMarkerToStrokeOrFill(pt_lyr, align_lyr, f"{distance} Meters", orient)
                    arcpy.AddMessage("Completed aligning " + str(pt_lyr) + " to " + str(align_lyr))
                arcpy.management.SelectLayerByAttribute(pt_lyr, "CLEAR_SELECTION")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Align points error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def convert_polygon(in_p, secondary_list, minimum_area, in_prim_sql, working_gdb):
    '''
    Deletes all features smaller than the minimum size. If the feature to be deleted
    touches one of the compare feature classes, the geometry of the delete feature
    will be added to the feature it touches.
    '''
    # Set the workspace
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    try:
        input_layer = 0
        desc = arcpy.da.Describe(in_p)
        fc_name = desc['name']
        secondary_layers = []
        input_primary_lyr = arcpy.management.MakeFeatureLayer(in_p, f"{fc_name}_primary_lyr", in_prim_sql)
        all_input = [input_primary_lyr]
        secondary_names = []
        for value in secondary_list:
            value = value.strip("'")
            desc = arcpy.da.Describe(value)
            name = desc['name']
            secondary_names.append(name)
            value = arcpy.management.MakeFeatureLayer(value, f"{name}_secondary_lyr")
            secondary_layers.append(str(value).strip("\'"))
            all_input.append(str(value).strip("\'"))

        in_wksp = desc['path']

        # Create feature layer from the input
        input_layer = arcpy.management.MakeFeatureLayer(in_p, "input_layer")
        # Create for common thing for input data
        if minimum_area <= 0:
            arcpy.AddError("Minimum area must be above 0.")
            return
        # Use Describe object and get Shape Area field
        area_field = arcpy.da.Describe(in_p)['areaFieldName']
        query = f"{area_field} >= {minimum_area}"
        arcpy.AddMessage(f"Applying minimum area filter with query: {query} for layer: {input_layer}")

        # Apply selection based on the minimum area
        arcpy.management.SelectLayerByAttribute(input_layer, "NEW_SELECTION", query)

        if int(arcpy.management.GetCount(input_layer)[0]) >= 1:
            # Find areas from input that are not in secondary.
            # Classic Union honors parallelProcessingFactor and its parallel
            # overlay engine intermittently fails with "The table was not
            # found" - run this call single-threaded with an explicit output.
            all_unioned = os.path.join(working_gdb, "all_unioned")
            with arcpy.EnvManager(parallelProcessingFactor="0"):
                all_union = arcpy.analysis.Union(all_input, all_unioned, "ONLY_FID")
            union_single = in_wksp + "\\union_single"
            arcpy.management.MultipartToSinglepart(all_union, union_single)
            arcpy.AddMessage(str(union_single))

            query = ""
            for name in secondary_names:
                query += ('FID_' + name + " = -1 AND ")
            query = query[:-5]
            copy_lyr = arcpy.management.MakeFeatureLayer(union_single, "copy_lyr", query)
            # Convert overlapping features
            if count_features(copy_lyr) >= 1:
                arcpy.AddMessage("Looking for overlapping features to convert. "
                                    + str(count_features(copy_lyr))
                                    + " features to be processed...")
                arcpy.topographic.EliminatePolygon(copy_lyr, secondary_layers, minimum_area)
            else:
                arcpy.AddMessage("No features to Convert")

            arcpy.management.Delete([union_single, all_unioned])
        else:
            arcpy.AddMessage("No features meet the minimum area requirement.")
        # Delete temp files
        clean_list = [input_layer]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Convert polygons error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        # Re-raise so the theme stops instead of continuing with the
        # vegetation conversion silently skipped.
        raise

def erase_features(input_primary, input_secondary, working_gdb, max_gap_area, fill_option, invisibility_field):
    try:
        arcpy.env.overwriteOutput = True
        input_primary = [fc for fc in input_primary if has_features(fc)]
        input_secondary = [fc for fc in input_secondary if has_features(fc)]
        if not input_primary or not input_secondary:
            arcpy.AddMessage("Erase features: no primary or secondary features to process")
            return

        for in_pri in input_primary:
            arcpy.management.RepairGeometry(in_pri, "DELETE_NULL", "ESRI")
            # Calculate Field
            if "HH0020_Lake_A" in in_pri or "HH0210_Pond_A" in in_pri:
                arcpy.management.CalculateField(in_table=in_pri, field=invisibility_field, expression=0, expression_type='PYTHON3')

        # Merge all primaries into one erase mask, so each secondary is erased
        # and rewritten once instead of once per primary (erasing by A then by B
        # is the same as erasing by A union B).
        merged_primary = os.path.join(working_gdb, "erase_mask_merged")
        erase_mask = os.path.join(working_gdb, "erase_mask")
        arcpy.management.Merge(input_primary, merged_primary)
        arcpy.analysis.PairwiseDissolve(merged_primary, erase_mask, multi_part="MULTI_PART")

        veg_erase = os.path.join(working_gdb, "veg_erase")
        for in_sec in input_secondary:
            arcpy.management.RepairGeometry(in_sec, "DELETE_NULL", "ESRI")
            arcpy.analysis.PairwiseErase(in_sec, erase_mask, veg_erase)
            if count_features(veg_erase) > 0:
                # Replace the secondary features with their erased versions
                arcpy.management.DeleteFeatures(in_sec)
                arcpy.management.Append(veg_erase, in_sec, "NO_TEST")

        # Clean up temps before FillGaps so they are removed even if it errors
        arcpy.management.Delete([merged_primary, erase_mask, veg_erase])

        # Fill gaps between each water primary and the processed secondaries
        for water_key in ("HH0020_Lake_A", "HH0210_Pond_A", "HH0042_River_Coverage_A"):
            group_primaries = [fc for fc in input_primary if water_key in fc]
            if group_primaries:
                fill_gaps_fcs = list(set(group_primaries + input_secondary))
                arcpy.topographic.FillGaps(fill_gaps_fcs, max_gap_area, fill_option)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Erase features error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        # Re-raise so the theme and the log record the failure instead of
        # reporting a successful run with the gap filling silently skipped.
        raise


def determine(input_lines, input_polygon, out_table, line_field, poly_field, working_gdb):
    try:
        poly_lyr = arcpy.management.MakeFeatureLayer(input_polygon, "poly_lyr")
        line_lyr = arcpy.management.MakeFeatureLayer(input_lines, "line_lyr")

        arcpy.management.Integrate([poly_lyr, line_lyr])

        node_field = "Node_end"

        # Add fields for storing the OIDs of each input feature class
        fields = arcpy.ListFields(out_table)

        names = []
        for field in fields:
            names.append(field.name)

        if not line_field in names:

            arcpy.management.AddField(out_table, line_field, "LONG")

        if not poly_field in names:
            arcpy.management.AddField(out_table, poly_field, "LONG")

        if not node_field in names:
            arcpy.management.AddField(out_table, node_field, "Text")

        # Select just those lines that intersect polygons
        arcpy.management.SelectLayerByLocation(line_lyr, "INTERSECT", poly_lyr)
        arcpy.management.SelectLayerByLocation(line_lyr, "WITHIN", poly_lyr, "", "REMOVE_FROM_SELECTION")

        # if at least on line touches one polygon
        # loop through all the line features
        if int(arcpy.management.GetCount(line_lyr)[0]) >= 1:
            near_tab = arcpy.analysis.GenerateNearTable(line_lyr, poly_lyr, f"{working_gdb}\\line_near_poly", "0 Meters", closest="ALL")
            line_to_poly = {}
            line_ids = []
            poly_ids = []
            with arcpy.da.SearchCursor(near_tab, ['IN_FID', 'NEAR_FID']) as n_cur:
                for n_row in n_cur:
                    if n_row[0] in line_ids:
                        cur_ids = line_to_poly[n_row[0]]
                        cur_ids.append(n_row[1])
                        line_to_poly[n_row[0]] = cur_ids
                        poly_ids.append(n_row[1])
                    else:
                        line_ids.append(n_row[0])
                        line_to_poly[n_row[0]] = [n_row[1]]
                        poly_ids.append(n_row[1])

            poly_ids = set(poly_ids)
            poly_geos = {}

            with arcpy.da.SearchCursor(poly_lyr, ['oid@', 'SHAPE@']) as s_cur:
                for s_row in s_cur:
                    if s_row[0] in poly_ids:
                        poly_geos[s_row[0]] = s_row[1]

            # Open an insert cursor
            i_cursor = arcpy.da.InsertCursor(out_table, [line_field, poly_field, node_field])
            with arcpy.da.SearchCursor(line_lyr, ['oid@', 'SHAPE@']) as cursor:
                for row in cursor:
                    geo = row[1]
                    line_id = row[0]
                    if line_id in line_ids:
                        poly_touches = line_to_poly[line_id]
                        start_pt = geo.firstPoint
                        end_pt = geo.lastPoint
                        for touch_id in poly_touches:
                            poly_geo = poly_geos[touch_id]
                            if not poly_geo.disjoint(start_pt):
                                #... add a record to the table
                                arcpy.AddMessage("Line feature " + str(row[0]) + " touches")
                                new_row = (row[0], touch_id, "start")
                                i_cursor.insertRow(new_row)
                            elif not poly_geo.disjoint(end_pt):
                                #... add a record to the table
                                arcpy.AddMessage("Line feature " + str(row[0]) + " touches")
                                new_row = (row[0], touch_id, "end")
                                i_cursor.insertRow(new_row)
            del i_cursor
            
            # Delete temp file
            arcpy.management.Delete([near_tab])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Detemine error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def reconnect_touching(input_polygon, input_lines, out_table, delete):
    # Set environment
    arcpy.env.overwriteOutput = True
    try:
        desc = arcpy.da.Describe(input_polygon)
        poly_name = desc['name']
        arcpy.AddMessage(poly_name)
        line_name = arcpy.da.Describe(input_lines)['name']
        # Check the fields in output table to make sure they match the inputs
        l_match = ""
        p_match = ""
        fields = arcpy.ListFields(out_table)
        for field in fields:
            if line_name in field.name:
                l_match = field.name
                arcpy.AddMessage(l_match)
            if poly_name in field.name:
                p_match = field.name
                arcpy.AddMessage(p_match)

        if l_match != '' or p_match != '':
            # Get a list of the lines that are supposed to be connected
            match_dict = {}
            values = []
            poly_ids = []
            with arcpy.da.SearchCursor(out_table, [l_match, p_match]) as cursor:
                for row in cursor:
                    if row[0] not in values:
                        values.append(str(row[0]))
                        match_dict[row[0]] = [row[1]]
                        poly_ids.append(row[1])
                    else:
                        cur_dict = match_dict[row[0]]
                        cur_dict.append(row[1])
                        match_dict[row[0]] = cur_dict
                        poly_ids.append(row[1])
            unique_values = set(values)
            poly_ids = set(poly_ids)

            arcpy.AddMessage(str(len(unique_values)) + " lines to test")
            # Loop through the lines
            if len(unique_values) >= 1:
                where = "OBJECTID = "
                where += " OR OBJECTID = ".join(unique_values)
                line_lyr = arcpy.management.MakeFeatureLayer(input_lines, "in_line_lyr", where)
                arcpy.management.SelectLayerByLocation(line_lyr, "INTERSECT", input_polygon, invert_spatial_relationship="INVERT")

                if int(arcpy.management.GetCount(line_lyr)[0]) >= 1:
                    poly_geos = {}
                    with arcpy.da.SearchCursor(input_polygon, ['oid@', 'shape@']) as cursor:
                        for row in cursor:
                            if row[0] in poly_ids:
                                poly_geos[row[0]] = row[1]

                    with arcpy.da.UpdateCursor(input_lines, ['oid@', 'SHAPE@'], where) as u_cur:
                        for u_row in u_cur:
                            line_geo = u_row[1]
                            # Get a list of the polygon features this feature should be connected to
                            unique_polys = match_dict[u_row[0]]
                            for touch_poly in unique_polys:
                                if touch_poly in poly_geos:
                                    poly = poly_geos[touch_poly]
                                    if line_geo.disjoint(poly):
                                        arcpy.AddMessage("Reconnecting line " + str(u_row[0]))
                                        line = poly.boundary()

                                        # Determine if point should be added to beginning or end of the line
                                        start_pt = line_geo.firstPoint
                                        end_pt = line_geo.lastPoint

                                        start_tup = line.queryPointAndDistance(start_pt)
                                        end_tup = line.queryPointAndDistance(end_pt)

                                        array = arcpy.Array()
                                        if start_tup[2] <= end_tup[2]:
                                            # Add the point to the beginning of the line
                                            point = start_tup[0]
                                            array.add(point.centroid)
                                            for part in line_geo:
                                                for pnt in part:
                                                    array.add(pnt)
                                                break
                                        else:
                                            # Add the point to the end of the line
                                            for part in line_geo:
                                                point = end_tup[0]
                                                for pnt in part:
                                                    array.add(pnt)
                                                break
                                            array.add(point.centroid)
                                        # Create a line
                                        polyline = arcpy.Polyline(array)
                                        # Update the geometry of the row
                                        u_row[1] = polyline
                                        u_cur.updateRow(u_row)
            if delete == 'TRUE':
                arcpy.management.Delete(out_table)
        else:
            arcpy.AddError("Specified output table does not contain expected fields.")
            if not l_match:
                arcpy.AddError("  .. Missing field with oids for " + line_name)
            if not p_match:
                arcpy.AddError("  .. Missing field with oids for " + poly_name)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Reconnecting touching error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def recreate_boundary_lines(boundary_line, polygon, topology_fcs=[]):
   # Set enviornments to override outputs and define temp workspace
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = arcpy.env.scratchGDB
    try:
        # Determine info for main_fc
        if int(arcpy.management.GetCount(polygon)[0]) >= 1:
            main_name = arcpy.da.Describe(polygon)['name']
            main_field = "FID_" + main_name
            query = main_field + " <> -1"

            topo_names = []
            for feat_class in topology_fcs:
                topo_fc = str(feat_class)
                if topo_fc != polygon:
                    if int(arcpy.management.GetCount(topo_fc)[0]) >= 1:
                        name = arcpy.da.Describe(topo_fc)['name']
                        topo_names.append(name)
                        if name != main_name:
                            query += (" AND FID_" + name + " = -1")
                else:
                    if feat_class in topology_fcs:
                        topology_fcs.remove(feat_class)
            topology_fcs.insert(0, polygon)

            # Run feature to line to split at each break...
            arcpy.AddMessage(" ... Creating lines")
            temp = arcpy.management.FeatureToLine(topology_fcs, "temp_boundary_line", attributes="NO_ATTRIBUTES")
            arcpy.management.RepairGeometry(temp)
            # Select only those lines relating to the main fc
            arcpy.AddMessage(" ... Selecting lines to rebuild")
            topology_fcs.remove(polygon)
            temp_layer = arcpy.management.MakeFeatureLayer(temp, "temp_layer")
            arcpy.management.SelectLayerByAttribute(temp_layer, "NEW_SELECTION", "OBJECTID >= 1")

            for feat_class in topology_fcs:
                if int(arcpy.management.GetCount(temp_layer)[0]) >= 1:
                    arcpy.AddMessage("Add To Selection")
                    if feat_class != polygon:
                        arcpy.management.SelectLayerByLocation(temp_layer, "SHARE_A_LINE_SEGMENT_WITH", feat_class, selection_type="REMOVE_FROM_SELECTION")
                else:
                    arcpy.AddMessage("New To Selection")
                    arcpy.management.SelectLayerByLocation(temp_layer, "SHARE_A_LINE_SEGMENT_WITH", feat_class, selection_type="NEW_SELECTION")

            arcpy.AddMessage(str(int(arcpy.management.GetCount(boundary_line)[0])) + " new boundary lines created")
            if int(arcpy.management.GetCount(boundary_line)[0]) >= 1:
                with_atts = arcpy.analysis.SpatialJoin(temp_layer, boundary_line, "Temp_boundary_attributes", join_operation="JOIN_ONE_TO_ONE", join_type="KEEP_ALL")
            else:
                arcpy.management.SelectLayerByAttribute(temp_layer, "CLEAR_SELECTION")
                with_atts = temp_layer
            arcpy.AddMessage(" ... Deleting lines from boundary" )
            arcpy.management.DeleteFeatures(boundary_line)

            arcpy.AddMessage(" ... Appending new lines to boundary")
            arcpy.management.Append(with_atts, boundary_line, "NO_TEST")
        else:
            arcpy.AddMessage(" ... Deleting lines from boundary" )
            arcpy.management.DeleteFeatures(boundary_line)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Recreate boundary lines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def getAttributeValue(dataset, objectid, field):
    try:
        # Get OBJECTID field
        oid_fld = arcpy.da.Describe(dataset)['OIDFieldName']
        # Create where clause to only get feature with OBJECTID passed to function
        where = f"{oid_fld} = {objectid}"
        # Create a feature layer of a single feature for the OBJECTID
        arcpy.management.MakeFeatureLayer(dataset, "lyr", where)
        # Use a search cursor to return the value of an attribute
        with arcpy.da.SearchCursor("lyr", [field]) as cur:
            for row in cur:
                attr_val = row[0]

        return attr_val

    except Exception as e:
            tb = traceback.format_exc()
            error_message = f"Get attribute value error: {e}\nTraceback details:\n{tb}"
            arcpy.AddError(error_message)

def trim_polygon_within_distance(input_poly, name_fld, compare_feature, distance, min_area, delete, working_gdb):
    # Environment variables
    arcpy.env.overwriteOutput = 1
    try:
        clean_list = []

        if not compare_feature:
            compare_feature = input_poly
    
        # Only include polys that have a name or have an area greater than 2,500 meters
        # Create where clause
        area_fld_d = arcpy.da.Describe(input_poly)['areaFieldName']
        where = ("(" + name_fld + " <> '' AND " + name_fld + " IS NOT NULL) OR (" +
                 area_fld_d + " > " + str(min_area) + ")")
        arcpy.AddMessage(where)
        arcpy.management.MakeFeatureLayer(input_poly, "poly_lyr", where)
        clean_list.append("poly_lyr")

        comp_lyr = arcpy.management.MakeFeatureLayer(compare_feature, "comp_lyr")
        clean_list.append("comp_lyr")
        comp_type = arcpy.da.Describe(compare_feature)['shapeType']
        comp_name = arcpy.da.Describe(compare_feature)['name']
        input_name = arcpy.da.Describe(input_poly)['name']

        arcpy.AddMessage("Querying features within " + str(distance) + " of each other...")
        # Generate near table to find all polygons within 12.5 meters of each other
        near_tbl = arcpy.analysis.GenerateNearTable("poly_lyr", comp_lyr, "near_tbl", distance, closest="ALL").getOutput(0)
        clean_list.append(near_tbl)

        arcpy.AddMessage("Processing features...")
        # Loop through table and store all IN_FID values in a dictionary as the key
        #    with the NEAR_FID in a list as a value
        fids_dict = {}
        with arcpy.da.SearchCursor(near_tbl, ["IN_FID", "NEAR_FID"]) as cur:
            for row in cur:
                if str(row[0]) not in fids_dict.keys():
                    # If IN_FID is not already a key in the dictionary,
                    #    add the key and value to the dictionary
                    fids_dict[str(row[0])] = [str(row[1])]
                else:
                    # Else, if IN_FID is already a key in the dictionary,
                    #    append the value to the list of values for that key
                    fids_dict[str(row[0])].append(str(row[1]))

        # Initialize geometry look-up table in dictionary. This dictionary will
        #     be used as a look-up table within the update cursor when the
        #     geometry of each feature in the poly layer is updated to meet the
        #     distance requirement.
        geom_dict = {}

        # Initialize list of OBJECTIDs for features that need their geom updated
        oid_list = []

        # # Create empty geometry to store intermediate data in memory
        # g = arcpy.Geometry()

        # Key/value pairs are duplicated in dictionary. For example:
        #     {'1': ['2'], '2': [1]}
        #     Need to maintain list to save time so not checking same pair
        #     of geometries more than once
        kv_list = []
        # Loop through key/value pairs in dictionary
        #     Find intersection of buffers, and erase intersection from larger poly
        for key, val in fids_dict.items():
            # Initialize updated key flag to False
            #     It's possible that there could be two or more NEAR_FID features
            #     for a given IN_FID (if len(val) > 1).  In that case, if the geom
            #     of the key is updated, need to use the updated geom when creating
            #     buffers and erasing intersection
            updated_key = False
            # Get geometry of IN_FID
            infid_shp = getAttributeValue("poly_lyr", key, "SHAPE@")

            # Create buffer around IN_FID
            g = f"{working_gdb}\\infid_buff"
            infid_buff = arcpy.analysis.Buffer(infid_shp, g, distance)
            infid_buff = [row[0] for row in arcpy.da.SearchCursor(infid_buff, ['SHAPE@'])][0]

            # Value in dictionary is a list of NEAR_FID records
            #    Loop through each NEAR_FID in list
            for v in val:
                # Check if key/value pair has already been tested
                if (key, v) not in kv_list:
                    # Append key/value tuple to list as (value, key)
                    kv_list.append((v, key))

                    nearfid_shp = getAttributeValue(comp_lyr, v, "SHAPE@")

                    # Check if updated key flag is true. If it is, then use updated
                    #     key geom to create buffer instead of original key geom
                    if updated_key:
                        # Create buffer around IN_FID
                        g = f"{working_gdb}\\infid_buff"
                        infid_buff = arcpy.analysis.Buffer(new_key, g, distance)
                        infid_buff = [row[0] for row in arcpy.da.SearchCursor(infid_buff, ['SHAPE@'])][0]

                    # Create buffer around NEAR_FID
                    g = f"{working_gdb}\\nearfid_buff"
                    nearfid_buff = arcpy.analysis.Buffer(nearfid_shp, g, distance)
                    nearfid_buff = [row[0] for row in arcpy.da.SearchCursor(nearfid_buff, ['SHAPE@'])][0]

                    # Get intersection between IN_FID buffer and NEAR_FID buffer
                    g = f"{working_gdb}\\intrsct"
                    intrsct = arcpy.analysis.Intersect([infid_buff, nearfid_buff], g)
                    intrsct = [row[0] for row in arcpy.da.SearchCursor(intrsct, ['SHAPE@'])][0]

                    # Get geometry of both IN_FID and NEAR_FID to determine larger feature
                    #     Intersection will be erased from larger feature - from Amber:
                    #     "The reason why I vote to remove the geometry from the larger of
                    #     the two polygons is there are other requirements that each of the
                    #     polygons be at least 2500 meters square so we make some of the
                    #     polygons bigger.  Most of the time, the reason my the lakes are so
                    #     close together is because one of them was made larger and if we
                    #     remove the geometry from that feature, it now becomes less than
                    #     the minimum size."
                    infid_area = infid_shp.area
                    if comp_type == "Polygon":
                        nearfid_area = nearfid_shp.area
                    else:
                        nearfid_area = 0

                    # Erase intersection from larger geometry
                    # Erasing intersection of buffers will ensure that no other feature
                    #     is within 12.5 meters of the polygon
                    if infid_area > nearfid_area:
                        update_feat = key # This is the OID of the geom that needs to be updated
                        # Set update key flag to True
                        updated_key = True
                        new_key = infid_shp
                        # Append IN_FID value (key) to OBJECTID list
                        oid_list.append(key)
                    else:
                        update_feat = v # This is the OID of the geom that needs to be updated
                        # Append NEAR_FID value (v in val) to OBJECTID list
                        oid_list.append(v)

                    # Store result from erase in the geometry look-up dictionary as the
                    #     value where the key is the ObjectID of the feature that needs
                    #     to be updated.
                    if update_feat in geom_dict:
                        geom_shp = geom_dict[update_feat]
                        union_shp = geom_shp.union(intrsct)
                        geom_dict[update_feat] = union_shp

                    else:
                        geom_dict[update_feat] = intrsct

        # Remove duplicates from the OBJECTID list and create where clause
        #     for use in Update Cursor
        oid_list = list(set(oid_list))
        # Sort list numerically. When list of OBJECTIDs is printed to GP Window,
        #     it will be easier to follow if OBJECTIDs are printed numerically.
        oid_list.sort()
        erase_geos = []

        if len(oid_list) >= 1:
            arcpy.AddMessage("Updating feature geometry...")

            # Where clause to create subset of only features with geom updates
            oid_fld = arcpy.da.Describe(input_poly)['OIDFieldName']
            where_updates = oid_fld + " = " + (" OR " + oid_fld + " = ").join(oid_list)

            # Create update cursor to update poly geometries
            with arcpy.da.UpdateCursor("poly_lyr", ["OID@", "SHAPE@", "INVISIBILITY"], where_updates) as cur:
                for row in cur:
                    # Use geometry look-up dictionary to get poly geometry
                    #     associated with OBJECTID
                    shp = geom_dict[str(row[0])]
                    g = f"{working_gdb}\\erase"
                    erase = arcpy.analysis.Erase(row[1], shp, g)
                    erase = [row[0] for row in arcpy.da.SearchCursor(erase, ['SHAPE@'])]
                    if erase:
                        new_shp = erase[0]
                        erase_geos.append(shp)
                        if new_shp.area >= min_area:
                            arcpy.AddMessage("Updating geometry of feature " + str(row[0]))
                            # Update geometry with poly geometry
                            row[1] = new_shp
                            row[2] = 0
                            # Update row
                            cur.updateRow(row)
                        else:
                            if delete == 'TRUE':
                                arcpy.AddWarning("Feature " + str(row[0]) +
                                " is smaller than minimum size when trimmed." +
                                " Setting feature to be invisible.")
                                # Update feature with invisibility = 1 to hide feature
                                row[2] = 1
                                # Update row
                                cur.updateRow(row)
                            else:
                                arcpy.AddWarning("Feature " + str(row[0]) +
                                " is smaller than minimum size when trimmed." +
                                " Must perform manual edit of feature.")

                    else:
                        arcpy.AddWarning("Feature " + str(row[0]) +
                        " is smaller than minimum size when trimmed." +
                        " Setting feature to be invisible.")
                        # Update feature with invisibility = 1 to hide feature
                        row[2] = 1
                        # Update row
                        cur.updateRow(row)
        else:
            arcpy.AddMessage("No features to update...")

        # Delete temp files
        arcpy.management.Delete(clean_list)

        if len(erase_geos) >= 1:
            delete_areas = working_gdb + "\\delete_areas_" + input_name + "_" + comp_name
            arcpy.management.CopyFeatures(erase_geos, delete_areas)
        else:
            delete_areas = arcpy.management.CreateFeatureclass(working_gdb, "delete_areas_" + input_name + "_" + comp_name, "POLYGON")

        return delete_areas

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Trim polygon within distance error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def make_unique_layers(layer_list, map_name):
    aprx = arcpy.mp.ArcGISProject('CURRENT')
    maps = aprx.listMaps(map_name)[0]

    unique = {}
    for lyr in layer_list:
        if lyr.name not in unique:
            unique[lyr.name] = lyr
        else:
            maps.removeLayer(lyr)
    fc_layers = list(unique.values())

    return fc_layers

# other helper functions

def extendPolyLineToPoint(layer, extension_pt):
    """
       Extends a polyline's closest endpoint to a point
       Input:
          layer - feature layer - should be a selection set
          extension_pt - arcpy.PointGemetry() object
       Returns:
          None
    """
    try:
        arcpy.AddMessage("Extending lines to polygon center.")
        array = arcpy.Array()
        with arcpy.da.UpdateCursor(layer, ["SHAPE@"]) as rows:
            for row in rows:
                line_geom = row[0]
                firstPoint = line_geom.firstPoint
                lastPoint = line_geom.lastPoint
                if extension_pt.distanceTo(firstPoint) > extension_pt.distanceTo(lastPoint):
                    for part in row[0]:
                        for pnt in part:
                            array.add(pnt)
                        break
                    array.add(extension_pt.centroid)
                else:
                    array.add(extension_pt.centroid)
                    for part in row[0]:
                        for pnt in part:
                            array.add(pnt)
                        break
                polyline = arcpy.Polyline(array)
                array.removeAll()
                row[0] = polyline
                rows.updateRow(row)
                del row
                del firstPoint
                del lastPoint
                del line_geom

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Extend polyline to point error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def ConvertEnclosed(primaryFCLyr, secondaryFCLyrs, working_gdb):
    arcpy.AddMessage("Searching for features that are fully contained.")
    #-------------------------------------------------------------------------------
    # THIS SECTION HANDLES ENCLOSED FEATURES THAT OVERLAP OTHER FC (NOT OVERLAP HOLES)
    # using 'COMPLETELY_WITHIN' filter
    #-------------------------------------------------------------------------------
    # Set Environment
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb
    try:
        secondaryFCNames = []
        for layer in secondaryFCLyrs:
            desc = arcpy.da.Describe(layer)
            FCName = desc['name']
            secondaryFCNames.append(layer)

        delete_ids = []
        for i in range(len(secondaryFCLyrs)):    # keep all secondary lists indices same
            surroundingFCLyr = secondaryFCLyrs[i]

            # Get new selection to refresh selection set
            with arcpy.da.SearchCursor(primaryFCLyr, ("OID@", "SHAPE@")) as cursor:
                for row in cursor:
                    desc = arcpy.da.Describe(surroundingFCLyr)
                    surroundName = desc['name']
                    primaryOID = row[0]
                    primary_geo = row[1]

                    # Is the primary feature completely within surrounding feature?
                    spatialSelectedFeatures = arcpy.management.SelectLayerByLocation(surroundingFCLyr, "COMPLETELY_CONTAINS", primary_geo)
                    if int(arcpy.management.GetCount(spatialSelectedFeatures).getOutput(0)) > 0:
                        # Get ID of ssurrounding feature
                        surroundingFIDSet = [int(oid) for oid in arcpy.da.Describe(surroundingFCLyr)['FIDSet']]
                        # Create temp fc to store and process geometry
                        tempContainedFC =  surroundName + "_removeContainedPolygonsTmp"
                        # Copy surrounding feature to temp FC
                        test = arcpy.management.CopyFeatures(surroundingFCLyr, f"{working_gdb}\\test_fc")
                        arcpy.management.CopyFeatures(test, tempContainedFC)     # cannot use arcpy.Geometry() as it fails in append_management
                        # Now select the feature from primary FC and append its geometry to a temp FC
                        arcpy.management.Append([primaryFCLyr], tempContainedFC , "NO_TEST", "", "")

                        # Select appended features (any OID > 1 , in this case) and eliminate
                        tempContainedFCLyr = tempContainedFC + "Lyr"
                        arcpy.management.MakeFeatureLayer(tempContainedFC, tempContainedFCLyr)

                        newFeatures = arcpy.management.SelectLayerByAttribute(tempContainedFCLyr, "NEW_SELECTION", "OBJECTID > 1")
                        if int(arcpy.management.GetCount(newFeatures).getOutput(0)) > 0:
                            elimContainedFeat= arcpy.management.Eliminate(tempContainedFCLyr, f"{working_gdb}\\elim_contained_feature", "AREA")
                            elimContainedFeat = [row[0] for row in arcpy.da.SearchCursor(elimContainedFeat, ['SHAPE@'])]

                            # Update secondary feature with new geometry
                            # Including another field 'NAM' in query as placeholder - else update not working
                            with arcpy.da.UpdateCursor(surroundingFCLyr, ("NAM","SHAPE@"),"OBJECTID=" + str(surroundingFIDSet[0]) ) as updateCursor:
                                for updtRow in updateCursor:
                                    # Update with geometry from eliminate tool
                                    updtRow = (updtRow[0], elimContainedFeat[0])
                                    updateCursor.updateRow(updtRow)
                                    arcpy.AddMessage("Updated contained geometry for ID {0} in {1}".format(str(surroundingFIDSet[0]), surroundName))

                        # Delete the original selected features from the primary FC  - just to avoid confusion for later queries
                        delete_ids.append(str(primaryOID))
                        # Delete the temp FC
                        arcpy.management.Delete(tempContainedFCLyr)
                        arcpy.management.Delete(tempContainedFC)

        if len(delete_ids) >= 1:
            # delete the original features
            where = "OBJECTID = "
            where += " OR OBJECTID = ".join(delete_ids)
            arcpy.management.SelectLayerByAttribute(primaryFCLyr, "NEW_SELECTION", where)
            arcpy.management.DeleteFeatures(primaryFCLyr)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Convert enclosed error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def ConvertOverlapping(primaryFCLyr, secondaryFCLyrs, working_gdb):
    arcpy.AddMessage("Searching for features that overlap.")
    #-------------------------------------------------------------------------------
    # THIS SECTION HANDLES ENCLOSED FEATURES THAT OVERLAP EMPTY GEOMETRY/HOLES
    #-------------------------------------------------------------------------------
    try:
        # All input FC for FeatureToLine_management
        inputLayers = secondaryFCLyrs
        inputLayers.append(primaryFCLyr)

        numbFeats =int(arcpy.management.GetCount(primaryFCLyr).getOutput(0))
        if numbFeats > 0:
            arcpy.AddMessage(str(numbFeats) + " selected from input")
            # create FeatureToLine for primary selected features and ALL secondary FC
            featureToLineFC = f"{working_gdb}\\featureToLineFC"
            arcpy.AddMessage("Running Feature to Line")
            scratch = arcpy.env.scratchGDB
            arcpy.management.FeatureToLine(inputLayers, featureToLineFC, "", "ATTRIBUTES")

            desc = arcpy.da.Describe(primaryFCLyr)['catalogPath']
            primaryFCName = arcpy.da.Describe(desc)['name']

            lstQueryFields = []
            secondaryFIDFields = []
            secondaryFCNames = []
            for layer in secondaryFCLyrs:
                desc = arcpy.da.Describe(layer)['catalogPath']
                FCName = arcpy.da.Describe(desc)['name']
                secondaryFCNames.append(layer)
                secondaryFIDFields.append("FID_" + str(FCName))
                lstQueryFields.append("FID_" + str(FCName))

            # Now get the FID field name
            primaryFIDField = "FID_" + str(primaryFCName)

            if primaryFIDField in secondaryFIDFields:
                secondaryFIDFields.remove(primaryFIDField)
            # Create a Describe object from the GDB Feature Class
            desc = arcpy.da.Describe(featureToLineFC)
            shapeLength = desc['lengthFieldName']

            # Get all unique primary IDs
            whereClause = primaryFIDField + " > -1 "
            lstPrimaryFCIDs = [row[0] for row in arcpy.da.SearchCursor(featureToLineFC, primaryFIDField, whereClause)]
            uniquePrimaryIDs = set(lstPrimaryFCIDs)
            if(shapeLength):
                lstQueryFields.append(shapeLength)

            # Dictionary to store primary feat ID and secondary FC it should be appended to
            primaryFeatureAppend = {}

            # order by field to get Max length and get only Top row
            # ****including prefix errors out - have to work around it
            # prefix = " TOP 1 "
            # for each unique primary ID , get the max length by 'DESC' query and getting only first row
            arcpy.AddMessage("Determining features to convert.")
            for uniqPrimaryID in uniquePrimaryIDs:
                postfix  = "ORDER BY " + shapeLength + " DESC"
                whereClause = primaryFIDField + " = " + str(uniqPrimaryID)
                cnt = 0
                if (has_features_fields_where(featureToLineFC, lstQueryFields, whereClause)):
                    with arcpy.da.SearchCursor(featureToLineFC, lstQueryFields, whereClause, sql_clause = (None, postfix ) ) as cursor:
                        # Get first row only
                        for row in cursor:
                            index = []
                            for i in range(len(secondaryFIDFields)):    # keep both lists indices same
                                # arcpy.AddMessage("for uniqid  {0} {1} = {2}".format(str(uniqPrimaryID), secondaryFIDFields[i], row[i]))
                                # check which FID_.... field is > -1 and get its FID value , and the index postion of this FC in the secondaryFIDFields list
                                # say if FID_V_Forest_A has a value greater than -1 , then the primary feature should be appended with V_Forest_A
                                if row[i] > 0:
                                    # used index to determine if more than one secondary
                                    # shares this line segment
                                    index.append(i)
                            # Dictionary key=primaryFeatOID , value = [index in secondaryFIDFields/secondaryFCNames, secondary feat OID]
                            if len(index) == 1:
                                primaryFeatureAppend[uniqPrimaryID] = [index[0], row[index[0]]]
                                break
                            if len(index) > 1:
                                arcpy.AddMessage("Shares boundary with multiple secondary")
                                break
                            cnt += 1

            for i in range(len(secondaryFCLyrs)):
                secondaryLyr = secondaryFCLyrs[i]
                # Dictionary to store primary feat ID and secondary FC ID it should be appended to
                OIDPairs = {}
                for uniqPrimaryID, values in primaryFeatureAppend.items():
                    if  values[0] == i:   # index in both lists are same
                        OIDPairs[uniqPrimaryID] = values[1]    # dictionary key=primaryFeatOID , value = secondary feat OID

                for uniqPrimaryID, secondaryID in OIDPairs.items():
                    # create temp fc to store and process geometry
                    tempFC = f"{working_gdb}\\" + primaryFCName + "_removePolygonsTmp"

                    # Now select the feature from secondary FC and copy its geometry to a temp FC
                    arcpy.management.SelectLayerByAttribute(secondaryLyr, "NEW_SELECTION", "OBJECTID = " + str(secondaryID))
                    arcpy.management.CopyFeatures(secondaryLyr, tempFC)     # cannot use arcpy.Geometry() as it fails in append_management

                    # Now select the feature from primary FC and append its geometry to a temp FC
                    arcpy.management.SelectLayerByAttribute(primaryFCLyr, "NEW_SELECTION", "OBJECTID = " + str(uniqPrimaryID))
                    arcpy.management.Append([primaryFCLyr], tempFC , "NO_TEST", "", "")

                    # Delete the original selected features from the primary FC  - just to avoid confusion for later queries
                    arcpy.management.DeleteFeatures(primaryFCLyr)

                    # Select appended features and eliminate
                    tempFCLyr = tempFC + "Lyr"
                    arcpy.management.MakeFeatureLayer(tempFC, tempFCLyr)
                    newFeatures = arcpy.management.SelectLayerByAttribute(tempFCLyr, "NEW_SELECTION", "OBJECTID > 1 ")

                    if int(arcpy.management.GetCount(newFeatures).getOutput(0)) > 0:
                        elimFeat= arcpy.management.Eliminate(tempFCLyr, f"{working_gdb}\\elim_feature", "LENGTH")
                        elimFeat = [row[0] for row in arcpy.da.SearchCursor(elimFeat, ['SHAPE@'])]

                        # update secondary feature with new geometry
                        # Including another field 'NAM' in query as placeholder - else update not working
                        with arcpy.da.UpdateCursor(secondaryLyr, ("SHAPE@", "oid@"),"OBJECTID=" + str(secondaryID) ) as updateCursor:
                            for updtRow in updateCursor:
                                # Update with geometry from eliminate tool
                                updtRow[0] = elimFeat[0]
                                updateCursor.updateRow(updtRow)
                                arcpy.AddMessage("Updated geometry for ID {0} in {1}".format(str(secondaryID), secondaryFCNames[i]))

                    # Delete the temp FC
                    arcpy.management.Delete([tempFCLyr, tempFC, f"{working_gdb}\\featureToLineFC"])

                    # Delete from dictionary as each feature is processed to speed up later iterations
                    del primaryFeatureAppend[uniqPrimaryID]
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Convert overlapping error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def feature2point(working_gdb, inFc, point_fc, min_size, delete_input, one_point, unique_field, sql):
    # Set the workspace
    arcpy.env.overwriteOutput = True
    try:
        clean_list = []
        # Use Describe object and get Shape Area field
        desc = arcpy.da.Describe(inFc)
        if desc['shapeType'] == 'Polygon':
            size_field  = desc['areaFieldName']
            oid_field = desc['OIDFieldName']
        if desc['shapeType'] == 'Polyline':
            size_field  = desc['lengthFieldName']
            oid_field = desc['OIDFieldName']

        selection_criteria = ''
        if min_size:
            selection_criteria = f"{size_field} < {min_size}"
        if sql:
            if selection_criteria != '':
                selection_criteria = f"{selection_criteria} AND {sql})"
            else:
                selection_criteria = sql
        arcpy.management.MakeFeatureLayer(inFc, "SmallFeatures", selection_criteria)
        clean_list.append("SmallFeatures")
        count = int(arcpy.management.GetCount("SmallFeatures").getOutput(0))
        if count >= 1:
            if not one_point:
                # Convert polygon to point
                arcpy.AddMessage("Converting " + str(count) + " to point")
                arcpy.management.FeatureToPoint("SmallFeatures", f"{working_gdb}\\points")
                # Append point with output feature
                arcpy.AddMessage("Adding point to output feature class")
                arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

                if arcpy.Exists(f"{working_gdb}\\points"):
                    arcpy.management.Delete(f"{working_gdb}\\points")
                # Delete the features in the polygon feature class
                if delete_input:
                    arcpy.AddMessage( "Deleting features from " + inFc)
                    arcpy.management.DeleteFeatures("SmallFeatures")
                    arcpy.management.Delete("SmallFeatures")
            else:
                convertOIDs = []
                deleteOIDs = []
                arcpy.AddMessage("Determining which feature to convert")
                values = [row[0] for row in arcpy.da.SearchCursor("SmallFeatures", unique_field)]
                uniqueValues = set(values)
                # uniqueValues.discard("")
                # uniqueValues.discard(" ")
                # uniqueValues.discard(None)
                uniqueValues.difference_update({"", " ", None})
                for val in uniqueValues:
                    val = val.replace("'", "''")
                    postfix  = f"ORDER BY {size_field} DESC"
                    whereClause = f"{unique_field} = '{val}'"
                    arcpy.management.SelectLayerByAttribute("SmallFeatures", "NEW_SELECTION", whereClause)
                    oids = [row[0] for row in arcpy.da.SearchCursor("SmallFeatures", ['OID@', size_field, unique_field], sql_clause = (None, postfix))]
                    convertOIDs.append(oids[0])
                    deleteOIDs.append(oids)
                arcpy.AddMessage("Converting largest features to points")
                convert_layer = arcpy.management.MakeFeatureLayer(inFc, "convert_lyr")
                clean_list.append("convert_lyr")
                for oid in convertOIDs:
                    where = oid_field + " = " + str(oid)
                    arcpy.management.SelectLayerByAttribute(convert_layer, "ADD_TO_SELECTION", where)
                count = int(arcpy.management.GetCount(convert_layer).getOutput(0))
                if count >= 1:
                    arcpy.management.FeatureToPoint(convert_layer, f"{working_gdb}\\points")
                    arcpy.AddMessage("Adding point to output feature class")
                    arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

                    if arcpy.Exists(f"{working_gdb}\\points"):
                        arcpy.management.Delete(f"{working_gdb}\\points")

                arcpy.AddMessage(f"Converting features with no value in {unique_field}")
                where = f"{unique_field} IS NULL OR {unique_field} = ''"
                if sql:
                    where = f"{where} AND ({sql})"
                arcpy.management.SelectLayerByAttribute(convert_layer, "NEW_SELECTION", where)
                count = int(arcpy.management.GetCount(convert_layer).getOutput(0))
                if count >= 1:
                    arcpy.management.FeatureToPoint(convert_layer, f"{working_gdb}\\points")
                    arcpy.AddMessage("Adding point to output feature class")
                    arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

                    if arcpy.Exists(f"{working_gdb}\\points"):
                        arcpy.management.Delete(f"{working_gdb}\\points")
                if delete_input:
                    arcpy.management.SelectLayerByAttribute("SmallFeatures", "CLEAR_SELECTION")
                    arcpy.AddMessage( "deleting features from " + inFc)
                    # arcpy.management.DeleteFeatures("SmallFeatures")
                    arcpy.management.Delete("SmallFeatures")
        else:
            arcpy.AddMessage("No features meet criteria to be converted to point.")

        clean_list = ["convert_lyr", f"{working_gdb}\\points", "SmallFeatures"]
        # Delete temp files
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Feature to point error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


""" Polygon Features to Point convertion function that honor the 'unique_field' value and 'min_size' criteria both. Currently not in use"""
# def feature2point_bldg(inFc, point_fc, min_size, delete_input, one_point, unique_field, working_gdb):
#     # Set the workspace
#     arcpy.env.overwriteOutput = True

#     try:
#         # Use Describe object and get Shape Area field
#         desc = arcpy.da.Describe(inFc)
#         if desc['shapeType'] == 'Polygon':
#             size_field  = desc['areaFieldName']
#             unique_delimit = arcpy.AddFieldDelimiters(inFc, unique_field)
#             name_query = f"({unique_delimit} = '' or {unique_delimit} = ' ' or {unique_delimit} IS NULL)"
#             selectionCriteria = ''
#             if min_size:
#                 selectionCriteria = f"{size_field} < {min_size}"

#             # Create feature layer with selection criteria   
#             arcpy.management.MakeFeatureLayer(inFc, "SmallFeatures", selectionCriteria)
#             # Select features based on unique field
#             arcpy.management.SelectLayerByAttribute("SmallFeatures", "NEW_SELECTION", f"{selectionCriteria} And {name_query}")
#             # Get count of selected features
#             count = count_features("SmallFeatures")
#             # Convert polygon to point
#             arcpy.AddMessage("Converting " + str(count) + " to point")
#             arcpy.management.FeatureToPoint("SmallFeatures", f"{working_gdb}\\points")
#             # Append point with output feature
#             arcpy.AddMessage("Adding point to output feature class")
#             arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

#             # Delete the features in the polygon feature class
#             if delete_input:
#                 arcpy.AddMessage( "Deleting features from " + inFc)
#                 arcpy.management.DeleteFeatures("SmallFeatures")

#             # Appying domain to the point feature class
#             fld_source = arcpy.ListFields(inFc)
#             fld_target = arcpy.ListFields(point_fc)
#             domain_dict = {f.name: f.domain for f in fld_source if f.domain}
#             for t in fld_target:
#                 if t.name in domain_dict:
#                     domain_name = domain_dict[t.name]
#                     subtype_code = ""
#                     arcpy.management.AssignDomainToField(point_fc, t.name, f"{domain_name}", subtype_code)
#         else:
#             arcpy.AddMessage("No features meet criteria to be converted to point.")

#             clean_list = ["convert_lyr", f"{working_gdb}\\points", "SmallFeatures", f"{working_gdb}\\points"]
#             # Delete temp files
#             arcpy.management.Delete(clean_list)

#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Feature to point for building error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)

""" New Polygon Features to Point convertion function for features less then 'min_size' threshold, regardless of the 'unique_field' value. Currently in use"""
def feature2point_bldg(inFc, point_fc, min_size, delete_input, one_point, unique_field, working_gdb):
    # Set the workspace
    arcpy.env.overwriteOutput = True

    try:
        # Use Describe object and get Shape Area field
        desc = arcpy.da.Describe(inFc)

        if desc["shapeType"] == "Polygon":
            size_field = desc["areaFieldName"]
            # unique_delimit = arcpy.AddFieldDelimiters(inFc, unique_field)

            selectionCriteria = ""
            if min_size:
                selectionCriteria = f"{size_field} < {min_size}"

            arcpy.AddMessage(f"Selection Criteria: {selectionCriteria}")
            # Create feature layer with area-based selection only
            arcpy.management.MakeFeatureLayer(inFc, "SmallFeatures", selectionCriteria)

            count = count_features("SmallFeatures")

            if count > 0:
                # Convert polygon to point
                arcpy.AddMessage("Converting " + str(count) + " to point")
                arcpy.management.FeatureToPoint("SmallFeatures", f"{working_gdb}\\points")

                # Append point with output feature
                arcpy.AddMessage("Adding point to output feature class")
                arcpy.management.Append(f"{working_gdb}\\points", point_fc, "NO_TEST")

                # Delete the features in the polygon feature class
                if delete_input:
                    arcpy.AddMessage("Deleting features from " + inFc)
                    arcpy.management.DeleteFeatures("SmallFeatures")

                # Applying domain to the point feature class
                fld_source = arcpy.ListFields(inFc)
                fld_target = arcpy.ListFields(point_fc)
                domain_dict = {f.name: f.domain for f in fld_source if f.domain}

                for t in fld_target:
                    if t.name in domain_dict:
                        domain_name = domain_dict[t.name]
                        subtype_code = ""
                        arcpy.management.AssignDomainToField(point_fc, t.name, f"{domain_name}", subtype_code)
            else:
                arcpy.AddMessage("No polygon features meet the criteria of less then 'min_size' to be converted to point.")

            clean_list = ["SmallFeatures", f"{working_gdb}\\points"]
            arcpy.management.Delete(clean_list)

        else:
            arcpy.AddMessage("Input feature class is not polygon.")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Feature to point for building error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def gen_shared_features(main_fc, generalize_operations, simple_tolerance, smooth_tolerance, working_gdb, topology_fcs, barrier_line_fc):
    # Set enviornments to override outputs and define temp workspace
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb

    fcs = []
    fc_paths = {}
    clean_list = []
    
    try:
        if has_features(main_fc):
            arcpy.AddMessage("Determining shared edges...")
            # Determine info for main_fc
            main_name = arcpy.da.Describe(main_fc)['name']
            # Split topology fcs
            for topo_fc in topology_fcs:
                fcs, fc_paths = process_fc(topo_fc, fcs, fc_paths) 
            # Run feature to line to split at each break...
            arcpy.AddMessage(" ... Combining lines")
            out_lines = arcpy.management.FeatureToLine(fcs, f"{working_gdb}\\generalize_lines", "#", "ATTRIBUTES")
            clean_list.append(out_lines)
            # arcpy.management.Integrate(out_lines)
            arcpy.management.Integrate([out_lines])
            arcpy.management.RepairGeometry(out_lines)
            # Select only those lines relating to the main fc
            fields = arcpy.ListFields(out_lines, f"*{main_name}*")
            if fields:
                main_field = fields[0].name
                query = main_field + " <> -1"
            else:
                raise Exception("Unable to determine output field that stores" + "information about " + str(main_name))

            out_layer = arcpy.management.MakeFeatureLayer(out_lines, "out_layer")
            arcpy.management.SelectLayerByAttribute(out_layer, "NEW_SELECTION", query)

            # Defining "output" to ensure value exists if operations cannot be
            # performed. Also defining single varaible that can be updated
            # after each operation so that the next operation always runs on
            # the previous output
            output = out_layer
            # arcpy.AddMessage(str(int(arcpy.management.GetCount(output)[0])) + " shared lines")
            cnt = 0
            for operation in generalize_operations:
                operation = operation.strip()
                operation = operation.upper()
                if operation == "SIMPLIFY":
                # If simplication tolerance provided, run simplify
                    arcpy.AddMessage("Simplifying lines...")
                    out_name = f"{main_name}_simplify_{cnt}"
                    output= arcpy.cartography.SimplifyLine(output, out_name, "POINT_REMOVE", simple_tolerance, "FLAG_ERRORS", "NO_KEEP", "NO_CHECK", barrier_line_fc, "RESOLVE_ERRORS")
                # If smooth tolerance provided, run smooth
                elif operation == "SMOOTH":
                    arcpy.AddMessage("Smoothing lines...")
                    out_name = f"{main_name}_smooth_{cnt}"
                    output=arcpy.cartography.SmoothLine(output,out_name, "PAEK", smooth_tolerance, "FIXED_CLOSED_ENDPOINT", "RESOLVE_ERRORS", barrier_line_fc)
                else:
                    arcpy.AddWarning("Unknown generalization operation " + operation)
                cnt += 1   

            # Append the lines not smoothed or simplified back into result fc
            input_count = count_features(out_lines)
            gen_count = count_features(output)

            if gen_count < input_count:
                arcpy.management.SelectLayerByAttribute(out_layer, "SWITCH_SELECTION")
                arcpy.management.Append(out_layer, output, "NO_TEST")

            output_layer = arcpy.management.MakeFeatureLayer(output, "output_layer")

            # Loop through each of the original feature classes and rebuild geometry
            fields = arcpy.ListFields(output)
            for feat_class in fcs:
                # Based on the temp feature classes determine the matching original feature class
                feat_class = str(feat_class)
                if arcpy.Exists(feat_class):
                    name = arcpy.da.Describe(feat_class)['name']
                    topo_feat_class = fc_paths[name]
                    arcpy.AddMessage("Rebuilding " + arcpy.da.Describe(topo_feat_class)['catalogPath'] + "...")
                    # If the geometry type of the feature class is a polygon
                    shape_type = arcpy.da.Describe(topo_feat_class)['shapeType']
                    if shape_type == "Polygon":
                        id_cnt = find_id_fields(name, fields)
                        if id_cnt == -1:
                            raise Exception(f"Could not find matching ID field for feature class: {name}")
                        left_field = fields[id_cnt+1].name
                        right_field = fields[id_cnt+2].name
                        query, unique_ids = unique_query(output, main_field, left_field, right_field)
                 
                        if len(unique_ids) >= 1:
                            arcpy.management.SelectLayerByAttribute(output_layer, "NEW_SELECTION", query)
                            rebuild_features(output_layer, topo_feat_class, shape_type, unique_ids, left_field, working_gdb, right_field)
                    else:
                        temp_cnt = find_id_fields(name, fields)
                        if temp_cnt == -1:
                            raise Exception(f"Could not find matching ID field for feature class: {name}")
                        id_field = fields[temp_cnt].name
                        query, unique_ids = unique_query(output, main_field, id_field, None)
                        if len(unique_ids) >= 1:
                            arcpy.management.SelectLayerByAttribute(output_layer, "NEW_SELECTION", query)
                            rebuild_features(output_layer, topo_feat_class, shape_type, unique_ids, id_field, working_gdb, None)
                else:
                    arcpy.AddError("Unable to rebuild " + str(feat_class))
        else:
            arcpy.AddMessage("Main feature class has no features to generalize.")

        # Delete temp file
        if len(clean_list) > 0:
            arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Generalised shared features error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def enlarge_polygon_barrier(polygon_fc, sql, intersect_fc, minimum_size, enlarge, barrier_fcs, working_gdb):
    # Set the workspace
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    try:
        arcpy.AddMessage("Barriers" + str(len(barrier_fcs)))
        enlarge = float(enlarge)
        clean_list = []

        #--- determine query for selecting features ---#
        # Find area field
        desc = arcpy.da.Describe(polygon_fc)
        fc_name = desc['name']
        area_delimited = desc['areaFieldName']
        oid_delimited = desc['OIDFieldName']
        # Query for features smaller than minimum size
        query = f"{area_delimited} < {minimum_size}"
        
        if sql:
            query = query + " AND (" + sql + ")"
        # Add additional SQL query
        arcpy.AddMessage(query)
        # Select only those features with area smaller than minimum size
        arcpy.AddMessage("Selecting small features.")
        if arcpy.Exists("small_features"):
            arcpy.management.Delete("small_features")
        small_features = arcpy.management.MakeFeatureLayer(polygon_fc, "small_features", query)
        arcpy.management.MakeFeatureLayer(polygon_fc, "geo_feature", query)

        if intersect_fc:
            arcpy.management.MakeFeatureLayer(intersect_fc, "intersect")
            arcpy.management.SelectLayerByLocation(small_features, "INTERSECT", "intersect")

        result = arcpy.management.GetCount(small_features)
        count = int(result.getOutput(0))

        if has_features(small_features):
            arcpy.AddMessage(str(count) + " features are smaller than the minimum size and need to be enlarged.")
            # Open update cursor
            with arcpy.da.UpdateCursor(small_features, ['OID@', 'SHAPE@']) as cursor:
                for row in cursor:
                    geo = row[1]
                    newgeo = geo
                    arcpy.AddMessage("Processing " + str(row[0]))
                    # Get centerpoint of geometry
                    pt = geo.centroid
                    new_area = geo.area - 1
                    # Determine if starting geometry crosses any features
                    if len(barrier_fcs) >= 1:
                        arcpy.AddMessage("Determining if expanded geometry is crossed")
                    while new_area < float(minimum_size):
                        # Buffer the feature until it is bigger that the minimum size
                        newgeo = newgeo.buffer(enlarge)
                        prev_area = new_area
                        if newgeo.area >= float(minimum_size):
                            arcpy.AddMessage(str(row[0]) + " enlarged to " + str(newgeo.area))

                            # If barrier feature classes are specified
                            if len(barrier_fcs) >= 1:
                                intersect_layers = []
                                cnt = 0
                                # find any features from the barrier feature classes that cross the new geometry.
                                for barrier in barrier_fcs:
                                    barrier = str(barrier)
                                    cnt += 1
                                    desc = arcpy.da.Describe(barrier)
                                    fcName = desc['name']

                                    layerName = "layer_" + str(cnt)
                                    barrier_lyr = arcpy.management.MakeFeatureLayer(barrier, layerName)
                                    clean_list.append(barrier_lyr)

                                    # Select the features from the barrier FC that intersect the buffered geometry
                                    arcpy.management.SelectLayerByLocation(barrier_lyr, "INTERSECT", newgeo)
                                    # Ignore the features that were already within the original geometry of the feature
                                    arcpy.management.SelectLayerByLocation(barrier_lyr, "WITHIN", geo, "", "REMOVE_FROM_SELECTION")
                                    # If the barrier feature class is the polygon fC
                                    if fcName == fc_name:
                                        # Remove the feature we are processing from the selection
                                        searchIDQuery = oid_delimited + " = " + str(row[0])
                                        arcpy.management.SelectLayerByAttribute(barrier_lyr, "REMOVE_FROM_SELECTION", searchIDQuery)

                                    # count = int(arcpy.management.GetCount(barrier_lyr).getOutput(0))
                                    if has_features(barrier_lyr):   #Joy added has_features check instead of count check
                                        intersect_layers.append(barrier_lyr)

                                # If any features from the barrier cross the geometry
                                if len(intersect_layers) >= 1:
                                    arcpy.AddMessage("Enlarged feature touches a barrier feature")
                                    # Create new geometries that are split by the crossing feature
                                    if arcpy.Exists(f"{working_gdb}\\TempGeo"):
                                        arcpy.management.Delete(f"{working_gdb}\\TempGeo")
                                    geofeat = arcpy.management.CopyFeatures(newgeo, f"{working_gdb}\\TempGeo")
                                    intersect_layers.append(geofeat)

                                    out_poly = f"{working_gdb}\\outpoly"
                                    if arcpy.Exists(f"{working_gdb}\\outpoly"):
                                        arcpy.management.Delete(out_poly)
                                    arcpy.management.FeatureToPolygon(intersect_layers, out_poly)
                                    with arcpy.da.SearchCursor(out_poly, ["SHAPE@"]) as mem_cur:
                                        for mem_row in mem_cur:
                                            # Find the part of the geometry that contains the center of the original geometry.
                                            if pt.within(mem_row[0]):
                                                # Keep that geometry
                                                arcpy.AddMessage("Enlarged feature will be split at barrier.")
                                                newgeo = mem_row[0]

                                                arcpy.AddMessage(str(row[0]) + " split to " + str(newgeo.area))

                            new_area = newgeo.area
                            if new_area >= float(minimum_size):
                                arcpy.AddMessage("Update Record")
                                row[1] = newgeo
                                cursor.updateRow(row)

                            arcpy.AddMessage("Assign new area")
                            arcpy.AddMessage(str(round(prev_area, 12)))
                            arcpy.AddMessage(str(round(new_area, 12)))
                            if prev_area == new_area:
                                arcpy.AddWarning("Cannot enlarge feature without crossing barriers.")
                                new_area = float(minimum_size) + 1.0
        else:
            arcpy.AddMessage("No features smaller than minimum size.")
        # Delete temp files
        clean_list = [f"{working_gdb}\\TempGeo", f"{working_gdb}\\outpoly", "small_features"]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Enlarge polygon barrier error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def erase_polygons_by_replace(enlargeFC, eraseList, sql, working_gdb):
    # Set the workspace
    arcpy.env.overwriteOutput = True
    arcpy.AddMessage("Scratch : " + working_gdb)
    try:
        enlarge_features = arcpy.management.MakeFeatureLayer(enlargeFC, "enlarge_features")

        for erase in eraseList:
            if arcpy.Exists(erase):
                # count = int(arcpy.management.GetCount(erase)[0])
                if has_features(erase):   #Joy added has_features check instead of count check
                    desc = arcpy.da.Describe(erase)
                    fcName = desc['name']
                    arcpy.AddMessage("comparing " + fcName + " to " + enlargeFC)
                    if sql:
                        erase_features = arcpy.management.MakeFeatureLayer(erase, "erase_features", sql)
                        select_erase_fc = arcpy.management.SelectLayerByLocation(erase_features, "INTERSECT", enlarge_features)
                    else:
                        erase_features = arcpy.management.MakeFeatureLayer(erase, "erase_features")
                        select_erase_fc = arcpy.management.SelectLayerByLocation(erase_features, "INTERSECT", enlarge_features)

                    # result = int(arcpy.management.GetCount(select_erase_fc)[0])
                    if has_features(select_erase_fc):   #Joy added has_features check instead of count check
                        arcpy.AddMessage("Erasing Features")
                        erase_out = working_gdb + "\\erase_out"
                        arcpy.AddMessage("output: " + erase_out)
                        arcpy.analysis.PairwiseErase(erase_features, enlarge_features, erase_out)

                        arcpy.AddMessage("Deleting features in " + fcName)
                        arcpy.management.DeleteFeatures(erase_features)

                        arcpy.AddMessage("Adding erased features to " + fcName)
                        arcpy.management.Append(erase_out, erase, "NO_TEST")

                        if arcpy.Exists(erase_out):
                            arcpy.management.Delete(erase_out)

                    if arcpy.Exists(erase_features):
                        arcpy.management.Delete(erase_features)
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Erase polygon by replace error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def merge_touching_features_new(polygon_fc, sql, name_field, working_gdb):
    """if multiple segements of the line are connected, merge them togehter"""
    try:
        if sql:
            poly_layer = arcpy.management.MakeFeatureLayer(polygon_fc, "poly_layer", sql)
        else:
            poly_layer = arcpy.management.MakeFeatureLayer(polygon_fc, "poly_layer")

        arcpy.AddMessage("Dissolving Touching Features")
        near = arcpy.analysis.GenerateNearTable(poly_layer, poly_layer, f"{working_gdb}\\near_tab", "0 Meters", "NO_LOCATION", "NO_ANGLE", "ALL")
        touching_ids = []
        near_dict = {}
        with arcpy.da.SearchCursor(near, ["IN_FID", "NEAR_FID"]) as cursor:
            for row in cursor:
                # If this is the first record for that in_fid value
                if row[0] not in touching_ids:
                    # Add to the touching_ids list and near dictionary
                    touching_ids.append(row[0])
                    near_dict[row[0]] = [row[1]]
                # If this is not the first record
                else:
                    # Updated the dictionary to add the new near id
                    cur_list = near_dict[row[0]]
                    cur_list.append(row[1])
                    near_dict[row[0]] = cur_list

        arcpy.AddMessage(str(len(touching_ids)) + " features touch other features.")
        if len(touching_ids) >= 1:
            geo_dict = {}
            with arcpy.da.SearchCursor(poly_layer, ['oid@', 'shape@', name_field]) as cursor:
                for row in cursor:
                    if row[0] in touching_ids:
                        if row[2]:
                            name_val = row[2]
                        else:
                            name_val = "None"
                        geo_dict[row[0]] = [row[1], name_val]

            keep_ids = []
            delete_ids = []
            with arcpy.da.UpdateCursor(poly_layer, ['oid@', 'shape@', name_field], sql_clause=(None, "ORDER BY " + name_field + " DESC")) as cursor:
                for row in cursor:
                    obj_id = row[0]
                    merged_ids = []
                    # If close to another feature but hasn't already been merged
                    if obj_id in touching_ids and obj_id not in merged_ids:
                        arcpy.AddMessage(str(obj_id))
                        # Get a list of the ids that touch
                        keep_ids.append(obj_id)
                        id_list = near_dict[obj_id]
                        new_geo = row[1]
                        name_list = [row[2]]

                        while len(id_list) >= 1:
                            arcpy.AddMessage(str(len(id_list)) + " possible features to merge")
                            value = id_list.pop()
                            arcpy.AddMessage(value)
                            near_vals = geo_dict[value]
                            near_name = near_vals[1]
                            name_list.append(near_name)

                            # Get geometry
                            merged_ids.append(value)
                            merged_id_set = set(merged_ids)
                            near_geo = near_vals[0]
                            new_geo = new_geo.union(near_geo)

                            # Determine if this feature touches any other features
                            if value in near_dict:
                                new_ids = set(near_dict[value])
                                new_ids = new_ids - merged_id_set
                                if obj_id in new_ids:
                                    new_ids.remove(obj_id)
                                if len(new_ids) >= 1:
                                    (str(len(new_ids)) + " added features to merge")
                                    id_list.extend(new_ids)
                            if value in id_list:
                                id_list.remove(value)

                        # Update row with new geometry get a list of unique names - if null and one other then OK to dissolve
                        name_list = set(name_list)
                        if "None" in name_list:
                            name_list.remove("None")

                        if not new_geo.isMultipart and len(name_list) <= 1:
                            arcpy.AddMessage("Enlarging feature " + str(row[0]))
                            delete_ids.extend(merged_ids)
                            row[1] = new_geo
                            cursor.updateRow(row)
                    if obj_id in delete_ids:
                        arcpy.AddMessage("Removing feature " + str(row[0]))
                        cursor.deleteRow()
        else:
            arcpy.AddMessage("No geometries to dissolve.")
        # Delete temp files
        arcpy.management.Delete([f"{working_gdb}\\near_tab", "poly_layer"])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Merge touching features error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def remove_by_converting(primaryFC, secondaryFCs, minimumArea, additionalCriteria, working_gdb):
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        arcpy.env.workspace = arcpy.env.scratchGDB
        arcpy.AddMessage("Scratch : " + str(arcpy.env.scratchGDB))

        # Use Describe object and get Shape Area field
        desc = arcpy.da.Describe(primaryFC)
        area_field = desc['areaFieldName']

        selectionCriteria = area_field + " < " + str(minimumArea)
        # Add additional optional SQL query
        if additionalCriteria:
            selectionCriteria = selectionCriteria + " AND (" + additionalCriteria + ")"

        arcpy.AddMessage(selectionCriteria)

        # Make a layer from the feature class
        primaryFCLyr = "primaryFCLyr"
        arcpy.management.MakeFeatureLayer(primaryFC, primaryFCLyr)

        # Now get info for secondary FC
        secondaryFCNames = []
        secondaryFCLyrs = []
        for fc in secondaryFCs:
            # get just the secondary fc name without path
            indx = str(fc).rfind("\\")
            fcName = str(fc)[indx+1:]
            secondaryFCNames.append(fcName)

            # Create layer
            lyrName = fcName + "Lyr"
            arcpy.management.MakeFeatureLayer(fc, lyrName)
            arcpy.management.SelectLayerByLocation(lyrName, "INTERSECT", primaryFCLyr)
            secondaryFCLyrs.append(lyrName)

        # Convert overlapping features
        selectedFeatures = arcpy.management.SelectLayerByAttribute(primaryFCLyr, "NEW_SELECTION", selectionCriteria)
        if  int(arcpy.management.GetCount(primaryFCLyr)[0]) >= 1:
            arcpy.AddMessage(str(int(arcpy.management.GetCount(primaryFCLyr)[0])) + " features to be processed by overlap")
             # Select only those secondary features that intersect the primary
            for clearFCLyr in secondaryFCLyrs:
                arcpy.management.SelectLayerByLocation(clearFCLyr, "Intersect", selectedFeatures)

            ConvertOverlapping(selectedFeatures, secondaryFCLyrs, working_gdb)

        # Convert enclosed features
        # Get features from primary FC that meet Attribyte criteria - new selection
        selectedFeatures = arcpy.management.SelectLayerByAttribute(primaryFCLyr, "NEW_SELECTION", selectionCriteria)
        if  int(arcpy.management.GetCount(primaryFCLyr)[0]) >= 1:
            arcpy.AddMessage(str(int(arcpy.management.GetCount(primaryFCLyr)[0])) + " features to be processed by enclosed")
            # Select only those secondary features that intersect the primary
            for clearFCLyr in secondaryFCLyrs:
                arcpy.management.SelectLayerByLocation(clearFCLyr, "Intersect", selectedFeatures)

            ConvertEnclosed(selectedFeatures, secondaryFCLyrs, working_gdb)

        arcpy.management.SelectLayerByAttribute(primaryFCLyr, "NEW_SELECTION", selectionCriteria)
        if  int(arcpy.management.GetCount(primaryFCLyr)[0]) >= 1:
            arcpy.AddMessage(str(int(arcpy.management.GetCount(primaryFCLyr)[0])) + " features to be processed")
            arcpy.AddMessage("Deleting Features without replacing")
            arcpy.management.DeleteFeatures(primaryFCLyr)

        # Delete temp files
        clean_list = [primaryFCLyr, secondaryFCNames, secondaryFCLyrs]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Remove by converting error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def extend_polygon_sides(feature_list, working_gdb, minimumLength, minimumWidth, additional_criteria, simplification_tolerance):
    #-------------------------------------------------------------------------------
    # Name:        ResizeBuildings
    # Purpose:

    # FIRST SECTION - IF LENGTH/WIDTH LESS THAN MINIMUM, CREATE POLYGON WITH MINIMUM VALUE
    # 1. Select features by attributes
    # 2. Create mimimum bounding rectangle for the building features
    # 3. Check length and width of this MBG and if less than minimum length/width  , create new geometry
    #    with minimum length/width
    # 4. Update original feature with geometry
    #
    # SECOND SECTION - FILL IN CORNERS OF BUILDING IF SECTIO OF BUILDING IS LESS THAN MINIMUM
    # 1. Select features by attributes
    # 2. Create mimimum bounding rectangle for the building features
    # 3. Run the Symmetrical Difference tool for each mimimum bounding rectangle (may return multipart features)
    # 4. Run the MultipartToSinglepart tool  - so that individual featue extent can be compared
    # 5. From the above output, if side of rectangle is less than minimum length/width then in temp FC :
    #   - append this rectangle to building feature
    #   - select appended features
    #   - run Eliminate tool
    # 4. Update original feature with geometry
    #-------------------------------------------------------------------------------
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        for building_fc in feature_list:
            # Delete list
            delete_list = []

            desc = arcpy.da.Describe(building_fc)
            oidField = desc['OIDFieldName']
            FCName = desc['name']
            # Make a layer from the feature class
            buildingFCLyr = "buildingFCLyr"
            if additional_criteria:
                arcpy.management.MakeFeatureLayer(building_fc, buildingFCLyr , additional_criteria)
            else:
                arcpy.management.MakeFeatureLayer(building_fc, buildingFCLyr)
            delete_list.append(buildingFCLyr)
            # count = int(arcpy.management.GetCount(buildingFCLyr).getOutput(0))
            if has_features(buildingFCLyr):   #Joy added has_features check instead of count check
                # Get mimimum bounding rectangle with "MBG_FIELDS" option - need ORIG_FID, MBG_Width, MBG_Length, MBG_Orientation
                mbgFC = FCName + "_mbg"
                arcpy.management.MinimumBoundingGeometry(buildingFCLyr, mbgFC, "RECTANGLE_BY_WIDTH", "NONE", mbg_fields_option = "MBG_FIELDS")
                # Make feature layer
                mbgFCLyr = str(mbgFC) + "Lyr"
                arcpy.management.MakeFeatureLayer(mbgFC, mbgFCLyr)
                delete_list.append(mbgFC)
                delete_list.append(mbgFCLyr)
                # Create a Describe object from the GDB Feature Class
                desc = arcpy.da.Describe(mbgFC)
                mbg_Width = "MBG_Width"
                mbg_Length = "MBG_Length"
                mbg_Orientation = "MBG_Orientation"
                mbg_OrigFID = "ORIG_FID"
                mbg_OID = desc['OIDFieldName']

                mbg_Width_delim = mbg_Width
                mbg_Length_delim = mbg_Length

                #-------------------------------------------------------------------------------
                # IF LENGTH/WIDTH LESS THAN MINIMUM, CREATE POLYGON WITH MINIMUM VALUE
                #-------------------------------------------------------------------------------
                lstMBGDeleteOIDs = []

                # strange error - have to specify SHAPE@ in this cursor
                # cause desc.shapeFieldName do not work here
                whereClause =f"{mbg_Length_delim} < {minimumLength} or {mbg_Width_delim} < {minimumWidth}"
                with arcpy.da.SearchCursor(mbgFC, [mbg_Width, mbg_Length, mbg_Orientation, mbg_OrigFID, mbg_OID, "SHAPE@"]) as cursor:
                    for row in cursor: # one row
                        mbgGeom_Width = row[0]
                        mbgGeom_Length = row [1]
                        mbgGeom_Orientation = row [2]
                        mbgGeom_OrigFID = row [3]
                        mbgGeom_OID = row [4]
                        mbgGeom_Shape = row[5]

                        if (mbgGeom_Length < minimumLength) or (mbgGeom_Width < minimumWidth):
                            # Get the lines of the polygon
                            # temp_lstSides = arcpy.management.SplitLine(mbgGeom_Shape, f"{working_gdb}\\first_side")  #new script but commeneted out due to slow performance
                            # lstSides = [row[0] for row in arcpy.da.SearchCursor(temp_lstSides, ['SHAPE@'])]  #new script but commeneted out due to slow performance
                            lstSides = arcpy.management.SplitLine(mbgGeom_Shape, arcpy.Geometry()) #eddited as per old script
                            # lstSides will have 4 lines - handle scenario where polygon is PERFECT RECTANGLE
                            # get first 2 sides of rectangle and find the smaller/longer sides
                            if lstSides[0].length < lstSides[1].length:
                                widthPolyline_1 = lstSides[0]
                                lengthPolyline_1 = lstSides[1]
                                widthPolyline_2 = lstSides[2]
                                lengthPolyline_2 = lstSides[3]
                            else:
                                lengthPolyline_1 = lstSides[0]
                                widthPolyline_1 = lstSides[1]
                                lengthPolyline_2 = lstSides[2]
                                widthPolyline_2 = lstSides[3]

                            # Increase length
                            if (mbgGeom_Length < minimumLength):
                                # The orientation angles are in decimal degrees clockwise from north
                                # orientation angles are for longer side of the resulting rectangle (length)
                                # Convert to radians
                                radian = mbgGeom_Orientation * math.pi/180

                                # so, in a right-angled triangle , we know the angle with y-axis, the right angle,  and the hypotenuse.
                                # find the opp side with math.sin
                                # Make sure absolute value of new height and width is used
                                adj_side = math.fabs(minimumLength * math.sin(radian))
                                opp_side = math.fabs(minimumLength * math.cos(radian))
    
                                # Set new values for end points of both lengths
                                vertex1_X = lengthPolyline_1.firstPoint.X
                                vertex1_Y = lengthPolyline_1.firstPoint.Y

                                # In this case, we use lastpoint cause SplitLine lists the lines in order of direction
                                # so we need the lastpoint for calculation
                                vertex4_X = lengthPolyline_2.lastPoint.X
                                vertex4_Y = lengthPolyline_2.lastPoint.Y

                                if (lengthPolyline_1.firstPoint.X > lengthPolyline_1.lastPoint.X):
                                    vertex2_X = vertex1_X - adj_side
                                    vertex3_X = vertex4_X - adj_side
                                else:
                                    vertex2_X = vertex1_X + adj_side
                                    vertex3_X = vertex4_X + adj_side

                                if (lengthPolyline_1.firstPoint.Y > lengthPolyline_1.lastPoint.Y):
                                    vertex2_Y = vertex1_Y - opp_side
                                    vertex3_Y = vertex4_Y - opp_side
                                else:
                                    vertex2_Y = vertex1_Y + opp_side
                                    vertex3_Y = vertex4_Y + opp_side

                            elif (mbgGeom_Width < minimumWidth):
                                # Increase width
                                # orientation angles are for longer side of the resulting rectangle (length)
                                # as rectangle angle is 90 , add 90 to mbgGeom_Orientation,
                                # then subtract from 180 to get angle of width with y-axis

                                # Convert to radians
                                widthAngle = 180 - (mbgGeom_Orientation+90)
                                radian =  widthAngle * math.pi/180

                                adj_side = math.fabs(minimumWidth * math.sin(radian))
                                opp_side = math.fabs(minimumWidth * math.cos(radian))
                                # arcpy.AddMessage("opp_side  adj_side  = {0} {1}".format(str(opp_side),str(adj_side)))

                                # set new values for end points of both lengths
                                vertex1_X = widthPolyline_1.firstPoint.X
                                vertex1_Y = widthPolyline_1.firstPoint.Y

                                # in this case, we use lastpoint cause SplitLine lists the lines in order of direction
                                # so we need the lastpoint for calculation
                                vertex4_X = widthPolyline_2.lastPoint.X
                                vertex4_Y = widthPolyline_2.lastPoint.Y

                                if (widthPolyline_1.firstPoint.X > widthPolyline_1.lastPoint.X):
                                    vertex2_X = vertex1_X - adj_side
                                    vertex3_X = vertex4_X - adj_side
                                else:
                                    vertex2_X = vertex1_X + adj_side
                                    vertex3_X = vertex4_X + adj_side

                                if (widthPolyline_1.firstPoint.Y > widthPolyline_1.lastPoint.Y):
                                    vertex2_Y = vertex1_Y - opp_side
                                    vertex3_Y = vertex4_Y - opp_side
                                else:
                                    vertex2_Y = vertex1_Y + opp_side
                                    vertex3_Y = vertex4_Y + opp_side

                            # Create new geometry
                            newGeomArr = arcpy.Array(arcpy.Point(vertex1_X,vertex1_Y))
                            newGeomArr.append(arcpy.Point(vertex2_X ,vertex2_Y))
                            newGeomArr.append(arcpy.Point(vertex3_X ,vertex3_Y))
                            newGeomArr.append(arcpy.Point(vertex4_X ,vertex4_Y))
                            newGeomArr.append(arcpy.Point(vertex1_X,vertex1_Y))
                            newPoly = arcpy.Polygon(newGeomArr)

                            if (newPoly.area > 0):
                                # Update original feature with new geometry
                                with arcpy.da.UpdateCursor(buildingFCLyr, (oidField,"SHAPE@"), oidField + " = " + str(mbgGeom_OrigFID) ) as updateCursor:
                                    for updtRow in updateCursor:
                                        # Update geometry
                                        updtRow = (updtRow[0], newPoly)
                                        updateCursor.updateRow(updtRow)
                                        # arcpy.AddMessage("Updated geometry for ID {0} ".format(str(updtRow[0])))
                                        # Store mbg OID for deletion later
                                        lstMBGDeleteOIDs.append(mbgGeom_OID)
                    del cursor

                    # delete all MBG whose original feature was resized above
                    # checks list to see if the list is empty before proceeding
                    if len(lstMBGDeleteOIDs) > 0:
                        if len(lstMBGDeleteOIDs) == 1:
                            whereClause = f"{mbg_OID} = {lstMBGDeleteOIDs[0]}"
                        else:
                            whereClause = f"{mbg_OID} IN {tuple(lstMBGDeleteOIDs)}"

                        arcpy.management.SelectLayerByAttribute(mbgFCLyr, "NEW_SELECTION", whereClause)
                        arcpy.management.DeleteFeatures(mbgFCLyr)

                        arcpy.management.SelectLayerByAttribute(mbgFCLyr, "CLEAR_SELECTION")
                    else:
                        arcpy.AddMessage("No buildings in this feature class were enlarged.")

            # Clean up
            arcpy.management.Delete(delete_list)
        
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Resize building error: {e}\nTraceback details:\n{tb}"  
        arcpy.AddError(error_message)


def has_features(fc):
    try:
        with arcpy.da.SearchCursor(fc, ["OID@"]) as cursor:
            return next(cursor, None) is not None  # True if at least one feature #shovon added
    except Exception as e:
        # A broken layer or data source makes SearchCursor fail (often with an
        # unpythonic SystemError). Treat it as unreadable, warn, and move on.
        arcpy.AddWarning(f"Could not read features from '{fc}': {e}")
        return False
    
def has_features_fields_where(fc, fields, where_clause : str) -> bool:
    with arcpy.da.SearchCursor(fc, fields, where_clause) as cursor:
        return next(cursor, None) is not None   #shounok added
    
def has_features_where(fc, where_clause : str) -> bool:
    with arcpy.da.SearchCursor(fc, '*', where_clause) as cursor:
        return next(cursor, None) is not None  #shounok added


# Setting Theme Tool Progressor
def set_theme_progress(label : str, total_steps :int, current_step :int, init_message : str ="Running Tool…", *,
                       init : bool =False, done : bool = False) -> None:
    """
    Initialize once, then only update position/label on subsequent calls.
    - init=True on the first call (or when total_steps changes)
    - done=True on the final call to reset the progressor

    current_step is the absolute step number (0..total_steps).

    Author: Shounok Rahman
    """
    state = getattr(set_theme_progress, "_state", {"initialized": False, "total": None})

    if init or (not state["initialized"]) or (state["total"] != total_steps):
        arcpy.ResetProgressor()
        arcpy.SetProgressor("step", init_message, 0, int(total_steps), 1)
        state["initialized"] = True
        state["total"] = int(total_steps)

    step = max(0, min(int(current_step), int(total_steps)))
    arcpy.SetProgressorLabel(f"{label} ({step}/{state['total']})")
    arcpy.SetProgressorPosition(step)

    if done:
        arcpy.ResetProgressor()
        state = {"initialized": False, "total": None}

    setattr(set_theme_progress, "_state", state)


def is_repair_needed(fc, method="Esri"):
    """
        Check whether a feature class has geometry problems.

        Parameters
        ----------
        fc : str
            Path to the feature class.
        method : str, optional
            Geometry checking method. Options:
            - "OGC"  : stricter (preferred)
            - "ESRI" : Esri's legacy rules

        Returns
        -------
        tuple
            (needs_repair, issues_tbl)
            - needs_repair : bool, True if issues exist
            - issues_tbl   : path to in-memory table containing details
                            (fields include FID, PROBLEM, etc.)
        Author
        ---
        Shounok Rahman
    """
    # write results to in_memory to keep it fast & temporary
    name = arcpy.ValidateTableName(os.path.basename(fc) + "_geomcheck", "in_memory")
    out_tbl = os.path.join("in_memory", name)
    arcpy.management.CheckGeometry(fc, out_tbl, method)
    cnt = int(arcpy.management.GetCount(out_tbl)[0])
    issues = []
    if cnt > 0:
        with arcpy.da.SearchCursor(out_tbl, ["FEATURE_ID", "PROBLEM"]) as cursor:
            issues = [(fid, problem) for fid, problem in cursor]
    # Clean up in_memory table
    arcpy.management.Delete(out_tbl)
    return cnt > 0, issues



def create_map_add_layers(map_name):
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    maps = aprx.listMaps(map_name)
    if(maps):
       maps = maps[0]
    else:
        maps = aprx.createMap(map_name, 'MAP') 
    return maps.name


def import_mapx(mapx_file_name, logger, map_name = "", mapx_dir_name="Mapx"):
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        logger.info("Starting map update process...")
        # === 1️ Remove existing map if present ===
        arcpy.AddMessage("Removing .mapx from current project...")
        current_working_dir = os.getcwd()
        # parent_dir = os.path.dirname(current_working_dir)
        # mapx_path = os.path.join(parent_dir, mapx_dir_name, f"{mapx_file_name}.mapx")
        mapx_path = os.path.join(current_working_dir, f"{mapx_file_name}.mapx")
        existing_maps = [m for m in aprx.listMaps() if map_name != "" and m.name == map_name]
        if existing_maps:
            for m in existing_maps:
                logger.info(f"Removing existing map: {m.name}")
                aprx.deleteItem(m)
            aprx.save()
            logger.info(" Existing map removed.\n")
        else:
            logger.info("No existing map found. Proceeding...\n")

        # === 2️ Import mapx into project ===
        arcpy.AddMessage(f"Importing .mapx ({mapx_path}) into current project...")
        if not os.path.isfile(mapx_path):
            # Without the mapx the theme would run against an empty map and
            # fail (or silently do nothing) much later - stop immediately.
            raise FileNotFoundError(
                f"Required map file not found: {mapx_path}. "
                f"Place '{os.path.basename(mapx_path)}' in the Scripts_50k folder (the tool's working directory), then run again."
            )
        aprx.importDocument(mapx_path)
        arcpy.AddMessage(f"mapx ({mapx_path}) successfully imported into current project")
        logger.info(f"mapx ({mapx_path}) successfully imported into current project")

        imported_maps = [m for m in aprx.listMaps() if map_name != "" and m.name == map_name]
        # arcpy.AddMessage(f"imported maps: {imported_maps}")
        if not imported_maps:
            imported_maps.append(aprx.createMap(map_name, 'MAP'))  
        map_obj = imported_maps[0]
        arcpy.AddMessage(f"Map '{map_name}' imported successfully.")
        logger.info(f"Map '{map_name}' imported successfully.")
        return map_obj
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Mapx import error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        # Re-raise so the run stops: returning None here would only crash later
        # with a confusing AttributeError on imported_map.name.
        raise

def update_mapx_datasource(map_obj, new_gdb, logger, will_open_map_view = False):
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        arcpy.AddMessage("Detecting the old data sources...")
        old_gdb = None
        for lyr in map_obj.listLayers():
            if lyr.isFeatureLayer or lyr.isRasterLayer:
                try:
                    ws = os.path.dirname(lyr.dataSource)
                    if ws.lower().endswith(".gdb"):
                        old_gdb = ws
                        break
                    # For layers inside dataset: e.g. path\my.gdb\topo
                    elif ".gdb" in ws.lower():
                        old_gdb = ws[: ws.lower().find(".gdb") + 4]
                        break
                except Exception:
                    continue

        if not old_gdb:
            # No feature/raster layers in a GDB found.
            # According to current ArcGIS Pro docs, passing "" (or None)
            # as current_connection_info will replace all connection
            # properties with new_workspace_info.
            arcpy.AddMessage(
                "No existing GDB-based feature/raster layers detected; "
                "initializing connection properties with new GDB."
            )
            arcpy.AddMessage(f"Initializing data sources... {new_gdb}")
            map_obj.updateConnectionProperties(
                "",
                new_gdb,
                auto_update_joins_and_relates=True,
                validate=True  # allow setting even if data not created yet
            )
            logger.info("Could not find the old data source for the map. Updating connection properties with new geodatabase.")
            # map_obj.openView()
        else:
            arcpy.AddMessage(f"Detected old GDB: {old_gdb}")
            if old_gdb != new_gdb:  
                arcpy.AddMessage(f"Updating data sources... {new_gdb}")
                map_obj.updateConnectionProperties(
                    current_connection_info=old_gdb,
                    new_connection_info=new_gdb,
                    auto_update_joins_and_relates=True
                )
                for lyr in map_obj.listLayers():
                    if lyr.supports("DATASOURCE"):
                        lyr.updateConnectionProperties(lyr.connectionProperties, new_gdb)
                logger.info(f"Updating Map Data Source Connection properties. Replacing {old_gdb} with {new_gdb}")
                arcpy.AddMessage("Data source has been updated successfully. Proceeding...")
                aprx.save()
            else:
                arcpy.AddMessage(f"As {old_gdb} and {new_gdb} are same, skipping updating connection properties.")
        logger.info("Mapx datasource updated successfully")

        current_working_dir = os.getcwd()
        # parent_dir = os.path.dirname(current_working_dir)
        # updated_mapx = os.path.join(parent_dir, "Mapx", f"updated_{map_obj.name}.mapx")
        updated_mapx = os.path.join(current_working_dir, f"updated_{map_obj.name}.mapx")
        is_updated_mapx_file = os.path.isfile(updated_mapx)
        # Remove the file if exists
        if(is_updated_mapx_file):
            os.remove(updated_mapx) 
        try:
            updated_map_obj = None
            if old_gdb != new_gdb:
                map_obj.exportToMAPX(updated_mapx)
                logger.info(f" Saved updated mapx as: {updated_mapx}")
                aprx.closeViews("MAPS")
                found_maps = aprx.listMaps(map_obj.name)
                if len(found_maps):
                    aprx.deleteItem(found_maps[0])
                aprx.importDocument(updated_mapx)
                imported_maps = [m for m in aprx.listMaps() if map_obj.name != "" and m.name == map_obj.name]
                updated_map_obj = imported_maps[0]
            else:
                updated_map_obj = map_obj
            if(will_open_map_view):
                updated_map_obj.openView()
        except Exception as e:
            logger.warning(f" Could not save mapx copy (non-fatal): {e}")
        aprx.save()
        arcpy.AddMessage("Map updated and project saved successfully!")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Mapx import error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)

def generate_near_and_join_tidal_gate(
    in_features,
    near_features,
    near_table_name,
    logger,
    output_gdb=None,
    search_radius=None,
    location="NO_LOCATION",
    angle="ANGLE",
    closest="CLOSEST",
    closest_count=0,
    method="PLANAR",
    distance_unit="Meters",
):
    """
    Ensures NEAR_DIST and NEAR_ANGLE fields exist in `in_features`,
    generates a near table with `near_features`, joins the table,
    calculates NEAR_DIST and NEAR_ANGLE, then removes the join.
    """
    logger.info(f"in_features are: {in_features}, near_features are: {near_features}")
    # Allow overwriting outputs
    arcpy.env.overwriteOutput = True
    in_feature_fields = arcpy.ListFields(in_features)
    in_feature_field_names =  [fname.name for fname in in_feature_fields]
    # arcpy.AddMessage(f"in_feature_fields: {in_feature_fields}")

    # Set default output_gdb to current workspace if not provided
    if not output_gdb:
        output_gdb = arcpy.env.workspace
        if not output_gdb:
            raise ValueError("No output_gdb provided and no current workspace set.")

    # --- Step 1: Ensure NEAR_DIST and NEAR_ANGLE fields exist ---
    existing_fields = [f.name.upper() for f in in_feature_fields]
    required_fields = [
        ("Near_Dist", "DOUBLE"),
        ("Near_Angle", "DOUBLE")
    ]

    for field_name, field_type in required_fields:
        if field_name.upper() not in existing_fields:
            arcpy.management.AddField(in_features, field_name, field_type)
            logger.info(f"Field '{field_name}' added.")
        else:
            logger.info(f"Field '{field_name}' already exists, skipping add.")

    # --- Step 2: Generate Near Table ---
    near_table = os.path.join(output_gdb, near_table_name)
    arcpy.AddMessage(f"Generating near table: {near_table} and table name: {near_table_name}")
    arcpy.analysis.GenerateNearTable(
        in_features=in_features,
        near_features=near_features,
        out_table=near_table,
        search_radius=search_radius,
        location=location,
        angle=angle,
        closest=closest,
        closest_count=closest_count,
        method=method,
        distance_unit=distance_unit
    )
    
    # --- Step 3: Join Near Table ---
    logger.info(f"Joining near table to {in_features}...")
    fields_to_add = ["NEAR_DIST",  "NEAR_ANGLE"]
    arcpy.management.JoinField(in_features, "OBJECTID", near_table, "IN_FID")
    # --- Step 4: Calculate Fields ---
    logger.info("Calculating NEAR_DIST and NEAR_ANGLE...")
    
    arcpy.management.CalculateField(
        in_table=in_features,
        field="Near_Dist",
        expression=f"!NEAR_DIST_1! / 16",
        expression_type="PYTHON3"
    )
    arcpy.management.CalculateField(
        in_table=in_features,
        field="Near_Angle",
        expression=f"!NEAR_ANGLE_1!",
        expression_type="PYTHON3"
    )
    # #  # Removing DeleteFields for avoiding unnecessary complications
    # # logger.info(f"Deleting Extra fields from {in_features}")
    # # arcpy.management.DeleteField(in_features, in_feature_field_names, method="KEEP_FIELDS")


def apply_layer_definition(fc_layers , defn_query: str, map_name = None):
    """
    Apply a definition query to matching layers in the active map of the current ArcGIS Pro project
    and save the project.

    Parameters
    ----------
    fc_layers : Iterable[str]
        An iterable of layer name strings to which the definition query should be applied.
        Names must match layer names in the active map (case-sensitive in ArcGIS Pro by default).
    defn_query : str
        A SQL expression compatible with the layers' workspace (e.g., file geodatabase or enterprise
        geodatabase). The expression is set as the layer's `definitionQuery`.

    Returns
    -------
    List[str]
        A list of layer names that were successfully found and updated.

    Raises
    ------
    ValueError
        If `fc_layers` is empty, or `defn_query` is an empty string.
    RuntimeError
        If there is no active map in the current ArcGIS Project (e.g., when no map is open).
    arcpy.ExecuteError
        If an ArcPy operation fails internally.


    Author: Shounok Rahman
    Simple modificattion done by Joy
    Examples
    --------
    Apply a filter to multiple layers:

    >>> apply_layer_definition(["BA0010_Residential_Building_A", "BB0010_Commercial_Building_A"], "INVISIBILITY = 0")

    See Also
    --------
    arcpy.mp.ArcGISProject
    arcpy.mp.Map
    arcpy.mp.Layer

    
    """

    if not fc_layers:
        raise ValueError("Parameter 'fc_layers' must contain at least one layer name.")
    if not isinstance(defn_query, str) or not defn_query.strip():
        raise ValueError("Parameter 'defn_query' must be a non-empty SQL string.")

    aprx = arcpy.mp.ArcGISProject("CURRENT")
    map_obj = aprx.activeMap
    if(map_name):
        map_obj = aprx.listMaps(map_name)[0]
    if map_obj is None:
        raise RuntimeError("No active map found in CURRENT ArcGIS Pro project.")

    map_layers = map_obj.listLayers()

    # Exact match
    existing_layers = [ml for ml in map_layers if ml.name in fc_layers]

    if existing_layers:
        arcpy.AddMessage(f"Applying definition query to layers: {[lyr.name for lyr in existing_layers]}")
        arcpy.AddMessage(f"Total definition query applied: {len(existing_layers)}")
    else:
        arcpy.AddWarning(f"No matching layers found for names: {fc_layers}")

    for lyr in existing_layers:
        if lyr.supports("DEFINITIONQUERY"):
            lyr.definitionQuery = defn_query
        else:
            arcpy.AddWarning(f"Layer does not support definition queries: {lyr.name}")

    aprx.save()
    return [lyr.name for lyr in existing_layers]

def get_feature_layer_by_feature_class(feature_class_name, map_name = None):
    """
    Get the Feature Layers from the Active Map by Feature Class BaseName.

    Parameters
    ----------
    feature_class_name : string
        The name of the feature class.
    map_name: string
        The name of the Map

    Returns
    -------
    List<layer_object>
        Returns a list of feature layers by matching feature class names 

    Author: Shounok Rahman
    """
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    active_map = None
    if(map_name):
        map_list = aprx.listMaps(map_name)
        active_map = map_list[0]
    else: 
        active_map = aprx.activeMap
    active_layers = active_map.listLayers(feature_class_name)
    result_layers = []
    for lyr in active_layers:
        if lyr.isGroupLayer:
            pass
        elif (lyr.name == feature_class_name):
            result_layers.append(lyr)
    return result_layers    

# def get_feature_layer_from_map_by_feature_class(feature_class_name, map_name):
#     """
#     Get the Feature Layers from the Active Map by Feature Class BaseName.

#     Parameters
#     ----------
#     feature_class_name : string
#         The name of the feature class.
#     map_name: string
#         The name of the Map

#     Returns
#     -------
#     List<layer_object>
#         Returns a list of feature layers by matching feature class names 

#     Author: Shounok Rahman
#     """
#     aprx = arcpy.mp.ArcGISProject("CURRENT")
#     active_map = aprx.activeMap
#     active_layers = active_map.listLayers(feature_class_name)
#     result_layers = []
#     for lyr in active_layers:
#         if lyr.isGroupLayer:
#             pass
#         elif (lyr.name == feature_class_name):
#             result_layers.append(lyr)
#     return result_layers    

def add_source_tracking(fc):
    """
    Adds and populates persistent source-tracking fields on a feature class.

    Parameters
    ----------
    fc : str
        Path to the feature class to which source-tracking fields will be added.
        The feature class may reside in a file geodatabase or enterprise
        geodatabase.
    
    Fields Added
    ------------
    SRC_FC : TEXT (length 255)
        Stores the base name of the source feature class.

    SRC_OID : LONG
        Stores the original OBJECTID value of each feature at the time this
        function is executed.

    Behavior
    --------
    - Existing fields with the same names are reused and repopulated.
    - All rows in the feature class are updated.
    - Geometry and non-tracking attributes are not modified.

    Notes
    -----
    - OBJECTID values are not preserved through geoprocessing operations such
      as ``Merge``; therefore, this function provides a persistent identifier
      required for safe geometry replacement workflows.

    Author
    --------
    Shounok Rahman
    GIS Specialist, ESRI Bangladesh
    
    Examples
    --------
    >>> add_source_tracking("Residential_Building_A")
    >>> add_source_tracking("Residential_Building_P")

    """
    if "SRC_FC" not in [f.name for f in arcpy.ListFields(fc)]:
        arcpy.management.AddField(fc, "SRC_FC", "TEXT", field_length=255)

    if "SRC_OID" not in [f.name for f in arcpy.ListFields(fc)]:
        arcpy.management.AddField(fc, "SRC_OID", "LONG")

    fc_name = arcpy.Describe(fc).baseName

    with arcpy.da.UpdateCursor(fc, ["SRC_FC", "SRC_OID", "OID@"]) as cursor:
        for row in cursor:
            row[0] = fc_name
            row[1] = row[2]
            cursor.updateRow(row)

def close_active_map_views ( logger ) -> bool:
    maps_closed = False
    aprx = arcpy.mp.ArcGISProject('CURRENT')
    try:
        aprx.closeViews('MAPS')
        logger.info("All active map view has been closed")
        maps_closed = True
    except Exception:
        logger.error("Could not close map views. Please try closing views manually")
    return maps_closed

def resolve_fc_from_fc_list(fc_name, fc_list):
         matches = [fc for fc in fc_list if fc_name in fc]
         return matches[0] if matches else None

def init_layer_name_resolver(excel_path: str) -> None:
    """Call once (e.g., in main.py) to initialize the global resolver."""
    global _LAYER_NAMES
    v = Validator(excel_file=excel_path)  # adjust if your ctor differs
    _LAYER_NAMES = v.get_layer_names("common_layer_names")

def resolve_lyr() -> LayerNames:
    """Zero-arg accessor usable anywhere after initialization."""
    if _LAYER_NAMES is None:
        raise RuntimeError(
            "Layer name resolver not initialized. Call init_layer_name_resolver(excel_path) first."
        )
    return _LAYER_NAMES

def replace_gdb_from_backup(backup_path_edit, working_gdb, logger):

    arcpy.AddMessage("Started replace_gdb_from_backup tool")
    logger.info(f"Started replace_gdb_from_backup tool, replacing {working_gdb} with geodatabase from {backup_path_edit} directory.")
    # Check if backup_path_edit exists
    if not os.path.exists(backup_path_edit):
        arcpy.AddWarning("Backup path does not exist.")
        return
    try:
        file_list = os.listdir(backup_path_edit)
        
        working_gdb_name = os.path.basename(working_gdb)
        
        for file_name in file_list:
            if file_name == working_gdb_name:
                arcpy.AddMessage("File exists in Edit Folder.")
                full_file_path = Path(backup_path_edit) / file_name

                arcpy.management.Delete(working_gdb)
                arcpy.AddMessage("Current GDB deleted")

                shutil.copytree(full_file_path, working_gdb)
                arcpy.AddMessage("Current GDB replaced with GDB from Edit folder.")
                
                # Add the copied geodatabase as database connection in ArcGIS Pro
                try:
                    aprx = arcpy.mp.ArcGISProject("CURRENT")
                    db_list = aprx.databases

                    found = False
                    for db in db_list:
                        if isinstance(db, dict):
                            if working_gdb.lower() in str(db.get('connectionString', '')).lower():
                                # arcpy.AddMessage(f"The working geodatabase is: {working_gdb.lower()} and db is dict.")
                                found = True
                                break
                        elif isinstance(db, str):
                            if working_gdb.lower() == db.lower():
                                # arcpy.AddMessage(f"The working geodatabase is: {working_gdb.lower()} and db is str.")
                                found = True
                                break
                                
                    if not found:
                        db_list.append(working_gdb)
                        aprx.updateDatabases(db_list)
                        logger.info(f"Geodatabase {working_gdb_name} added as database connection.")
                except Exception as e:
                    arcpy.AddWarning(f"Could not add {working_gdb_name} as database connection: {e}")
                    logger.warning(f"Could not add {working_gdb_name} as database connection: {e}")
            else:
                arcpy.AddMessage(f"No GDB replacement required, exiting replace_gdb_from_backup tool")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Contour Cleanup error: {e}\nTraceback details:\n{tb}"
        logger.error(error_message)
        arcpy.AddError(error_message)
        raise


