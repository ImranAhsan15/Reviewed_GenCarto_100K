import arcpy
import traceback
import sys
from common_utils import *

def remove_closed_lines(working_db, input_lines, sql, distance, per, dangles, delete, visible_field, check_connect, connect_angle, comp_lines, val_dict):
    if not comp_lines:
        comp_lines = input_lines

    if delete == 'false' and not visible_field:
        arcpy.AddError("Must populate visibile field")

    percent_parallel = per/val_dict['Hypso_percent_parallel_val']

    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_db
    scratch = working_db
    try:
        arcpy.management.AddGeometryAttributes(input_lines, "LINE_BEARING")
        line_lyr = arcpy.management.MakeFeatureLayer(input_lines, "line_lyr", sql)
        arcpy.AddMessage(str(int(arcpy.management.GetCount(line_lyr)[0] )))
        # Find lines that have dangles
        if dangles == 'true':
            mem = f"{scratch}\\dangle"
            arcpy.management.FeatureVerticesToPoints(line_lyr, mem, "DANGLE")
            values = [str(row[0]) for row in arcpy.da.SearchCursor(mem, ("ORIG_FID"))]
            if arcpy.Exists(mem):
                arcpy.management.Delete(mem)
            arcpy.management.SelectLayerByAttribute(line_lyr, "CLEAR_SELECTION")
            arcpy.AddMessage(str(len(values)) + "Features have dangles")
            if len(values) >= 1:
                hypso_field_name = val_dict['Hypso_field_name']
                # Convert list of Target FID values to a SQL statement
                where = f"{hypso_field_name} = "
                where += f" OR {hypso_field_name} = ".join(values)
                arcpy.management.SelectLayerByAttribute(line_lyr, "New_Selection", where)

        proc_cnt = int(arcpy.management.GetCount(line_lyr)[0])
        arcpy.AddMessage(str(proc_cnt) + " features selected.")

        if proc_cnt > 0:
            arcpy.AddMessage(comp_lines)
            near_dangles = arcpy.analysis.GenerateNearTable(line_lyr, comp_lines, "near_dangles", distance, "NO_LOCATION", "NO_ANGLE", "ALL")

            touching_ids = []
            near_dict = {}
            connect_dict = {}
            connect_ids = []
            # Get a list of the lines close to other lines...
            with arcpy.da.SearchCursor(near_dangles, ["IN_FID", "NEAR_FID", "NEAR_DIST"]) as cursor:
                for row in cursor:
                    if row[2] > 1:
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
                    elif row[2] <= 1:
                        # If this is the first record for that in_fid value
                        if row[0] not in connect_ids:
                            # Add to the touching_ids list and near dictionary
                            connect_ids.append(row[0])
                            connect_dict[row[0]] = [row[1]]
                        # If this is not the first record
                        else:
                            # Updated the dictionary to add the new near id
                            cur_list = connect_dict[row[0]]
                            cur_list.append(row[1])
                            connect_dict[row[0]] = cur_list

            arcpy.AddMessage("Near Cnt " + str(len(touching_ids)))
            arcpy.AddMessage("Getting comparison geometries")
            geo_dict = {}
            with arcpy.da.SearchCursor(comp_lines, ['OID@', 'shape@', 'BEARING']) as cursor:
                for row in cursor:
                    geo_dict[row[0]] = [row[1], row[2]]
            line_ignore = []
            length_field = arcpy.da.Describe(input_lines)['lengthFieldName']
            order = f"{val_dict['Hypso_order_by']} {length_field}"
            if delete == 'false':
                fields = [length_field, "OID@", "shape@", "BEARING", visible_field]
            else:
                fields = [length_field, "OID@", "shape@", "BEARING"]

            hidden_list = []
            keep_list = []
            to_hide = []
            with arcpy.da.UpdateCursor(line_lyr, fields, sql_clause=(None, order)) as cursor:
                for row in cursor:
                    obj_id = row[1]
                    geom = row[2]
                    angle = row[3]
                    if not angle:
                        angle = 0
                    if geom.length >= distance:
                        if obj_id in touching_ids:
                            arcpy.AddMessage("Process " + str(obj_id))
                            # Determine what mid points the line is near, and then which line
                            if obj_id in to_hide:
                                to_hide.remove(obj_id)
                                # If the feature has near features but isn't hidden
                                # consider hiding it if touchin a feature that is hidden
                                if delete == 'false':
                                    row[4] = 1
                                    cursor.updateRow(row)
                                else:
                                    cursor.deleteRow()
                                hidden_list.append(obj_id)
                                line_ignore.append(obj_id)
                                arcpy.AddMessage("hiding near connected" + str(obj_id))
                            else:
                                near_list = near_dict[obj_id]
                                # If the line is not already hidden, determine if hide this line
                                for near in near_list:
                                    if near not in hidden_list and near != obj_id and obj_id not in hidden_list and near not in to_hide:
                                        # buffer the nearby geometry and determine
                                        # how much of the feature geometry falls
                                        # within the buffer
                                        [near_geo, near_angle] = geo_dict[near]

                                        near_buffer = near_geo.buffer(distance)
                                        include_geo = geom.intersect(near_buffer, 2)
                                        # If more than the specified percent is within the buffer
                                        if ((include_geo.length/geom.length) >= percent_parallel):
                                            arcpy.AddMessage("Near " + str(near))
                                            if delete == 'false':
                                                row[4] = 1
                                                cursor.updateRow(row)
                                            else:
                                                cursor.deleteRow()
                                            hidden_list.append(obj_id)
                                            line_ignore.append(obj_id)
                                            arcpy.AddMessage("hiding near " + str(obj_id))

                                # If line was hidden, find other lines at same angle that should likely also be hidden.
                                if check_connect == 'true':
                                    append_ids = []
                                    keep_looping = True
                                    id_val = obj_id

                                    if obj_id in hidden_list and obj_id in connect_dict:
                                        while keep_looping:
                                            arcpy.AddMessage("test id " + str(id_val))
                                            if id_val in append_ids:
                                                append_ids.remove(id_val)
                                            # Get a list of all the features this one is connected to
                                            if id_val in connect_dict:
                                                connections = connect_dict[id_val]
                                                arcpy.AddMessage("Connected to " + str(len(connections)) + " features")
                                                for connect_id in connections:
                                                    # if the connecting feature also has a near feature
                                                    # and hasn't already been set to_hide
                                                    if connect_id not in to_hide:
                                                        # determine if the feature has the same angle
                                                        # as a hidden feature (+ or - 2 degrees)
                                                        [near_geo, near_angle] = geo_dict[connect_id]
                                                        angle_low = angle - connect_angle
                                                        angle_high = angle + connect_angle

                                                        if angle_low <= near_angle <= angle_high:
                                                            arcpy.AddMessage("Connected to: " + str(connect_id))
                                                            # If so add to the list of feature to hide
                                                            to_hide.append(connect_id)
                                                            append_ids.append(connect_id)

                                            if len(append_ids) >= 1:
                                                keep_looping = True
                                                id_val = append_ids.pop()
                                            else:
                                                keep_looping = False

                    if obj_id not in hidden_list:
                        keep_list.append(obj_id)

            if len(to_hide) >= 1:
                arcpy.AddMessage("Delete final connected features")
                with arcpy.da.UpdateCursor(line_lyr, fields) as cursor:
                    for row in cursor:
                        obj_id = row[1]
                        if obj_id in to_hide:
                            to_hide.remove(obj_id)
                            # if the feature has near features but isn't hidden
                            # consider hiding it if touchin a feature that is hidden
                            if delete == 'false':
                                row[4] = 1
                                cursor.updateRow(row)
                            else:
                                cursor.deleteRow()
                            hidden_list.append(obj_id)
                            line_ignore.append(obj_id)
                            arcpy.AddMessage("hiding near connected" + str(obj_id))

            arcpy.AddMessage(str(len(hidden_list)) + " features were hidden or deleted")
 
        # Clean up
        clean_list = [f"{scratch}\\near_dangles", "line_lyr", "input_lines", "del_lyr"]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete close lines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def thin_cuttings_and_embankments(working_gdb, fc_list, distance, minimum_length, percent_parallel, val_dict):
    # Set environment
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = 1
    arcpy.AddMessage(f"{working_gdb}")
    try:
        # Get the feature classes from the workspace
        embankment_fc = [fc for fc in fc_list if resolve_lyr().Embankment_L in fc][0]
        cutting_fc = [fc for fc in fc_list if resolve_lyr().Cutting_L in fc][0]
        # start here of additional lines for 100k from below 100k_TCE
        cliff_precipitous = [fc for fc in fc_list if resolve_lyr().Cliff_Precipitous_L in fc][0]
        # end here of additional lines for 100k_TCE
        # add unit to distance
        distance_str =  f"{distance} Meters"
        fc_name_em = arcpy.da.Describe(embankment_fc)["name"]
        # Simplify the geometry of the feature class and remove closed lines
        embankment_simplified = f"{working_gdb}\\{fc_name_em}_simplified"
        arcpy.cartography.SimplifyLine(embankment_fc, embankment_simplified, "BEND_SIMPLIFY", distance_str, "FLAG_ERRORS", "KEEP_COLLAPSED_POINTS", "NO_CHECK", None, "NO_CHECK")
        arcpy.AddMessage(f"Simplified {embankment_fc} to {embankment_simplified}")
        # Remove closed lines from the simplified feature class
        remove_closed_lines(working_gdb, embankment_simplified, None, distance, percent_parallel, val_dict['Hypso_dangles'], val_dict['Hypso_delete'], val_dict['Hypso_visible_field'], val_dict['Hypso_check_connect'], val_dict['Hypso_connect_angle'], None, val_dict)
        # Fc name
        fc_name_cut = arcpy.da.Describe(cutting_fc)["name"]
        cutting_simplified = f"{working_gdb}\\{fc_name_cut}_simplified"
        arcpy.cartography.SimplifyLine(cutting_fc, cutting_simplified, "BEND_SIMPLIFY", distance_str, "FLAG_ERRORS", "KEEP_COLLAPSED_POINTS", "NO_CHECK", None, "NO_CHECK")
        arcpy.AddMessage(f"Simplified {cutting_fc} to {cutting_simplified}")
        remove_closed_lines(working_gdb, cutting_simplified, None, distance, percent_parallel, val_dict['Hypso_dangles'], val_dict['Hypso_delete'], val_dict['Hypso_visible_field'], val_dict['Hypso_check_connect'], val_dict['Hypso_connect_angle'], None, val_dict)
        
        # start here of additional lines for 100k from below 100k_TCE
        # cliff_precipitous Fc name
        fc_name_cut_cliff_precipitous = arcpy.da.Describe(cliff_precipitous)["name"]
        cliff_precipitous_simplified = f"{working_gdb}\\{fc_name_cut_cliff_precipitous}_simplified"
        arcpy.cartography.SimplifyLine(cliff_precipitous, cliff_precipitous_simplified, "BEND_SIMPLIFY", distance_str,
                                       "FLAG_ERRORS", "KEEP_COLLAPSED_POINTS", "NO_CHECK", None, "NO_CHECK")
        arcpy.AddMessage(f"Simplified {cliff_precipitous} to {cliff_precipitous_simplified}")
        remove_closed_lines(working_gdb, cliff_precipitous_simplified, None, distance, percent_parallel, val_dict['Hypso_dangles'], val_dict['Hypso_delete'], val_dict['Hypso_visible_field'], val_dict['Hypso_check_connect'], val_dict['Hypso_connect_angle'], None, val_dict)

        # Remove closed lines from the simplified cutting feature class comparing to the embankment feature class
        remove_closed_lines(working_gdb, cutting_simplified, None, distance, percent_parallel, val_dict['Hypso_dangles'], val_dict['Hypso_delete'], val_dict['Hypso_visible_field'], val_dict['Hypso_check_connect'], val_dict['Hypso_connect_angle'], embankment_simplified, val_dict)
        arcpy.AddMessage(f"Removed closed lines from {cutting_simplified} comparing to {embankment_simplified}")

        # Feature layer creation, feature deleted from main fc and feature append
        area_field = arcpy.da.Describe(embankment_fc)["lengthFieldName"]
        expression = f"{area_field} > {minimum_length} AND ( INVISIBILITY = 0 OR INVISIBILITY IS NULL )"
        arcpy.AddMessage(f"{area_field}")
        arcpy.AddMessage(f"{minimum_length}")
        arcpy.AddMessage(f"{expression}")


        arcpy.AddMessage(f"Deleting features")
        embankment_lyr = arcpy.management.MakeFeatureLayer(embankment_simplified, 'embankment_lyr', expression)
        arcpy.management.DeleteFeatures(embankment_fc)
        arcpy.management.Append(embankment_lyr, embankment_fc, "NO_TEST")

        cutting_lyr = arcpy.management.MakeFeatureLayer(cutting_simplified, 'cutting_lyr', expression)
        arcpy.management.DeleteFeatures(cutting_fc)
        arcpy.management.Append(cutting_lyr, cutting_fc, "NO_TEST")

        cliff_precipitous_lyr = arcpy.management.MakeFeatureLayer(cliff_precipitous_simplified, 'cliff_precipitous_lyr',
                                                                  expression)
        arcpy.management.DeleteFeatures(cliff_precipitous)
        arcpy.management.Append(cliff_precipitous_lyr, cliff_precipitous, "NO_TEST")
        # end here of additional lines for 100k_TCE

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Error in thin_cuttings_and_embankments function: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def smooth_contours(fc_list, working_gdb, smoothing_tolerance):
    # Set the workspace
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True

    try:
        contour_fc = [fc for fc in fc_list if resolve_lyr().Contour_Line_L in fc][0]
        # Make a feature layer
        layer_name = "contour_line_lyr"
        arcpy.management.MakeFeatureLayer(contour_fc, layer_name)
        # Output path for smoothed contours
        smooth_output = f"{working_gdb}\\smooth_contour"
        # Smooth line
        arcpy.AddMessage("Smoothing contour lines...")
        arcpy.cartography.SmoothLine(layer_name, smooth_output, "PAEK", smoothing_tolerance, "FIXED_CLOSED_ENDPOINT")
        # Delete original features
        arcpy.AddMessage("Deleting original contour features...")
        arcpy.management.DeleteFeatures(contour_fc)
        # Append smoothed features back
        arcpy.AddMessage("Appending smoothed contours back...")
        arcpy.management.Append(smooth_output, contour_fc, "NO_TEST")
        arcpy.AddMessage("Contour smoothing complete.")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Smooth contours error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def enlarge_hypso_polygons(working_gdb, fc_list, minimum_size_m, minimum_size_m2, increase):
    try:
        arcpy.env.overwriteOutput = True
        # Get feature classes
        mines_fc = [fc for fc in fc_list if resolve_lyr().Mine_A in fc][0]
        rock_fc = [fc for fc in fc_list if resolve_lyr().Rock_Outcrop_A in fc][0]
        geoscience_fc_list = [fc for fc in fc_list for fcs in [ resolve_lyr().Quarry_Pit_A, resolve_lyr().Geohazard_Site_A, resolve_lyr().Landslide_Site_A, resolve_lyr().Mud_Volcano_A ]  if fcs in fc]
        barrier_fcs = []
        sql = None
        intersect_fc = None
        # Rock
        enlarge_polygon_barrier(rock_fc, sql, intersect_fc, str(minimum_size_m), increase, barrier_fcs, working_gdb)
        # Mine
        enlarge_polygon_barrier(mines_fc, sql, intersect_fc, str(minimum_size_m), increase, barrier_fcs, working_gdb)
        # Geoscience
        for geoscience_fc in geoscience_fc_list:
            enlarge_polygon_barrier(geoscience_fc, sql, intersect_fc, str(minimum_size_m2), increase, barrier_fcs, working_gdb)
 
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Error in enlarging polygons: {str(e)} \nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def dissolve_touching_polygons(fc_list, working_gdb, dissolve_field):
    # Set environment
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True
    # Get required feature Class
    mines_fc = [fc for fc in fc_list if resolve_lyr().Mine_A in fc][0]
    rock_fc = [fc for fc in fc_list if resolve_lyr().Rock_Outcrop_A in fc][0]
    geoscience_fc_list = [fc for fc in fc_list for fcs in [resolve_lyr().Quarry_Pit_A, resolve_lyr().Geohazard_Site_A, resolve_lyr().Landslide_Site_A, resolve_lyr().Mud_Volcano_A]  if fcs in fc]

    try:
        sql=None

        # Mine
        merge_touching_features_new(mines_fc, sql, dissolve_field, working_gdb)
        # Rock
        merge_touching_features_new(rock_fc, sql, dissolve_field, working_gdb)
        # Geoscience
        for geoscience_fc in geoscience_fc_list:
            merge_touching_features_new(geoscience_fc, sql, dissolve_field, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Dissolve touching polygons error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def erase_veg_hypso(fc_list, working_gdb, hypso_compare_features, logger):
    # Set Environment
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True

    mines_fc = [fc for fc in fc_list if resolve_lyr().Mine_A in fc][0]
    rock_fc = [fc for fc in fc_list if resolve_lyr().Rock_Outcrop_A in fc][0]
    geoscience_fc_list = [fc for fc in fc_list for fcs in [ resolve_lyr().Quarry_Pit_A, resolve_lyr().Geohazard_Site_A, resolve_lyr().Landslide_Site_A, resolve_lyr().Mud_Volcano_A]  if fcs in fc]

    hypso_compare_features = list(filter(str.strip, hypso_compare_features))
    hypso_compare_features = [fc for a_lyr in hypso_compare_features for fc in fc_list if str(a_lyr) in fc]

    try:
        sql = None
        # Mine
        erase_polygons_by_replace(mines_fc, hypso_compare_features, sql, working_gdb)
        # Rock
        erase_polygons_by_replace(rock_fc, hypso_compare_features, sql, working_gdb)
        # Geoscience
        for geoscience_fc in geoscience_fc_list:
            erase_polygons_by_replace(geoscience_fc, hypso_compare_features, sql, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Hypso erase vegetation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)

def calculate_contoure_line_type(fc_list, working_gdb, logger) -> None:
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    contour_l_fc = resolve_lyr().Contour_Line_L
    if(contour_l_fc):
        contour_l_fc = [fc for fc in fc_list if contour_l_fc in fc][0]
    lyr_name = "contours_lyr"
        
    # Make a feature layer (required for selection-based workflows)
    arcpy.management.MakeFeatureLayer(contour_l_fc, lyr_name)
    # 1) CLI = 0  -> CLT = 0
    arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "CLI = 0")
    arcpy.management.CalculateField(lyr_name, "CLT", 0, "PYTHON3")

    # 2) CLI IN (1,2,3) -> CLT = 9
    arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "CLI IN (1, 2, 3)")
    arcpy.management.CalculateField(lyr_name, "CLT", 9, "PYTHON3")

    
    # 3) CLI IN (4,5,6,7) -> CLT = 8
    arcpy.management.SelectLayerByAttribute(lyr_name, "NEW_SELECTION", "CLI IN (4, 5, 6, 7)")
    arcpy.management.CalculateField(lyr_name, "CLT", 8, "PYTHON3")

    # Clear selection
    arcpy.management.SelectLayerByAttribute(lyr_name, "CLEAR_SELECTION")

    logger.info(f"Calculating Contour Line Type was successful for {contour_l_fc}")

    return None

# # Hypsography Generalization
def gen_hypsography(fc_list, hypso_compare_features, val_dict, working_gdb, logger):
    arcpy.AddMessage('Starting hypsography features generalization.....')
    # Set Environment
    arcpy.env.overwriteOutput = True
    try:
        # Thin Cuttings and Embankments
        thin_cuttings_and_embankments(working_gdb, fc_list, val_dict['Hypso_distance'], val_dict['Hypso_minimum_length'], val_dict['Hypso_parallel_percent'], val_dict)
        # Smooth contour
        smooth_contours(fc_list, working_gdb, val_dict['Hypso_smoothing_tolerance'])
        # Enlarge hypso polygons
        enlarge_hypso_polygons(working_gdb, fc_list, val_dict['Hypso_minimum_size'], val_dict['Hypso_miximum_size'], val_dict['Hypso_increase_factor'])
        # Dissolve hypso polygons
        dissolve_touching_polygons(fc_list, working_gdb, val_dict['Hypso_dissolved_field'])
        # Erase vegetation hypso
        erase_veg_hypso(fc_list, working_gdb, hypso_compare_features, logger)
        # # Calculate Countour Line Type from Contour Line Index
        calculate_contoure_line_type(fc_list, working_gdb, logger)
      
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info() 
        tb = traceback.format_exc()
        error_message = f"Hypsography generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Hypsography generalisation', f'{exc_value}\n')