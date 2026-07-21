import arcpy
import traceback
import sys
from common_utils import *

def merge_explode(poly_layer, field, working_gdb):
    try:
        deleteCount = 0
        # In memory layer to hold aggregation of polygons
        mem = f"{working_gdb}\\aggr"
        # Dissolve
        arcpy.analysis.PairwiseDissolve(poly_layer, mem, field)

       # Unable to get geom list, using Search Cursor to access shape of in_memory feature
        with arcpy.da.SearchCursor(mem, ["SHAPE@"]) as mem_cur:
            mem_row = mem_cur.next()
            geom = mem_row[0]

        if arcpy.Exists(f"{working_gdb}\\aggr"):
            arcpy.management.Delete(f"{working_gdb}\\aggr")
        return geom
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Merge explode error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def offset_xy(pnt_data, line_data, orient_fld, offset_dist, perpendicular, working_gdb):
    # Environment variables
    arcpy.env.overwriteOutput = 1
    # Offset distance constant
    RIGHTANGLE = 90.0

    arcpy.AddMessage(str(RIGHTANGLE))
    # Offset fields
    x_offset_fld = "OFFSETX"
    y_offset_fld = "OFFSETY"

    try:
        # Make feature layer from line data
        arcpy.management.MakeFeatureLayer(line_data, "lines")
        # Loop through each point feature
        with arcpy.da.UpdateCursor(pnt_data, ["OID@", "SHAPE@", x_offset_fld, y_offset_fld, orient_fld]) as cur:
            for row in cur:
                # Get field values
                oid, shp = row[0], row[1]

                arcpy.AddMessage(str(oid))

                # Find line feature that intersects current point
                where = arcpy.AddFieldDelimiters(pnt_data, "OBJECTID") + " = " + str(oid)
                arcpy.management.MakeFeatureLayer(pnt_data, "pnt", where)
                arcpy.management.SelectLayerByLocation("lines", "INTERSECT", "pnt")
                # Check if point intersection was found
                if int(arcpy.management.GetCount("lines").getOutput(0)) > 0:
                    # Split line into segments at vertices to find segment within line that intersects point
                    segments = arcpy.management.SplitLine("lines", f"{working_gdb}\\segments")
                    segments = [row[0] for row in arcpy.da.SearchCursor(segments, ['SHAPE@'])]
                    for polyline in segments:

                        if not polyline.disjoint(shp):
                            # Get angle of line segment in radians
                            delta_y = polyline.lastPoint.Y - polyline.firstPoint.Y
                            delta_x = polyline.lastPoint.X - polyline.firstPoint.X

                            angle = math.degrees(math.atan2(delta_y, delta_x))
                            arcpy.AddMessage("Orig " + str(angle))
                            angle = math.degrees(math.atan2(delta_y, delta_x))

                            if not angle:
                                angle = 0

                            if angle < 0:
                                angle = 360 + angle

                            arcpy.AddMessage(str(angle))
                            # Calculate angle of offset
                            offset_angle = abs(RIGHTANGLE - abs(angle))
                            # Calculate x and y offset
                            x_offset = abs(math.cos(math.radians(offset_angle)) * offset_dist)
                            y_offset = abs(math.sin(math.radians(offset_angle)) * offset_dist)
                            # Get coordinates of offset point based on quadrant angle falls in
                            if angle >= 0 and angle < 90:
                                # 1st quad
                                new_x = -x_offset
                                new_y = y_offset
                            elif angle >= 90 and angle < 180:
                                # 2nd quad
                                new_x = - x_offset
                                new_y = - y_offset
                            elif angle >= 180 and angle < 270:
                                # 3rd quad
                                new_x = x_offset
                                new_y = - y_offset
                                # 4th quad

                            else:
                                # 4th quad
                                new_x = x_offset
                                new_y = y_offset

                            # Update fields with calculated x and y offset values
                            arcpy.AddMessage("offset X " + str(new_x))
                            arcpy.AddMessage("offset Y " + str(new_y))
                            if perpendicular == 'TRUE':
                                row[2] = -new_x
                                row[3] = -new_y
                                angle = angle - RIGHTANGLE
                            else:
                                row[2] = new_x
                                row[3] = new_y

                            row[4] = angle
                            cur.updateRow(row)
                            # Found line segment that intersects - break out of for loop
                            break
                else:
                    arcpy.AddMessage("No intersection at Point: " + str(oid))
                    arcpy.AddWarning("No intersection at Point: " + str(oid))

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"offset_xy error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def explode_remove_dissolve(polygon_fc, min_area, working_gdb):
    try:
        # Define environment variables
        arcpy.env.overwriteOutput = 1
        arcpy.env.workspace = working_gdb

        clean_list = []
        delete_count = 0

        # --- determine query for selecting features ---
        # Find area field
        desc = arcpy.da.Describe(polygon_fc)
        area_field = desc['areaFieldName']
        # Query for features smaller than minimum size
        small_query = f"{area_field} < {min_area}"

        orig_ids = [row[0] for row in arcpy.da.SearchCursor(polygon_fc, 'OID@')]
        explode_features = arcpy.management.MultipartToSinglepart(polygon_fc, "Single_Part")
        explode_ids = [row[0] for row in arcpy.da.SearchCursor(explode_features, "OID@")]
        aggrigate_features = arcpy.management.MakeFeatureLayer(explode_features, "aggrigate_features")
        clean_list.append(aggrigate_features)
        arcpy.management.SelectLayerByAttribute(aggrigate_features, "NEW_SELECTION", small_query)
        dissolve_ids = [row[0] for row in arcpy.da.SearchCursor(aggrigate_features, "ORIG_FID")]

        result = arcpy.management.GetCount(aggrigate_features)
        count = int(result.getOutput(0))

        if count > 0:

            arcpy.management.DeleteFeatures(aggrigate_features)
            arcpy.management.SelectLayerByAttribute(aggrigate_features, "CLEAR_SELECTION")

            remain_ids = [row[0] for row in arcpy.da.SearchCursor(aggrigate_features, "ORIG_FID")]
            remain_oids = [row[0] for row in arcpy.da.SearchCursor(aggrigate_features, "OID@")]

            hide_ids = []
            for orig in orig_ids:
                if orig not in remain_ids:
                    hide_ids.append(orig)

            split_ids = []
            for orig in explode_ids:
                if orig not in remain_oids:
                    split_ids.append(orig)

            arcpy.AddMessage(str(dissolve_ids))

            with arcpy.da.UpdateCursor(polygon_fc, ['OID@', 'INVISIBILITY', 'SHAPE@']) as cursor:
                for u_row in cursor:
                    if u_row[0] in hide_ids:
                        arcpy.AddMessage("Hiding feature because all parts too small " + str(u_row[0]))
                        u_row[1] = 1
                        cursor.updateRow(u_row)
                    elif u_row[0] in dissolve_ids:
                        arcpy.AddMessage("Recreating multi-part geometry " + str(u_row[0]))
                        query = "ORIG_FID = " + str(u_row[0])
                        arcpy.management.SelectLayerByAttribute(aggrigate_features, "NEW_SELECTION", query)
                        u_row[2] = merge_explode(aggrigate_features, "ORIG_FID", working_gdb)
                        cursor.updateRow(u_row)
        else:
            arcpy.AddMessage("No features have small parts to be removed")

        arcpy.AddMessage("Deleted " + str(delete_count) + " features.")

        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Explode remove dissolve error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def trim_line_within_distance(input_line, visible_field, distance, min_length, ref_scale, erase, compare_features, working_gdb):
    """Find polygons within 12.5 Meters of each other with different names. Trim larger polygon so that they are 12.5 Meters apart."""
    # Environment variables
    arcpy.env.overwriteOutput = 1
    clean_list = []

    try:
        len_numb = min_length
        doub_dist = f"{(distance) * 2} Meters"

        # Only include polys that have a name or have an area greater than 2,500 meters
        # Create where clause
        where = (visible_field + " = 0 OR " + visible_field + " IS NULL")
        line_layer = arcpy.management.MakeFeatureLayer(input_line, "line_lyr", where)
        clean_list.append("line_lyr")
        desc = arcpy.da.Describe(input_line)
        spat_ref = desc['spatialReference']
        fc_name = desc['name']
        arcpy.management.RepairGeometry(input_line)
        arcpy.AddMessage("Getting outlines")

        line_layer = arcpy.management.MakeFeatureLayer(input_line, "line_lyr", where)
        clean_list.append(line_layer)

        conflict_areas = []
        for compare in compare_features:
            desc = arcpy.da.Describe(str(compare))
            comp_name = desc['name']
            arcpy.AddMessage("Finding conflicts with " + comp_name)
            comp_lyr = arcpy.management.MakeFeatureLayer(compare, "comp_lyr", where)

            # Find conflicts between symbols
            arcpy.management.SelectLayerByLocation(comp_lyr, "INTERSECT", line_layer, doub_dist)
            if int(arcpy.management.GetCount(comp_lyr)[0]) >= 1:
                conflicts = arcpy.cartography.FeatureOutlineMasks(comp_lyr, "conflicts_" + comp_name, ref_scale, spat_ref, distance)
                conflict_areas.append(conflicts)
            arcpy.management.Delete("comp_layer")

        if len(conflict_areas) >= 1:
            geom_dict = {}
            dissolve_field = "FID_" + fc_name
            arcpy.AddMessage("Dissolving conflicts")

            # Classic Union under parallelProcessingFactor can intermittently
            # fail with "The table was not found" - run it single-threaded.
            with arcpy.EnvManager(parallelProcessingFactor="0"):
                union = arcpy.analysis.Union(conflict_areas, "conflict_union", "ONLY_FID")
            diss_single = arcpy.management.MultipartToSinglepart(union, "dissolve_single")
            clean_list.extend([union, diss_single])

            dissolve_layer = arcpy.management.MakeFeatureLayer(diss_single)
            clean_list.append(dissolve_layer)
            dissolve = arcpy.analysis.PairwiseDissolve(dissolve_layer, "dissolved_conflicts_final", multi_part="SINGLE_PART")
            arcpy.management.RepairGeometry(dissolve)
            clean_list.append(dissolve)

            arcpy.AddMessage("Isolate conflicts")

            intersect = arcpy.analysis.Identity(line_layer, dissolve, "erase_areas", "ONLY_FID" )
            intersect_single = arcpy.management.MultipartToSinglepart(intersect, "intersect_single")
            clean_list.append(intersect)
            clean_list.append(intersect_single)

            arcpy.AddMessage("Aligning conflict edges")
            int_lyr = arcpy.management.MakeFeatureLayer(intersect_single, "intersect_layer_1")
            clean_list.append(int_lyr)
            where2 = "FID_dissolved_conflicts_final <> -1"
            arcpy.management.SelectLayerByAttribute(int_lyr, "NEW_SELECTION", where2)
            arcpy.edit.Densify(int_lyr, "DISTANCE", distance)
            doub_dist = f"{str(float(distance) * 3)} Meters"
 
            snap_env = [dissolve, "EDGE", doub_dist]
            arcpy.edit.Snap(int_lyr, [snap_env])
            arcpy.edit.ExtendLine(int_lyr, distance)

            if erase == "ERASE_INPUT":
                arcpy.AddMessage("Determining where conflicts remain")
                neg_buff = arcpy.analysis.PairwiseBuffer(dissolve, f"{working_gdb}\\neg_buff", "-1 Meters")
                clean_list.append(neg_buff)
                lines_new = arcpy.analysis.PairwiseErase(intersect_single, neg_buff, "final_input_line")
                clean_list.append(lines_new)
            else:
                lines_new = intersect_single

            # get final geometries
            arcpy.management.RepairGeometry(lines_new)
            line_final = arcpy.analysis.PairwiseDissolve(lines_new, "out_line_final", dissolve_field,multi_part="SINGLE_PART")
            clean_list.append(line_final)

            update_geo_dict = {}
            with arcpy.da.SearchCursor(line_final, [dissolve_field, 'SHAPE@']) as cur:
                for row in cur:
                    geo = row[1]
                    oid = row[0]

                    if geo.length >= float(len_numb):
                        if oid not in update_geo_dict:
                            update_geo_dict[oid] = geo
                        else:
                            cur_geo = update_geo_dict[oid]
                            cur_geo.union(geo)
                            update_geo_dict[oid] = cur_geo

            arcpy.AddMessage("Updating with final geometry")
            with arcpy.da.UpdateCursor(input_line, ['OID@', 'SHAPE@', visible_field]) as ucur:
                for urow in ucur:
                    if urow[0] in update_geo_dict:
                        urow[1] = update_geo_dict[urow[0]]
                    else:
                        urow[2] = 2
                    ucur.updateRow(urow)
        # Delete temp files
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Trim line within distance error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def offset_kilometer_post(fc_list, road_query, orient_fld, offset_dist_l, offset_dist_u, perpendicular, working_gdb, snap_dist):
    dynamic_fc_names = resolve_lyr()
    try:
        # Set Environment
        arcpy.env.overwriteOutput = 1
        # Get feature classes
        road = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        feature_layer_rd = arcpy.management.MakeFeatureLayer(road, "feature_layer_rd")
        kilometer_post = [fc for fc in fc_list if dynamic_fc_names.Kilometer_Post_P in fc][0]
        feature_layer_kilo = arcpy.management.MakeFeatureLayer(kilometer_post, "feature_layer_kilo")
        # Snapping kilometer post feature
        snap_env = [feature_layer_rd, "EDGE", snap_dist]
        arcpy.edit.Snap(feature_layer_rd, [snap_env])

        single_carriage_hwy = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[0])
        single_carriage_road = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[1])
        dual_carriage_hwy = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[2])
        dual_carriage_road = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[3])

        # Select by location
        sel_single_carriage_hwy = arcpy.management.SelectLayerByLocation(feature_layer_kilo, "INTERSECT", single_carriage_hwy, "", "NEW_SELECTION")
        sel_single_carriage_road = arcpy.management.SelectLayerByLocation(feature_layer_kilo, "INTERSECT", single_carriage_road, "", "NEW_SELECTION")
        sel_dual_carriage_hwy = arcpy.management.SelectLayerByLocation(feature_layer_kilo, "INTERSECT", dual_carriage_hwy, "", "NEW_SELECTION")
        sel_dual_carriage_road = arcpy.management.SelectLayerByLocation(feature_layer_kilo, "INTERSECT", dual_carriage_road, "", "NEW_SELECTION")
        # Calculation orient degree
        offset_xy(sel_single_carriage_hwy, feature_layer_rd, orient_fld, offset_dist_l, perpendicular, working_gdb)
        offset_xy(sel_single_carriage_road, feature_layer_rd, orient_fld, offset_dist_l, perpendicular, working_gdb)
        offset_xy(sel_dual_carriage_hwy, feature_layer_rd, orient_fld, offset_dist_l, perpendicular, working_gdb)
        offset_xy(sel_dual_carriage_road, feature_layer_rd, orient_fld, offset_dist_u, perpendicular, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Offset kilometer post error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def offset_benckmark(fc_list, road_query, bench_query, orient_fld, offset_dist_l, offset_dist_u, perpendicular, working_gdb, snap_dist):
    dynamic_fc_names = resolve_lyr()
    try:
        # Set Environment
        arcpy.env.overwriteOutput = 1
        # Get feature classes
        road = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        feature_layer_rd = arcpy.management.MakeFeatureLayer(road, "feature_layer_rd")
        height_point = [fc for fc in fc_list if dynamic_fc_names.Height_Point_P in fc][0]
        feature_layer_hp = arcpy.management.MakeFeatureLayer(height_point, "feature_layer_kilo", bench_query)
        # Snapping kilometer post feature
        snap_env = [feature_layer_rd, "EDGE", snap_dist]
        arcpy.edit.Snap(feature_layer_rd, [snap_env])

        single_carriage_hwy = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[0])
        single_carriage_road = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[1])
        dual_carriage_hwy = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[2])
        dual_carriage_road = arcpy.management.SelectLayerByAttribute(feature_layer_rd, "NEW_SELECTION", road_query[3])

        # Select by location
        sel_single_carriage_hwy = arcpy.management.SelectLayerByLocation(feature_layer_hp, "INTERSECT", single_carriage_hwy, "", "NEW_SELECTION")
        sel_single_carriage_road = arcpy.management.SelectLayerByLocation(feature_layer_hp, "INTERSECT", single_carriage_road, "", "NEW_SELECTION")
        sel_dual_carriage_hwy = arcpy.management.SelectLayerByLocation(feature_layer_hp, "INTERSECT", dual_carriage_hwy, "", "NEW_SELECTION")
        sel_dual_carriage_road = arcpy.management.SelectLayerByLocation(feature_layer_hp, "INTERSECT", dual_carriage_road, "", "NEW_SELECTION")
        # Calculation orient degree
        offset_xy(sel_single_carriage_hwy, feature_layer_rd, orient_fld, offset_dist_l, perpendicular, working_gdb)
        offset_xy(sel_single_carriage_road, feature_layer_rd, orient_fld, offset_dist_l, perpendicular, working_gdb)
        offset_xy(sel_dual_carriage_hwy, feature_layer_rd, orient_fld, offset_dist_u, perpendicular, working_gdb)
        offset_xy(sel_dual_carriage_road, feature_layer_rd, orient_fld, offset_dist_u, perpendicular, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Offset benchmark error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)  

# # Resolving Conflict for Lines
def resolve_conflict_lines(fc_list, feature_loc_path, input_line_layers, symbology_file_path, working_gdb, cartopartion, edge_features, embank_list, 
                           compare_fcs_embank, bridge_query, footprint_fcs, resolve_line_compare, road_query, log_dir, map_name, val_dict, logger):
    
    logger.info(f"Map Name is: {map_name}")
    arcpy.AddMessage('Starting resolve conflicts for lines.....')
    dynamic_fc_names = resolve_lyr()
    try:
        # Set Environment
        arcpy.env.overwriteOutput = 1
        # Get feature classes
        input_line_layers = list(filter(str.strip, input_line_layers))
        # input_line_layers = [fc for line_lyr in input_line_layers for fc in fc_list if str(line_lyr) in fc]
        # Get the map layers
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        fc_layers = None
        maps = aprx.listMaps(map_name)
        if(maps):
            fc_layers = maps[0].listLayers()
        # Line features
        river = [fc for fc in fc_layers if dynamic_fc_names.River_L  in fc.name][0]

        irrigation_canal = [fc for fc in fc_layers if dynamic_fc_names.Irrigation_Canal_L in fc.name][0]
        # Polygon features
        lake = [fc for fc in fc_layers if dynamic_fc_names.Lake_A in fc.name][0]
        pond = [fc for fc in fc_layers if dynamic_fc_names.Pond_A in fc.name][0]
        irrigation_cov = [fc for fc in fc_layers if dynamic_fc_names.Irrigation_Canal_Coverage_A in fc.name][0]
        ##Set the symbology for the line layers
        layerx_path = symbology_file_path
         
        #---Resolve road conflicts---#
        # Set the reference scale
        arcpy.env.referenceScale = val_dict['Resolve_conflict_line_ref_scale']
        arcpy.env.cartographicPartitions = cartopartion
        
        # Get the feature layer
        
        fc_line_layers = [lyr for lyr in fc_layers for fc in input_line_layers if fc in str(lyr.name)]
        
        
        if len(fc_line_layers) > 0:
            arcpy.AddMessage("Resolving road conflicts")
            arcpy.cartography.ResolveRoadConflicts(fc_line_layers, val_dict['Resolve_conflict_line_hierarchy_field'], f"{working_gdb}\\displace")

        # Determine and reconnect for line features
        arcpy.AddMessage("Determining and reconnecting line features")
        part_01_lk = (arcpy.da.Describe(lake)['name']).split("_")[1]
        part_01_pnd = (arcpy.da.Describe(pond)['name']).split("_")[1]
        part_01_ic = (arcpy.da.Describe(irrigation_cov)['name']).split("_")[1]
        part_02_r = (arcpy.da.Describe(river)['name']).split("_")[1]
        part_02_icnl = (arcpy.da.Describe(irrigation_canal)['name']).split("_")[1]

        out_name_1 = f"{part_01_lk}_{part_02_r}_rlc"
        out_name_2 = f"{part_02_icnl}_{part_01_lk}_rlc"
        out_name_3 = f"{part_02_r}_{part_01_pnd}_rlc"
        out_name_4 = f"{part_02_icnl}_{part_01_pnd}_rlc"
        out_name_5 = f"{part_02_r}_{part_01_ic}_rlc"
        out_name_6 = f"{part_02_icnl}_{part_01_ic}_rlc"

        out_table1 = working_gdb + "\\" + out_name_1
        out_table2 = working_gdb + "\\" + out_name_2
        out_table3 = working_gdb + "\\" + out_name_3
        out_table4 = working_gdb + "\\" + out_name_4
        out_table5 = working_gdb + "\\" + out_name_5
        out_table6 = working_gdb + "\\" + out_name_6

        # If the table exists, delete all the records
        if arcpy.Exists(out_table1):
            arcpy.management.DeleteRows(out_table1)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_1)
        if arcpy.Exists(out_table2):
            arcpy.management.DeleteRows(out_table2)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_2)
        if arcpy.Exists(out_table3):
            arcpy.management.DeleteRows(out_table3)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_3)
        if arcpy.Exists(out_table4):
            arcpy.management.DeleteRows(out_table4)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_4)
        if arcpy.Exists(out_table5):
            arcpy.management.DeleteRows(out_table5)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_5)
        if arcpy.Exists(out_table6):
            arcpy.management.DeleteRows(out_table6)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_6)

        line_field_river = "FID_" + arcpy.da.Describe(river)['name']
        line_field_canal = "FID_" + arcpy.da.Describe(irrigation_canal)['name']
        poly_field_lake = "FID_" + arcpy.da.Describe(lake)['name']
        poly_field_pond = "FID_" + arcpy.da.Describe(pond)['name']
        poly_field_irri_cov = "FID_" + arcpy.da.Describe(irrigation_cov)['name']
 
        # # Run the determine function
        determine(river, lake, out_table1, line_field_river, poly_field_lake, working_gdb)
        reconnect_touching(lake, river, out_table1, val_dict['Resolve_conflict_line_delete'])
        determine(irrigation_canal, lake, out_table2, line_field_canal, poly_field_lake, working_gdb)
        reconnect_touching(lake, irrigation_canal, out_table2, val_dict['Resolve_conflict_line_delete'])
        determine(river, pond, out_table3, line_field_river, poly_field_pond, working_gdb)
        reconnect_touching(pond, river, out_table3, val_dict['Resolve_conflict_line_delete'])
        determine(irrigation_canal, pond, out_table4, line_field_canal, poly_field_pond, working_gdb)
        reconnect_touching(pond, irrigation_canal, out_table4, val_dict['Resolve_conflict_line_delete'])
        determine(river, irrigation_cov, out_table5, line_field_river, poly_field_irri_cov, working_gdb)
        reconnect_touching(irrigation_cov, river, out_table5, val_dict['Resolve_conflict_line_delete'])
        determine(irrigation_canal, irrigation_cov, out_table6, line_field_canal, poly_field_irri_cov, working_gdb)
        reconnect_touching(irrigation_cov, irrigation_canal, out_table6, val_dict['Resolve_conflict_line_delete'])

        ## Recreate boundary
        edge_features = list(filter(str.strip, edge_features))
        edge_features = [fc for edge_lyr in edge_features for fc in fc_layers if str(edge_lyr) in fc.name and not fc.isGroupLayer]
        irrigation_canal_edge = [fc for fc in fc_layers if dynamic_fc_names.Irrigation_Canal_Edge_L in fc.name][0]
        river_bank = [fc for fc in fc_layers if dynamic_fc_names.River_Bank_L in fc.name][0]
        river_cov = [fc for fc in fc_layers if dynamic_fc_names.River_Coverage_A in fc.name][0]
        # Recreate boundary lines
        recreate_boundary_lines(irrigation_canal_edge, irrigation_cov, edge_features)
        recreate_boundary_lines(river_bank, river_cov, edge_features)
        # Propagate displacement
        footprint_fcs = list(filter(str.strip, footprint_fcs))
        footprint_fcs = [fc for ft_lyr in footprint_fcs for fc in fc_layers if str(ft_lyr) in fc.name]
        arcpy.env.referenceScale = val_dict['Resolve_conflict_line_ref_scale']
        for footprint in footprint_fcs:
            arcpy.cartography.PropagateDisplacement(footprint, f"{working_gdb}\\displace", "AUTO")
        #--- Resolve conflict for lakes and ponds ---#
        # Get feature classes
        compare_fcs = list(filter(str.strip, resolve_line_compare))
        compare_fcs = [fc for com_lyr in compare_fcs for fc in fc_list if str(com_lyr) in fc]

        # Polygon features
        lake = [fc for fc in fc_layers if dynamic_fc_names.Lake_A in fc.name][0]
        pond = [fc for fc in fc_layers if dynamic_fc_names.Pond_A in fc.name][0]

        road = [fc for fc in fc_layers if dynamic_fc_names.Road_L in fc.name][0]
        feature_layer_rd = arcpy.management.MakeFeatureLayer(road, "feature_layer_rd", val_dict['Resolve_conflict_line_lyr_expression'])
        track = [fc for fc in fc_layers if dynamic_fc_names.Track_L in fc.name][0]
        feature_layer_tr = arcpy.management.MakeFeatureLayer(track, "feature_layer_tr", val_dict['Resolve_conflict_line_lyr_expression'])
        railway = [fc for fc in fc_layers if dynamic_fc_names.Rail_Line_L in fc.name][0]
        feature_layer_rail = arcpy.management.MakeFeatureLayer(railway, "feature_layer_rail", val_dict['Resolve_conflict_line_lyr_expression'])

        # Determine and reconnect for lakes and ponds
        arcpy.AddMessage("Determining and reconnecting lakes and ponds")
        part_01_lk = (arcpy.da.Describe(lake)['name']).split("_")[1]
        part_01_pnd = (arcpy.da.Describe(pond)['name']).split("_")[1]
        part_02_r = (arcpy.da.Describe(river)['name']).split("_")[1]
        part_02_icnl = (arcpy.da.Describe(irrigation_canal)['name']).split("_")[1]

        out_name_1 = f"{part_01_pnd}_{part_02_r}_rlc"
        out_name_2 = f"{part_01_pnd}_{part_02_icnl}_rlc"
        out_name_3 = f"{part_01_lk}_{part_02_r}_rlc"
        out_name_4 = f"{part_01_lk}_{part_02_icnl}_rlc"

        out_table1 = working_gdb + "\\" + out_name_1
        out_table2 = working_gdb + "\\" + out_name_2
        out_table3 = working_gdb + "\\" + out_name_3
        out_table4 = working_gdb + "\\" + out_name_4

        # If the table exists, delete all the records
        if arcpy.Exists(out_table1):
            arcpy.management.DeleteRows(out_table1)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_1)
        if arcpy.Exists(out_table2):
            arcpy.management.DeleteRows(out_table2)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_2)
        if arcpy.Exists(out_table3):
            arcpy.management.DeleteRows(out_table3)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_3)
        if arcpy.Exists(out_table4):
            arcpy.management.DeleteRows(out_table4)
        else:
            # Create the table for storing information about touching features
            arcpy.management.CreateTable(working_gdb, out_name_4)
        # Required fields creation
        line_field_river = "FID_" + arcpy.da.Describe(river)['name']
        line_field_canal = "FID_" + arcpy.da.Describe(irrigation_canal)['name']
        poly_field_lake = "FID_" + arcpy.da.Describe(lake)['name']
        poly_field_pond = "FID_" + arcpy.da.Describe(pond)['name']
        # Delete boolean
        # delete = False
        # # Run the determine function
        determine(river, pond, out_table1, line_field_river, poly_field_pond, working_gdb)
        reconnect_touching(pond, river, out_table1, val_dict['Resolve_conflict_line_delete'])
        determine(irrigation_canal, pond, out_table2, line_field_canal, poly_field_pond, working_gdb)
        reconnect_touching(pond, irrigation_canal, out_table2, val_dict['Resolve_conflict_line_delete'])
        determine(river, lake, out_table3, line_field_river, poly_field_lake, working_gdb)
        reconnect_touching(lake, river, out_table3, val_dict['Resolve_conflict_line_delete'])
        determine(irrigation_canal, lake, out_table4, line_field_canal, poly_field_lake, working_gdb)
        reconnect_touching(lake, irrigation_canal, out_table4, val_dict['Resolve_conflict_line_delete'])
        # # Trim polygon features within distance
        pond_road = trim_polygon_within_distance(pond, val_dict['Resolve_conflict_line_name_field'], feature_layer_rd, val_dict['Resolve_conflict_line_distance_b'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        pond_rai = trim_polygon_within_distance(pond, val_dict['Resolve_conflict_line_name_field'], feature_layer_rail, val_dict['Resolve_conflict_line_distance_b'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        lake_rail = trim_polygon_within_distance(lake, val_dict['Resolve_conflict_line_name_field'], feature_layer_rail, val_dict['Resolve_conflict_line_distance_b'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        lake_road = trim_polygon_within_distance(lake, val_dict['Resolve_conflict_line_name_field'], feature_layer_rd, val_dict['Resolve_conflict_line_distance_b'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        lake_track = trim_polygon_within_distance(lake, val_dict['Resolve_conflict_line_name_field'], feature_layer_tr, val_dict['Resolve_conflict_line_distance_s'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        pond_track = trim_polygon_within_distance(pond, val_dict['Resolve_conflict_line_name_field'], feature_layer_tr, val_dict['Resolve_conflict_line_distance_s'], val_dict['Resolve_conflict_line_minimum_area'], val_dict['Resolve_conflict_line_delete'], working_gdb)
        # #---Explode remove dissolve---#
        explode_remove_dissolve(lake, val_dict['Resolve_conflict_line_minimum_area'], working_gdb)
        explode_remove_dissolve(pond, val_dict['Resolve_conflict_line_minimum_area'], working_gdb)
        # Delete small polygons by converting
        minimumArea = val_dict['Resolve_conflict_line_minimum_area'] * 4
        remove_by_converting(pond_road, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        remove_by_converting(pond_rai, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        remove_by_converting(lake_rail, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        remove_by_converting(lake_road, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        remove_by_converting(lake_track, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        remove_by_converting(pond_track, compare_fcs, minimumArea, val_dict['Resolve_conflict_line_additional_criteria'], working_gdb)
        # # Reduce embankment conflict
        embank_list = list(filter(str.strip, embank_list))
        embank_list = [fc for embn_lyr in embank_list for fc in fc_list if str(embn_lyr) in fc]
        compare_fcs_embank = list(filter(str.strip, compare_fcs_embank))
        compare_fcs_embank = [fc for emb_lyr in compare_fcs_embank for fc in fc_list if str(emb_lyr) in fc]
        for embn_fc in embank_list:
            trim_line_within_distance(embn_fc, val_dict['Resolve_conflict_line_visible_field'], val_dict['Resolve_conflict_line_distance_l'], val_dict['Resolve_conflict_line_distance_minimum_length'], val_dict['Resolve_conflict_line_ref_scale'], val_dict['Resolve_conflict_line_erase_input_features'], compare_fcs_embank, working_gdb)

        # Offset kilometer post
        offset_kilometer_post(fc_list, road_query, val_dict['Resolve_conflict_line_orient_fld'], val_dict['Resolve_conflict_line_offset_distance_s'], val_dict['Resolve_conflict_line_offset_distance_l'], val_dict['Resolve_conflict_line_perpendicular_k'], working_gdb, val_dict['RCL_offset_kilometer_post_kmpost_snap_dist'])
        # Offset benchmark
        offset_benckmark(fc_list, road_query, val_dict['Resolve_conflict_line_bench_query'], val_dict['Resolve_conflict_line_orient_fld'], val_dict['Resolve_conflict_line_offset_distance_benc_l'], val_dict['Resolve_conflict_line_offset_distance_benc_u'], val_dict['Resolve_conflict_line_perpendicular_b'], working_gdb, val_dict['RCL_offset_benchmark_kmpost_snap_dist'] )
        # Snap bridge
        road = [fc for fc in fc_layers if dynamic_fc_names.Road_L in fc.name][0]
        feature_layer_rd = arcpy.management.MakeFeatureLayer(road, "feature_layer_rd")
        track = [fc for fc in fc_layers if dynamic_fc_names.Track_L in fc.name][0]
        feature_layer_tr = arcpy.management.MakeFeatureLayer(track, "feature_layer_tr")
        arcpy.management.RepairGeometry(feature_layer_tr)
        railway = [fc for fc in fc_layers if dynamic_fc_names.Rail_Line_L in fc.name][0]
        feature_layer_rail = arcpy.management.MakeFeatureLayer(railway, "feature_layer_rail")
        arcpy.management.RepairGeometry(feature_layer_rail)
        bridge = [fc for fc in fc_layers if dynamic_fc_names.Bridge_P in fc.name][0]
        feature_layer_rail_br = arcpy.management.MakeFeatureLayer(bridge, "feature_layer_rail_br", bridge_query[0])
        feature_layer_road_br = arcpy.management.MakeFeatureLayer(bridge, "feature_layer_road_br", bridge_query[1])
        # Snapping between railway bridge and railway lyr
        snap_env = [feature_layer_rail, "EDGE", val_dict['RCL_snap_btwn_railbridge_railway_snap_dist']]
        arcpy.edit.Snap(feature_layer_rail_br, [snap_env])
        # Snapping between road bridge and road and track lyr
        snapEnv1 = [feature_layer_rd, "EDGE", val_dict['RCL_snap_btwn_roadbridge_road_snap_dist_1']]
        snapEnv2 = [feature_layer_tr, "EDGE", val_dict['RCL_snap_btwn_roadbridge_road_snap_dist_2']]
        arcpy.edit.Snap(feature_layer_road_br, [snapEnv1, snapEnv2])

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Resolve conflicts for lines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Resolve conflicts for lines', f'{exc_value}\n')