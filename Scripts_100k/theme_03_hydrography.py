import arcpy
import traceback
import sys
import DetermineTouching as touch
import SplitByBox
import RemoveByConverting as convert
from common_utils import *

def identify_polygon(poly_layer, line_layer, logger):
    """Populates the id from the polygon feature that contains a line on the line feature"""
    # Set environment variables
    arcpy.env.overwriteOutput = True

    try:
        # Add fields
        arcpy.management.AddField(line_layer, 'ORIG_FID', 'LONG', '#', '#', '#', '#', 'NULLABLE', 'NON_REQUIRED', '#')
        arcpy.management.AddField(line_layer, 'Casing', 'SHORT', '#', '#', '#', '#', 'NULLABLE', 'NON_REQUIRED', '#')

        with arcpy.da.SearchCursor(poly_layer, ['SHAPE@', 'OID@']) as cursor:
            for row in cursor:
                arcpy.management.SelectLayerByLocation(line_layer, 'WITHIN_CLEMENTINI', row[0])
                with arcpy.da.UpdateCursor(line_layer, ['ORIG_FID', 'Casing']) as u_cursor:
                    for u_row in u_cursor:
                        u_row[0] = row[1]
                        u_row[1] = 1
                        u_cursor.updateRow(u_row)

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Identify polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Identify polygon', f'{exc_value}\n')


def check_middle(poly_layer, delete_poly_for_wide, delete_poly_ids, dangle_polys):
    """ Determines if any of the polygons to be deleted are in the middle of two
    polygons that will not be deleted.  If so, adds polygon to widen list rather
    than deleting"""
    # Set environment variables
    arcpy.env.overwriteOutput = True
    try:
        arcpy.AddMessage("Checking for middle features")
        desc = arcpy.da.Describe(poly_layer)
        delimit_oid = desc['OIDFieldName']
        delete_query = ""
        for oid in delete_poly_ids:
            delete_query = delete_query + delimit_oid + " = " + str(oid) + " OR "
        delete_query = delete_query[:-4]

        delete_test = arcpy.management.MakeFeatureLayer(poly_layer, "delete_test", delete_query)

        near_delete = arcpy.analysis.GenerateNearTable(delete_test, poly_layer, "del_near", "0 Meters", closest="ALL")
        near_del_ids = [arow[0] for arow in arcpy.da.SearchCursor(near_delete, "IN_FID")]
        delete_unique_ids = list(set(near_del_ids))

        for del_id in delete_unique_ids:
            arcpy.AddMessage(f"checking feature {del_id}")
            query_u = f"IN_FID = {del_id}"
            cnt = 0

            for brow in arcpy.da.SearchCursor(near_delete, ["IN_FID", "NEAR_FID"], query_u):

                if brow[1] not in delete_unique_ids:
                    arcpy.AddMessage("touches feature not being deleted")
                    cnt += 1
                else:
                    arcpy.AddMessage("touches another delete feature")

            if cnt >= 2:
                arcpy.AddMessage(f"will not delete will widen {del_id}")
                delete_poly_ids.remove(del_id)

            else:
                arcpy.AddMessage("... Feature is dangle and will be deleted.")

        delete_query = ""
        for oid in delete_poly_ids:
            delete_query = delete_query + delimit_oid + " = " + str(oid) + " OR "
        delete_query = delete_query[:-4]

        return delete_query, delete_poly_ids
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Check middle error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def connect_centerlines(out_table, input_lines, line_fc, l_match, p_match, delete_ids, logger):
    # Set environment variables
    arcpy.env.overwriteOutput = True
    try:
        fields = arcpy.ListFields(out_table)
        arcpy.management.CopyFeatures(input_lines, "BeforeConnectLines")

        with arcpy.da.UpdateCursor(input_lines, ['OID@', 'SHAPE@']) as u_cur:
            for u_row in u_cur:
                line_geo = u_row[1]
                # Get a list of the polygon features this feature should be connected to
                query = f'{l_match} = {u_row[0]}'
                values = [str(row[0]) for row in arcpy.da.SearchCursor(out_table, p_match, query)]
                unique_polys = set(values)
                arcpy.AddMessage(f"Touches {len(unique_polys)} features")

                if len(unique_polys) >= 1:
                    where_clause = "ORIG_FID = "
                    where_clause += " OR ORIG_FID = ".join(unique_polys)

                    centerline_geos = [row[0] for row in arcpy.da.SearchCursor(line_fc, ["SHAPE@", "OID@", "ORIG_FID"], where_clause)]

                    # Loop through each polygon geometry to determine if the line is still connected
                    for poly in centerline_geos:

                        if line_geo.disjoint(poly):
                            arcpy.AddMessage(f"Reconnecting line {u_row[0]}")
                            near = arcpy.management.CopyFeatures(line_geo, "in_memory\\near")

                            arcpy.analysis.Near(near, poly, "", "LOCATION")

                            # Get the values from the Near_X and Near_y fields
                            for row in (arcpy.da.SearchCursor(near, ["NEAR_X", "NEAR_Y"])):
                                x_pt = row[0]
                                y_pt = row[1]
                                break
                            point = arcpy.Point(x_pt, y_pt)
                            pt_geo = arcpy.PointGeometry(point)

                            # Determine if point should be added to beginning or end of the line
                            start_pt = line_geo.firstPoint
                            end_pt = line_geo.lastPoint

                            array = arcpy.Array()
                            # If closer to the start
                            if pt_geo.distanceTo(start_pt) < pt_geo.distanceTo(end_pt):
                                # add the point to the beginning of the line
                                array.add(point)
                                for part in line_geo:
                                    for pnt in part:
                                        array.add(pnt)
                                    break
                            else:
                                # add the point to the end of the line
                                for part in line_geo:
                                    for pnt in part:
                                        array.add(pnt)
                                    break
                                array.add(point)

                            # create a line
                            polyline = arcpy.Polyline(array)
                            # update the geometry of the row
                            u_row[1] = polyline
                            u_cur.updateRow(u_row)

        # Copy feature after connect lines
        arcpy.management.CopyFeatures(input_lines, "AfterConnectLines")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Connect center lines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Connect center lines', f'{exc_value}\n')


def create_secondary_lyrs(topo_fcs, primary_lyr):
    ''' converts the secondary feature classes to layers and selects only those
    features that touch the primary_lyr'''
    try:
        topo_fclyrs = []
        for feat in topo_fcs:
            # get just the secondary fc name without path
            indx = str(feat).rfind("\\")
            fc_name = str(feat)[indx+1:]

            #create lyr
            lyr_name = fc_name + "lyr"
            arcpy.management.MakeFeatureLayer(feat, lyr_name)
            arcpy.management.SelectLayerByLocation(lyr_name, "INTERSECT", primary_lyr)
            if int(arcpy.management.GetCount(lyr_name)[0]) >= 1:
                topo_fclyrs.append(lyr_name)

        return topo_fclyrs

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Create secondary layers error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def rebuild_centerline(split_layer, center_layer, update_field, polygons, width_np, working_gdb, logger):
    """ Ensure all centerlines outside of polygons are visibile"""
    # Set environment variables
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb

    # Feature to line with centerline and polygons
    # This will split centerlines at intersections and split centerlines at boundary of expanded polygons
    try:
        arcpy.AddMessage("Updating casing values for centerlines")
        arcpy.management.SelectLayerByAttribute(split_layer, "CLEAR_SELECTION")
        orig_oid_field = f'FID_{(arcpy.da.Describe(split_layer))["name"]}'
        center_features = f"{orig_oid_field} <> -1"
        final_center_split = arcpy.management.FeatureToLine([split_layer, polygons], "temp_final_split", "#", 'ATTRIBUTES')
        final_center_lyr = arcpy.management.MakeFeatureLayer(final_center_split, "temp_final_lyr", center_features)

        # Calculate casing values if within polygon Casing = 1
        arcpy.management.SelectLayerByLocation(final_center_lyr, "WITHIN_CLEMENTINI", polygons)
        if int(arcpy.management.GetCount(final_center_lyr)[0]) >= 1:
            # Change the value in the Casing field so centerline is visible
            arcpy.management.CalculateField(final_center_lyr, update_field, 1)

        # If outside polygon Casing = 2
        arcpy.management.SelectLayerByAttribute(final_center_lyr, "SWITCH_SELECTION")
        if int(arcpy.management.GetCount(final_center_lyr)[0]) >= 1:
            # Change the value in the Casing field so centerline is visible
            arcpy.management.CalculateField(final_center_lyr, update_field, 2)

        # Unsplit polygons based on casing value - keep the orig OID
        arcpy.management.SelectLayerByAttribute(final_center_lyr, "CLEAR_SELECTION")
        center_oid_field = f'FID_{(arcpy.da.Describe(split_layer))["name"]}'
        un_layer = arcpy.analysis.PairwiseDissolve(final_center_lyr, "unsplit_center_layer", [update_field, center_oid_field])


        # Determine which original features are now 2 features...
        orig_oids = [str(row[0]) for row in arcpy.da.SearchCursor(un_layer, center_oid_field)]
        dup_oids = [x for i, x in enumerate(orig_oids) if orig_oids.count(x) > 1]
        # If duplicates exist.  Add new rows into the input feature class
        if len(dup_oids) >= 1:
            arcpy.management.AddField(center_layer, "ORIG_OID")
            arcpy.management.CalculateField(center_layer, "ORIG_OID", '!OBJECTID!',"PYTHON3")
            dup_oids = set(dup_oids)
            arcpy.AddMessage("Some original lines split into multiple")
            where_clause = "OBJECTID = "
            where_clause += "OR OBJECTID =".join(dup_oids)
            arcpy.management.MakeFeatureLayer(center_layer, "dup_layer", where_clause)
            arcpy.management.AddField("dup_layer", "Casing", 'SHORT', '#', '#', '#', '#', 'NULLABLE', 'NON_REQUIRED', '#')
            dup_recs = arcpy.management.CopyFeatures("dup_layer", "Duplicate_records")
            

        # Loop through the original features
        dup_rec_ids = []
        center_layer = arcpy.conversion.ExportFeatures(center_layer, "center_layer")
        field_list = [fld.name for fld in arcpy.ListFields(center_layer)]
        if "CASING" not in field_list and "Casing" not in field_list:
            # Add the Casing field to the line layer if it does not exist
            # This field will be used to determine if the line is within a polygon
            # or outside of a polygon
            arcpy.management.AddField(center_layer, "Casing", 'SHORT', '#', '#', '#',
                                    '#', 'NULLABLE', 'NON_REQUIRED', '#')
            
        with arcpy.da.UpdateCursor(center_layer, ["OID@", update_field, "SHAPE@"]) as u_cur:
            for u_row in u_cur:
                # Select the features from the unsplit lines that match the OID
                match_unsplit_query = center_oid_field + " = " + str(u_row[0])

                with arcpy.da.SearchCursor(un_layer, ['OID@', update_field, 'SHAPE@'], match_unsplit_query) as match_cur:
                    # If the feature is split into two
                    if str(u_row[0]) in dup_oids:
                        for match_row in match_cur:
                            # If the records have the same casing value, update the geometry
                            if match_row[1] == u_row[1]:
                                u_row[2] = match_row[2]
                                arcpy.AddMessage("Updating geometry")
                            # Otherwise store the oid to update the duplicate record
                            else:
                                dup_rec_ids.append(str(match_row[0]))

                    # If the feature is not split into two
                    else:
                        # Update the casing and geomtry values
                        for match_row in match_cur:
                            u_row[1] = match_row[1]
                            u_row[2] = match_row[2]

                u_cur.updateRow(u_row)

        if len(dup_rec_ids) >= 1:
            where_clause = "OBJECTID = "
            where_clause += "OR OBJECTID =".join(dup_rec_ids)
            arcpy.management.MakeFeatureLayer(un_layer, "un_layer1", where_clause)
            
            # Update the geometries of the copied duplicate records and switch the casing value for each record
            with arcpy.da.UpdateCursor(dup_recs, [update_field, "ORIG_OID", "SHAPE@"]) as dup_cur:
                for dup_row in dup_cur:
                    if dup_row[0] == 1:
                        dup_row[0] = 2
                    elif dup_row[0] == 2:
                        dup_row[0] = 1

                    # Determine new geometry
                    query = f'{center_oid_field} = {dup_row[1]}'
                    arcpy.management.SelectLayerByAttribute("un_layer1", "", query)
                    if int(arcpy.management.GetCount("un_layer1")[0]) >= 1:
                        geom = [row[0] for row in arcpy.da.SearchCursor("un_layer1", 'SHAPE@')]
                        dup_row[2] = geom[0]
                    dup_cur.updateRow(dup_row)

            # Finally, copy the duplicate records back into the centerline...
            arcpy.management.Append(dup_recs, center_layer, "NO_TEST")
            arcpy.management.DeleteField(center_layer, "ORIG_OID")

        length_field = (arcpy.da.Describe(center_layer))['lengthFieldName']
        length_query = f'{length_field} < {width_np/3} AND {update_field} = 1'
        arcpy.management.SelectLayerByAttribute(center_layer, "NEW_SELECTION", length_query)
        arcpy.management.SelectLayerByLocation(center_layer, "BOUNDARY_TOUCHES", polygons, "", "SUBSET_SELECTION")

        if int(arcpy.management.GetCount(center_layer)[0]) >= 1:
            arcpy.AddMessage("Small dangles were introduce, deleting dangles.")
            arcpy.management.DeleteFeatures(center_layer)

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Rebuild center lines error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Rebuild center lines', f'{exc_value}\n')


def build_name_query(input_polygons, name_val, name_field):
    """ Build a query to find all features with the same name as this record"""
    try:
        blank_values = "true"
        name_delimited = arcpy.AddFieldDelimiters(input_polygons, name_field)

        # Determine the field type for the chosen field
        for field in arcpy.ListFields(input_polygons):
            if field.name == name_field:
                field_type = field.type
                # If the field is a text field
                if field_type == "String":
                    # If blank values option is enabled, also search for features with no name value
                    if blank_values == "true" or name_val == "None":
                        values = [name_delimited + " = \'" + name_val + "\'",
                                name_delimited + " IS NULL",
                                name_delimited + " = \'\'",
                                name_delimited + " = \' \'"]
                        name_query = " OR ".join(values)
                        name_query[:-4]
                    else:
                        name_query = "{0} = '{1}'".format(name_delimited, name_val)
                # If the field is a number field
                elif (field_type == "Double" or field_type == "Integer" or
                    field_type == "Single" or field_type == "SmallInteger"):
                    if blank_values == "true":
                        values = [name_delimited + " = " + name_val,
                                name_delimited + " IS NULL",
                                name_delimited + " = 0"]
                        name_query = " OR ".join(values)
                        name_query[:-4]
                    else:
                        name_query = "{0} = {1}".format(name_delimited, name_val)
                else:
                    arcpy.AddMessage("Field type does not support including blank values."
                                    + " Will only combine matching values.")
                    
                    name_query = "{0} = '{1}'".format(name_delimited, name_val)
            
                return name_query
            
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Build name query error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def merge(poly_layer, distance, field, scratch):
    try:
        if arcpy.Exists(f"{scratch}\\aggr"):
            arcpy.management.Delete(f"{scratch}\\aggr")
        deleteCount = 0
        # In memory layer to hold aggregation of polygons
        mem = f"{scratch}\\aggr"
        # Aggregation (+ 10 to ensure both features in layer are aggregated
        arcpy.cartography.AggregatePolygons(poly_layer, mem, distance)
        # Unable to get geom list, using Search Cursor to access shape of in_memory feature
        with arcpy.da.SearchCursor(mem, ["SHAPE@"]) as mem_cur:
            for mem_row in mem_cur:
                geom = mem_row[0]
                arcpy.SelectLayerByLocation_management(poly_layer, "WITHIN", geom)
                # Use update cursor to update geometry of hydro poly
                # Use SQL Clause to sort by name so that the first record returned can be used
                # to update the geometry.  If this record doesn't have a name, then none of the
                # returned records do.
                with arcpy.da.UpdateCursor(poly_layer, [field, "SHAPE@"], sql_clause=(None, "ORDER BY " + field + " DESC")) as poly_cur:
                    for poly_row in poly_cur:
                        # Update geometry of current row
                        poly_row = (poly_row[0], geom)
                        poly_cur.updateRow(poly_row)
                        # Delete the remaining rows in the cursor that overlap
                        for other_row in poly_cur:
                            if not geom.disjoint(other_row[1]):
                                poly_cur.deleteRow()
                                deleteCount += 1
        if arcpy.Exists(mem):
            arcpy.management.Delete(mem)

        return deleteCount
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Merge error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def hydro_prep(line_fc, sel_fc_list):
    try:
        temp_list = []
        desc = arcpy.da.Describe(line_fc)
        fc_name = desc['name']
        features_lyr = arcpy.management.MakeFeatureLayer(line_fc, f"features_lyr_{fc_name}")
        
        for poly_fc in sel_fc_list:
            selected_fc = arcpy.management.SelectLayerByLocation(features_lyr, 'INTERSECT', poly_fc, "2 Meters", 'NEW_SELECTION')
            de_selected_fc = arcpy.management.SelectLayerByLocation(selected_fc, 'INTERSECT', poly_fc, "0 Meters", 'REMOVE_FROM_SELECTION')
            snap_env = [poly_fc, "EDGE", "2 Meters"]
            arcpy.edit.Snap(de_selected_fc, [snap_env])
            temp_list.append(poly_fc)
            temp_list.append(de_selected_fc)
        # Feature integration
        arcpy.management.Integrate(temp_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Hydro prep error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def remove_short_lines_connecting_polys(hydro_line, hydro_poly, name_fld, line_len, working_gdb):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    try:
        riverCount = 0

        # Make feature layer of hydro lines less than specified length
        arcpy.AddMessage( "Creating feature layer of hydro line segments less than " + str(line_len) + " Meters...")
        shape_length = arcpy.da.Describe(hydro_line)['lengthFieldName']
        where = f"{shape_length} < {line_len}"
        arcpy.management.MakeFeatureLayer(hydro_line, "hydro_line", where)

        # Add points at both ends of hydro lines subset (hydro lines < specified distance)
        # These points will be used later to determine if the points intersect hydro polys
        # and then in a spatial join to determine if the line has a hydro poly on both ends
        arcpy.AddMessage( "Creating end points for all hydro line segments less than " + str(line_len) + " Meters...")
        hydro_endpts = arcpy.management.FeatureVerticesToPoints("hydro_line", f"{working_gdb}\\hydro_endpts", "BOTH_ENDS")

        # Make feature layer of hydro line end points to use in selection
        arcpy.AddMessage( "Selecting hydro line end points that intersect with hydro polys...")
        arcpy.management.MakeFeatureLayer(hydro_endpts, "hydro_endpts")
        # Make feature layer of hydro polys to use in selection
        arcpy.management.MakeFeatureLayer(hydro_poly, "hydro_poly")
        # Select hydro line end points layer that intersect hydro polys
        arcpy.management.SelectLayerByLocation("hydro_endpts", "INTERSECT", "hydro_poly")

        # Create a spatial join of selected hydro line end points joined to hydro lines layer
        arcpy.AddMessage( "Creating spatial join of hydro line end points joined to hydro line segments...")
        hydro_sj = arcpy.analysis.SpatialJoin("hydro_line", "hydro_endpts", "hydro_line_sj")

        # Loop through each line in hydro spatial join where Count = 2
        # Select hydro polys that touch the boundary of each line
        # (should be two polys per line), run Aggregate Polygons GP
        # tool to combine the two separate polys, then delete the hydro
        # line segment from the hydro line data
        arcpy.AddMessage( "Deleting hydro line segments and merging hydro polys...")
        where = "Join_Count = 2"
        with arcpy.da.SearchCursor(hydro_sj, ["SHAPE@", "TARGET_FID"], where) as cur:
            for row in cur:
                # Select hydro polys that touch the boundary of the current line segment
                arcpy.management.SelectLayerByLocation("hydro_poly", "BOUNDARY_TOUCHES", row[0])
                result = arcpy.management.GetCount("hydro_poly")
                resultCnt = int(result.getOutput(0))
                if resultCnt > 0:
                    # Call merge function on hydro_poly layer
                    distance = str(line_len + 10) + " Meters"
                    name_list = [n_row[0] for n_row in arcpy.da.SearchCursor("hydro_poly", name_fld)]
                    name_list = set(name_list)
                    if None in name_list:
                        name_list.remove(None)
                    if "" in name_list:
                        name_list.remove("")

                    if len(name_list) <= 1:
                        merge("hydro_poly", distance, name_fld, working_gdb)
                        # Select original hydro line segemnt based on OBJECTID
                        where = "OBJECTID = " + str(row[1])
                        arcpy.management.SelectLayerByAttribute("hydro_line", "NEW_SELECTION", where)
                        result = arcpy.management.GetCount("hydro_line")
                        count = int(result.getOutput(0))
                        riverCount = riverCount + count
                        # Delete selected hydro line segment
                        arcpy.management.DeleteFeatures("hydro_line")

                    else:
                        arcpy.AddMessage("Line segment will not be removed because " +
                        "names of polygons do not match")
                        for name in name_list:
                            arcpy.AddMessage(name)
        arcpy.AddMessage(str(riverCount) + " line features were deleted.")
        # Clean up
        clean_list = ["hydro_line", "hydro_line_sj", "hydro_poly", "hydro_endpts", hydro_endpts, hydro_sj]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Remove short lines connecting polys error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def narrow_polygons_new(fc_list, polygon_input_list, centerline_input_list, width_units, buffer_percent_np, vis_field, topo_fcs, working_gdb, logger):
    ''' main function, performs most of operations'''
    # Set the workspace
    arcpy.env.overwriteOutput = True
    polygon_input_list = list(filter(str.strip, polygon_input_list))
    polygon_input_list = [fc for a_lyr in polygon_input_list for fc in fc_list if str(a_lyr) in fc]
    centerline_input_list = list(filter(str.strip, centerline_input_list))
    centerline_input_list = [fc for a_lyr in centerline_input_list for fc in fc_list if str(a_lyr) in fc]

    try:
        width_np = width_units
        buffer_distance_np = width_np / 2
        half_width_units = f"{buffer_distance_np} Meters"
        percent_out = 100 - buffer_percent_np
        arcpy.AddMessage("Buffer " + str(half_width_units))

        for polygon_input_np, center_line_input in zip(polygon_input_list, centerline_input_list):
            update_field = 'Casing'
            desc = arcpy.da.Describe(polygon_input_np)
            delimit_oid = desc['OIDFieldName']
            name = desc['name']
            # Create layers for inputs
            poly_input_layer = arcpy.management.MakeFeatureLayer(polygon_input_np, 'poly_in_lyr')
            center_layer = arcpy.management.MakeFeatureLayer(center_line_input, 'Center_line_lyr')

            # Set emty list
            casing_ids = []
            delete_poly_ids = []
            widen_ids = []
                        
            # Set emty dict
            delete_poly_for_wide = {}

            # If there is at least one centerline
            count_poly_input_layer = int(arcpy.management.GetCount(poly_input_layer)[0])
            count_center_line = int(arcpy.management.GetCount(center_layer)[0])

            if count_poly_input_layer >= 1 and count_center_line >= 1:
                arcpy.AddMessage(f'Prepping centerlines...{name}')
                # Split the Hydro Polygons and centerlines
                split_polygons, split_center, poly_is_split = SplitByBox.split(poly_input_layer, center_layer, width_np, working_gdb)
                poly_layer = arcpy.management.MakeFeatureLayer(split_polygons, 'poly_lyr')
                split_field = (arcpy.da.Describe(center_line_input))['name']
                q1 = f'FID_{split_field} <> -1'
                layer1 = arcpy.management.MakeFeatureLayer(split_center, 'split_lyr', q1)
                # Identity polygon
                identify_polygon(poly_layer, layer1, logger)
                arcpy.management.SelectLayerByAttribute(layer1, 'CLEAR_SELECTION')
                diss_center = arcpy.analysis.PairwiseDissolve(layer1, "temp_unsplit", "ORIG_FID", [[update_field, "MAX"]])
                # Add fields
                arcpy.management.AddField(diss_center, update_field, "SHORT")
                # Calculate field
                arcpy.management.CalculateField(diss_center, update_field, "!MAX_Casing!", "PYTHON3")
                # Creating layer
                split_layer = arcpy.management.MakeFeatureLayer(diss_center, "dissolve_center_lyr")
                # Select the centerlines that fall within a polygon
                query = "ORIG_FID IS NOT NULL"
                # Determine how to handle each polygon - delete or widen
                arcpy.AddMessage("Determining how to handle polygons...")
                count_split_center= int(arcpy.management.GetCount(split_center)[0])
                if count_split_center >= 1:
                    # Loop through each of the center lines
                    with arcpy.da.SearchCursor(split_center, ['OID@', 'SHAPE@', "ORIG_FID"], query) as cursor:
                        for row in cursor:
                            geo = row[1]
                            line_oid = row[0]
                            # Buffer the line
                            geo_buff = geo.buffer(buffer_distance_np)
                            # Find the polygon that the centerline is within
                            query = f'{delimit_oid} = {row[2]}'
                            # Open an update cursor on the polygon feature class
                            with arcpy.da.SearchCursor(poly_layer, ['OID@', 'SHAPE@'], query) as upcursor:
                                for uprow in upcursor:
                                    # Find the geometry of the polygon feature without the holes.
                                    oid = uprow[0]
                                    poly_geo = uprow[1]
                                    # Determine what percentage of the polygon geometry is contained in the buffer
                                    buff_intersect = geo_buff.intersect(poly_geo, 4)
                                    percent_contained = ((buff_intersect.area / poly_geo.area) * 100)
                                    percent_buffer_out = ((buff_intersect.area / geo_buff.area) * 100)
                                    # If the amount of the feature that is contained within the buffer is larger than the specified percent, then feature should be deleted
                                    if percent_contained >= buffer_percent_np:
                                        arcpy.AddMessage(f"... polygon {oid} is narrow and will be deleted")
                                        # Add polygon to delete list
                                        delete_poly_ids.append(oid)
                                        delete_poly_for_wide[oid] = line_oid
                                        widen_ids.append(line_oid)
                                        # Add centerline to update list
                                        casing_ids.append(str(row[0]))

                                    elif percent_buffer_out >= percent_out:
                                        arcpy.AddMessage(f"... polygon {oid} will be widened.")
                                        if geo.length >= (width_np * 2):
                                            widen_ids.append(line_oid)
                else:
                    arcpy.AddWarning("Unable to find any centerlines within polygons. No features will be modified.")
                # Determine which polygons have dangles
                arcpy.AddMessage("Determining dangles")
                arcpy.management.SelectLayerByAttribute(split_layer, "CLEAR_SELECTION")
                dangle_lyr = find_dangles(split_layer, update_field, working_gdb, logger)
                dangle_pts = [row[0] for row in arcpy.da.SearchCursor(dangle_lyr, ('SHAPE@'))]
                arcpy.AddMessage(f'{len(dangle_pts)} dangles')
                arcpy.analysis.Near(dangle_lyr, poly_layer, "0 Meters")
                dangle_polys = [row[0] for row in arcpy.da.SearchCursor(dangle_lyr, ('NEAR_FID'))]
                dangle_polys = set(dangle_polys)

                # First try converting polygons to topology feature classes, then delete polygons
                if len(delete_poly_ids) >= 1:
                    # Have to delete temp polygons because often only deleting parts...
                    delete_features = arcpy.management.MakeFeatureLayer(poly_layer, "delete_features")
                    arcpy.AddMessage("Determining layers to convert features to")
                    delete_query, delete_poly_ids = check_middle(poly_layer, delete_poly_for_wide, delete_poly_ids, dangle_polys)
                    arcpy.AddMessage(f'Delete query: {delete_query}')
                    arcpy.management.SelectLayerByAttribute(delete_features, "NEW_SELECTION", delete_query)
                    del_cnt = int(arcpy.management.GetCount(delete_features)[0])
                    arcpy.AddMessage(f'{del_cnt} features will be deleted')
                    if del_cnt >= 1:
                    # If secondary feature classes are identified, try converting the features to be deleted into one of these features
                        # Find out which centerline features touch the polygon that will be deleted
                        delete_features2 = arcpy.management.MakeFeatureLayer(delete_features, "delete_features2")
                        center_layer2 = arcpy.management.MakeFeatureLayer(split_center, "Centerline2_lyr")

                        arcpy.management.SelectLayerByLocation(center_layer2, "INTERSECT", delete_features2)

                        inside_layer2 = arcpy.management.MakeFeatureLayer(split_center, "inside2_lyr")
                        arcpy.management.SelectLayerByLocation(inside_layer2, "WITHIN", delete_features2)
                        snap_arr = [inside_layer2, "EDGE", half_width_units]
                        arcpy.edit.Snap(center_layer2, [snap_arr])

                        # touch_table = arcpy.management.CreateTable(working_gdb, "CenterlineTouch")

                        # touch.determine(center_layer2, delete_features2, touch_table, "center_id", "delete_poly_id")

                        # connect_centerlines(touch_table, center_layer2, split_center, "center_id", "delete_poly_id", delete_poly_ids, logger)

                        if len(topo_fcs) >= 1:
                            topo_fc_lyrs = create_secondary_lyrs(topo_fcs, delete_features)

                            # Convert overlapping features
                            count_delete_fcs = int(arcpy.management.GetCount(delete_features)[0])

                            if count_delete_fcs >= 1:
                                convert.ConvertOverlapping(delete_features, topo_fc_lyrs, working_gdb)
                            # Convert enclosed features
                            selected_delete_fcs = arcpy.management.SelectLayerByAttribute(delete_features, "NEW_SELECTION", delete_query)
                            cnt_del_fcs = int(arcpy.management.GetCount(selected_delete_fcs)[0]) 
                            if cnt_del_fcs >= 1:
                                convert.ConvertEnclosed(delete_features, topo_fc_lyrs)

                    # Finally, if any features could not be converted, just delete them
                    arcpy.management.SelectLayerByAttribute(delete_features, "NEW_SELECTION", delete_query)
                    if int(arcpy.management.GetCount(delete_features)[0]) >= 1:
                        arcpy.management.DeleteFeatures("delete_features")

                # Widen the remaining polygons

                enable_widening = False

                if len(widen_ids) >= 1:
                    arcpy.AddMessage('Features to widen')
                    arcpy.management.SelectLayerByAttribute(split_layer, "CLEAR_SELECTION")
                    poly_layer2 = arcpy.management.MakeFeatureLayer(split_polygons, "poly_lyr2")
                    with arcpy.da.SearchCursor(split_layer, ['OID@', 'SHAPE@', 'ORIG_FID']) as s_cur:
                        for s_row in s_cur:
                            if s_row[2] != None:
                                if s_row[2] >= 1:
                                    query = f'{(arcpy.da.Describe(poly_layer2))["OIDFieldName"]} = {s_row[2]}'
                                    with arcpy.da.UpdateCursor(poly_layer2, ['OID@', 'SHAPE@'], query) as p_up_cur:
                                        for p_up_row in p_up_cur:
                                            arcpy.AddMessage(f"Widening polygon {p_up_row[0]} from line {s_row[0]}")
                                            poly_geo = p_up_row[1]
                                            arcpy.AddMessage(f"Orig area {poly_geo.area}")
                                            buffer_geo = s_row[1].buffer(buffer_distance_np)
                                            test_buff = arcpy.management.CopyFeatures(buffer_geo, "test_buffer")
                                            # Integrate  between features
                                            arcpy.management.Integrate([[test_buff, 2], [split_center, 1]])
                                            # Densify
                                            arcpy.edit.Densify(test_buff, "DISTANCE", width_np)
                                            # Convert to geometry
                                            simple_buff = arcpy.management.CopyFeatures(test_buff, arcpy.Geometry())
                                            new_geo =poly_geo.union(simple_buff[0])
                                            p_up_row[1] = new_geo
                                            arcpy.AddMessage(f"New area {new_geo.area}")
                                            p_up_cur.updateRow(p_up_row)

                # Dissolve the polygons back together after being split and add back to input polygon
                arcpy.AddMessage("Updating input polygons")
                # Recreate the Polygons dissolve the polygons to create one feature for each input.
                if poly_is_split:
                    # dissolve_field = f"FID_{name}"
                    dissolve_field = "fid"
                    dissolve_features = arcpy.analysis.PairwiseDissolve(split_polygons, "temp_dissolve", dissolve_field)

                    # Update cursor to update the geometries of the original polygons
                    with arcpy.da.UpdateCursor(poly_input_layer, ['OID@', 'SHAPE@']) as cursor:
                        for row in cursor:
                            query = f'{dissolve_field} = {row[0]}'
                            values = [srow[0] for srow in arcpy.da.SearchCursor(dissolve_features, ['SHAPE@', dissolve_field], query)]
                            arcpy.AddMessage(f"Updating polygon {values} with dissolved geometry")
                            # If only one geometry is returned, update the geometry
                            if len(values) == 1:
                                arcpy.AddMessage(f"{row[0]} update")
                                geo = values[0]
                                row[1] = geo
                                cursor.updateRow(row)

                            # If no geometries are returned, the feature was deleted
                            elif len(values) == 0:
                                arcpy.AddMessage(f"{row[0]} delete")
                                cursor.deleteRow()
                            else:
                                arcpy.AddMessage(f"Unable to determine new geometry for {row[0]}")

                # Update Centerlines
                # Code to snap centerlines to centerline if snapped to poly and poly deleted
                if int(arcpy.management.GetCount(split_layer)[0]) >= 1:
                    new_split = arcpy.management.MakeFeatureLayer(split_center, "new_center_split_lyr", q1)
                    rebuild_centerline(new_split, center_layer, update_field, poly_input_layer, width_np, working_gdb, logger)
            else:
                arcpy.AddMessage("No polygon features found to process and No centerline features found to process.")

        # # Delete temp files
        arcpy.management.Delete([center_layer])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Narrow polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)



def extend_lines_remove_poly(line_FC, polygon_fc, minLakeAreaSize, expression, compare_fcs, working_gdb):
    """ main driver of program """
    try:
        # Local Variables
        delete_OIDs = []

        # Set the workspace
        arcpy.env.workspace = working_gdb
        arcpy.env.overwriteOutput = True
        arcpy.AddMessage("Scratch : " + str(working_gdb))

        desc = arcpy.da.Describe(polygon_fc)
        fc_name = desc['name']
        area_field = desc['areaFieldName']
        oid_field = desc['OIDFieldName']
        # Query for features smaller than minimum size
        query = f" {area_field} <= {minLakeAreaSize}"
        # Add additional SQL query
        if expression:
            query = query + " AND (" + str(expression) +")"
        arcpy.AddMessage(query)
        extractedLakes = arcpy.management.MakeFeatureLayer(polygon_fc, "extractedLakes", query)
        river_layer = arcpy.management.MakeFeatureLayer(line_FC, "river_lyr")
        intersectPolys = int(arcpy.management.GetCount(extractedLakes)[0])
        arcpy.AddMessage(str(intersectPolys) + " polygons to compare")
        if intersectPolys > 1:
            with arcpy.da.SearchCursor(extractedLakes, ["SHAPE@", 'OID@']) as srows:
                for row in srows:
                    centerpt = row[0].centroid
                    geom2Merge = arcpy.PointGeometry(centerpt, arcpy.da.Describe(extractedLakes)['spatialReference'])
                    arcpy.management.SelectLayerByLocation(river_layer, "INTERSECT", row[0], "", "NEW_SELECTION")
                    if int(arcpy.management.GetCount(river_layer)[0]) > 1:
                        arcpy.AddMessage("Updating lines that intersect polygon with OID " + str(row[1]))
                        extendPolyLineToPoint(river_layer, geom2Merge)
                        delete_OIDs.append(row[1])
                    del centerpt
                    del geom2Merge
                    del row

            # For each feature that needs to be deleted
            if len(compare_fcs) >= 1:
                arcpy.AddMessage(str(len(delete_OIDs)) + " features will be deleted")
                if len(delete_OIDs) >= 1:
                    arcpy.AddMessage("Selecting features to Delete")
                    delete_features = arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features")

                    for OID in delete_OIDs:
                        query = oid_field + " = " + str(OID)
                        arcpy.management.SelectLayerByAttribute("delete_features", "ADD_TO_SELECTION", query)

                    # Make layers for compare features
                    secondaryFCNames = []
                    secondaryFCLyrs = []
                    for fc in compare_fcs:
                        # Get just the secondary fc name without path
                        indx = fc.strip("\'")
                        desc = arcpy.da.Describe(indx)
                        fcName = desc['name']
                        secondaryFCNames.append(fcName)

                        # Create layer
                        lyrName = fcName + "Lyr"
                        arcpy.management.MakeFeatureLayer(indx, lyrName)
                        if int(arcpy.management.GetCount(lyrName)[0]) >= 1:
                            secondaryFCLyrs.append(lyrName)

                    result = arcpy.management.GetCount(delete_features)
                    delete_count = int(result.getOutput(0))
                    arcpy.AddMessage("Selected " + str(delete_count) + " features")
                    # Convert enclosed features
                    ConvertEnclosed(delete_features, secondaryFCLyrs, working_gdb)
                    # Convert overlapping features
                    delete_features2 = arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features2")
                    for OID in delete_OIDs:
                        query = oid_field + " = " + str(OID)
                        arcpy.management.SelectLayerByAttribute("delete_features2", "ADD_TO_SELECTION", query)

                    ConvertOverlapping(delete_features2, secondaryFCLyrs, working_gdb)

                    # Convert remaining features
                    delete_features3 = arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features3")
                    for OID in delete_OIDs:
                        query = oid_field + " = " + str(OID)
                        arcpy.management.SelectLayerByAttribute("delete_features3", "ADD_TO_SELECTION", query)
                    result = arcpy.management.GetCount(delete_features3)
                    delete_count = int(result.getOutput(0))
                    if delete_count >= 1:
                        #   Erase the Smaller Polygons from the Source
                        arcpy.AddMessage("Deleting Features")
                        with arcpy.da.UpdateCursor(delete_features3, ['oid@']) as urows:
                            for urow in urows:
                                if urow[0] in delete_OIDs:
                                    urows.deleteRow()
            else:
                # Erase the Smaller Polygons from the Source
                arcpy.AddMessage("Deleting Features")
                with arcpy.da.UpdateCursor(extractedLakes, ['oid@']) as urows:
                    for urow in urows:
                        if urow[0] in delete_OIDs:
                            urows.deleteRow()
        return line_FC

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Extend line and remove polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def delete_small_fc_near_large_fc(polygon_fc, sql, name_field, deleteSize, minSize, distance, Features, working_gdb):
    # Set the workspace
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True
    arcpy.AddMessage("Scratch : " + str(working_gdb))
    try:
        if Features:
            compareFeatures = Features
        else:
            compareFeatures = []
        arcpy.AddMessage("Will be compared to " + str(len(compareFeatures)) + " feature classes")
        # Distance
        distance = float(distance)
        delete_count = 0

        # --- determine query for selecting features ---
        # Find area field
        desc = arcpy.da.Describe(polygon_fc)
        area_field = desc['areaFieldName']
        oid_field = desc['OIDFieldName']
        # Query for features smaller than minimum size
        small_query = str(area_field) + " < " + str(deleteSize)
        # Add additional SQL query
        if sql:
            small_query = small_query + " AND (" + sql + ")"
        # Select only those features with area smaller than minimum size
        arcpy.AddMessage("Selecting small features.")
        if arcpy.Exists("small_features"):
            arcpy.management.Delete("small_features")
        small_features = arcpy.management.MakeFeatureLayer(polygon_fc, "small_features", small_query)

        large_query = str(area_field) + " >= " + str(minSize)
        if arcpy.Exists("large_features"):
            arcpy.management.Delete("large_features")
        large_features = arcpy.management.MakeFeatureLayer(polygon_fc, "large_features", large_query)

        result = arcpy.management.GetCount(small_features)
        count = int(result.getOutput(0))
        deleteOIDs = []

        if count > 0:
            arcpy.AddMessage("Searching for features to delete.")
            # Open update cursor
            with arcpy.da.SearchCursor(large_features, ['oid@', 'SHAPE@', name_field]) as cursor:
                for row in cursor:
                    oid = row[0]
                    geo = row[1]
                    name = row[2]
                    # Find all features, less than minSize that are within distance of selected feature
                    geo = geo.buffer(distance)
                    if len(compareFeatures) >= 1:
                        with arcpy.da.SearchCursor(small_features, ['oid@', 'SHAPE@', name_field]) as search_cursor:
                            for search_row in search_cursor:
                                # Feature features that touch the buffer...
                                if search_row[0] != oid:
                                    if not geo.disjoint(search_row[1]):
                                        #... elimate the feature we are looking at
                                        if search_row[0] not in deleteOIDs:
                                            #...And have same name
                                            if search_row[2] == name:
                                                arcpy.AddMessage(str(search_row[0]) + " will be deleted.  Name is the same as large feature.")
                                                deleteOIDs.append(search_row[0])

                                            #... or have no name
                                            elif search_row[2] == '' or not search_row[2]:
                                                arcpy.AddMessage(str(search_row[0]) + " will be deleted.  Name is blank.")
                                                deleteOIDs.append(search_row[0])
                    else:
                        with arcpy.da.UpdateCursor(small_features, ['oid@', 'SHAPE@', name_field]) as up_cursor:
                            for up_row in up_cursor:
                                if up_row[0] != oid:
                                    # If the geometry of the feature touces the buffer
                                    if not geo.disjoint(up_row[1]):
                                        # And the name matches
                                        if up_row[2] == name:
                                            arcpy.AddMessage(str(up_row[0]) + " will be deleted.  Name is the same as large feature.")
                                            up_cursor.deleteRow()
                                            delete_count += 1
                                        # Of the name is blank
                                        elif up_row[2] == '' or not up_row[2]:
                                            arcpy.AddMessage(str(up_row[0]) + " will be deleted.  Name is blank.")
                                            up_cursor.deleteRow()
                                            delete_count += 1

            if len(deleteOIDs) >= 1:
                arcpy.AddMessage("Deleting Features")
                delete_features = arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features")

                for OID in deleteOIDs:
                    query = oid_field + " = " + str(OID)
                    arcpy.management.SelectLayerByAttribute("delete_features", "ADD_TO_SELECTION", query)

                # Make layers for compare features
                secondaryFCNames = []
                secondaryFCLyrs = []
                for fc in compareFeatures:
                    # Get just the secondary fc name without path
                    indx = str(fc).rfind("\\")
                    fcName = str(fc)[indx+1:]
                    secondaryFCNames.append(fcName)
                    # Create layers
                    lyrName = fcName + "Lyr"
                    arcpy.management.MakeFeatureLayer(fc, lyrName)
                    arcpy.management.SelectLayerByLocation(lyrName, "INTERSECT", delete_features)
                    secondaryFCLyrs.append(lyrName)

                result = arcpy.management.GetCount(delete_features)
                delete_count = int(result.getOutput(0))

                arcpy.AddMessage("Selected " + str(delete_count) + " features")
                # Convert enclosed features
                ConvertEnclosed(delete_features, secondaryFCLyrs, working_gdb)
                # Convert overlapping features
                delete_features2 = arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features2")
                for OID in deleteOIDs:
                    query = oid_field + " = " + str(OID)
                    arcpy.management.SelectLayerByAttribute("delete_features2", "ADD_TO_SELECTION", query)

                ConvertOverlapping(delete_features2, secondaryFCLyrs, working_gdb)

                arcpy.management.MakeFeatureLayer(polygon_fc, "delete_features3")
                for OID in deleteOIDs:
                    query = oid_field + " = " + str(OID)
                    arcpy.management.SelectLayerByAttribute("delete_features3", "ADD_TO_SELECTION", query)
                if int(arcpy.management.GetCount("delete_features3")[0]) >= 1:
                    arcpy.management.DeleteFeatures("delete_features3")

        arcpy.AddMessage("Deleted " + str(delete_count) + " features.")
        # Clean up
        clean_list = ["small_features", "large_features", "delete_features", "delete_features2", "delete_features3"]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete small fc near large fc error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def aggregare_polygons(input_polygons, sql, name_field, min_size, distance, working_gdb):
    """Main Function. Determines which features to aggreate based on the parameters entered."""
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb
    try:
        delete_total = 0
        # Determine query for selecting features
        # Find and delimit system fields
        desc = arcpy.da.Describe(input_polygons)
        fc_name = desc['name']
        # Query for features smaller than minimum size
        small_query = ""
        if min_size:
            small_query = "{0} <= {1}".format(desc['areaFieldName'], min_size)

            if sql:
                small_query += " AND " + sql
        else:
            small_query = sql

        small_features = arcpy.management.MakeFeatureLayer(input_polygons, f"{working_gdb}\\small_{fc_name}", small_query)
        aggr_feats = arcpy.management.MakeFeatureLayer(input_polygons, f"{working_gdb}\\aggregate_{fc_name}", small_query)
        null_query = build_name_query(input_polygons, "", name_field)
        
        # If features meet the selection criteria
        if int(arcpy.management.GetCount(small_features)[0]) > 0:
            arcpy.AddMessage("Searching for features to aggregate")
            # Open update cursor on all features in polygon feature class
            with arcpy.da.SearchCursor(small_features, ['oid@', 'SHAPE@', name_field], sql_clause=(None, "ORDER BY " + name_field + " DESC")) as cursor:
                for row in cursor:
                    oid = row[0]
                    geo = row[1]
                    name = str(row[2])

                    if name:
                        name_query = build_name_query(input_polygons, name, name_field)
                    else:
                        name_query = null_query

                    select_count = 1
                    prev_count = 0

                    # keep looping while there are still features being selected
                    while select_count > prev_count:
                        prev_count = select_count
                        # Select the features near the record
                        arcpy.management.SelectLayerByLocation(aggr_feats, "INTERSECT", geo, distance)
                        # Remove any selected features that don't have
                        # the correct name value
                        arcpy.management.SelectLayerByAttribute(aggr_feats, "SUBSET_SELECTION", name_query)
                        select_count = int(arcpy.management.GetCount(aggr_feats)[0])

                    #there will always be one feature (the feature selected) if there is more than one feature, merge them
                    if select_count > 1:
                        arcpy.AddMessage("Aggregating " + str(select_count - 1) + " features with OID " + str(oid))
                        distance1 = distance + distance
                        delete_ct = merge(aggr_feats, distance1, name_field, working_gdb)

                        delete_total = delete_total + delete_ct

            arcpy.AddMessage(str(delete_total) + " features were merged to" + " other features and deleted...")
        else:
            arcpy.AddIDMessage("INFORMATIVE", 401)

        # Delete temp files
        clean_list = [small_features, aggr_feats]
        arcpy.management.Delete(clean_list)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Aggregation polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def convert_type(in_FC, SQL, minimumLength, out_FC, out_Subtype, connect, working_gdb):
    # Set the workspace
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True
    arcpy.AddMessage("scratchWorkspace " + str(arcpy.env.scratchWorkspace))
    try:
        # Make a layer from the feature class
        inFCLyr = "inFCLyr"
        arcpy.management.MakeFeatureLayer(in_FC, inFCLyr)

        desc = arcpy.da.Describe(in_FC)
        shapelength = desc['lengthFieldName']

        # Get features that meet selection criteria
        arcpy.AddMessage("Filtering selection criteria")
        where_clause = ""
        if minimumLength:
            where_clause = shapelength + "  < " +  str(minimumLength)
            if SQL:
                where_clause = where_clause + " AND (" + SQL + ")"
        else:
            if SQL:
                where_clause = SQL
        arcpy.management.SelectLayerByAttribute(inFCLyr, "", where_clause)
        if connect:
            outLyr = arcpy.management.MakeFeatureLayer(out_FC, "OutLyr")
            arcpy.management.SelectLayerByLocation(inFCLyr, "INTERSECT", outLyr, "", "SUBSET_SELECTION")

        count = int(arcpy.management.GetCount(inFCLyr).getOutput(0))

        arcpy.AddMessage(str(count) + " features meet criteria and will be converted.")
        if count >= 1:
            if out_Subtype:
                arcpy.AddMessage("Appending feature to subtype " + out_Subtype)
                arcpy.management.Append(inFCLyr, out_FC, "NO_TEST", "", out_Subtype)
            else:
                arcpy.AddMessage("Appending features to fc " + out_FC)
                arcpy.management.Append(inFCLyr, out_FC, "NO_TEST")

            arcpy.AddMessage("Deleting features from input.")
            arcpy.management.DeleteFeatures(inFCLyr)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Convert type error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def increase_line_length(damFC, sql, minimum_length, working_gdb):
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        arcpy.env.workspace = working_gdb
        arcpy.AddMessage("scratchWorkspace " + str(working_gdb))

        # Input Params
        SQL = sql
        minimumLength = minimum_length

        # Make a layer from the feature class
        damFCLyr = "damFCLyr"
        arcpy.management.MakeFeatureLayer(damFC, damFCLyr)

        # Get features that meet selection criteria
        desc = arcpy.da.Describe(damFC)
        shapelength = desc['lengthFieldName']
        where_clause = f"{shapelength} < {minimumLength}"
        if SQL:
            where_clause = where_clause + " AND (" + SQL + ")"

        with arcpy.da.SearchCursor(damFCLyr, ["OID@", "SHAPE@", shapelength], where_clause) as cursor:
            for row in cursor:
                origPolyLineGeometry = row[1]
                # Get start and end points of line
                origStartPnt = origPolyLineGeometry.firstPoint
                origEndPnt = origPolyLineGeometry.lastPoint

                # Get center X,Y point of line
                centerPntGeom = origPolyLineGeometry.positionAlongLine(0.5, True)
                centerPnt = centerPntGeom.firstPoint
                arcpy.AddMessage("centerPnt {0} {1} for OID {2}".format(centerPntGeom.firstPoint.X, centerPntGeom.firstPoint.Y, row[0]))

                # Calculate the angle between this line and x-axix
                radian = math.atan2((origPolyLineGeometry.lastPoint.Y - origPolyLineGeometry.firstPoint.Y), (origPolyLineGeometry.lastPoint.X - origPolyLineGeometry.firstPoint.X))

                origLength = origPolyLineGeometry.length
                arcpy.AddMessage("Original Length  = {0} ".format(str(origLength)))

                # Calculate element's new height and width (for half the minimumLength)
                # Make sure absolute value of new height and width is used
                new_wd = math.fabs(minimumLength/2 * math.cos(radian))
                new_ht = math.fabs(minimumLength/2 * math.sin(radian))
                #arcpy.AddMessage("New ht wth  = {0} {1}".format(str(new_ht),str(new_wd)))

                # Calculate the new X,Y
                # for first half of line - end point is already center point of original line
                # for second half of line - start point is already center point of original line
                startPoint = arcpy.Point()
                endPoint = arcpy.Point()

                if origStartPnt.X > origEndPnt.X:
                    startPoint.X = float(centerPnt.X + new_wd)
                    endPoint.X = float(centerPnt.X - new_wd)
                else:
                    startPoint.X = float(centerPnt.X - new_wd)
                    endPoint.X = float(centerPnt.X + new_wd)

                if origStartPnt.Y > origEndPnt.Y:
                    startPoint.Y = float(centerPnt.Y + new_ht)
                    endPoint.Y = float(centerPnt.Y - new_ht)
                else:
                    startPoint.Y = float(centerPnt.Y - new_ht)
                    endPoint.Y = float(centerPnt.Y + new_ht)

                # Create new Polyline
                array = arcpy.Array([startPoint, endPoint])
                newGeom = arcpy.Polyline(array)

                # Including another field 'NAM' in query as placeholder - else update not working
                with arcpy.da.UpdateCursor(damFCLyr, ("NAM", "SHAPE@"), "OBJECTID=" + str(row[0])) as updateCursor:
                    for updtRow in updateCursor:
                        # Update geometry
                        updtRow = (updtRow[0], newGeom)
                        updateCursor.updateRow(updtRow)
                        arcpy.AddMessage("Updated geometry")
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Increase DAM length error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def remove_close_lines(input_lines, sql, distance, per, dangles, delete, visible_field, check_connect, connect_angle, comp_lines, working_gdb):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb
    scratch = working_gdb
    try:
        if not comp_lines:
            comp_lines = input_lines

        if delete == 'false' and not visible_field:
            arcpy.AddError("Must populate visibile field")

        percent_parallel = per / 100
  
        arcpy.management.AddGeometryAttributes(input_lines, "LINE_BEARING")
        line_lyr = arcpy.management.MakeFeatureLayer(input_lines, "line_lyr", sql)
        arcpy.AddMessage(str(int(arcpy.management.GetCount(line_lyr)[0])))
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
                # Convert list of Target FID values to a SQL statement
                value_str = ", ".join(str(v) for v in values)
                where = f"OBJECTID IN ({value_str})"
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
            with arcpy.da.SearchCursor(comp_lines, ['OID@', 'SHAPE@', 'BEARING']) as cursor:
                for row in cursor:
                    geo_dict[row[0]] = [row[1], row[2]]
            line_ignore = []
            length_field = arcpy.da.Describe(input_lines)['lengthFieldName']
            order = "ORDER BY " + length_field
            if delete == 'false':
                fields = [length_field, "OID@", "SHAPE@", "BEARING", visible_field]
            else:
                fields = [length_field, "OID@", "SHAPE@", "BEARING"]
  
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
                                        if ((include_geo.length / geom.length) >= percent_parallel):
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


def remove_dangles_lines(working_gdb, hydro_lines, sql, seg_length, compare_fcs, recursive):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb

    clean_list = []

    try:
        # Denote dangles using points using the
        # Feature Vertices to Points GP tool at dangles
        arcpy.AddMessage("Creating points at dangles...")
        dangles = arcpy.management.FeatureVerticesToPoints(hydro_lines, "dangles", "DANGLE").getOutput(0)
        clean_list.append(dangles)
        # Use Describe function to get SHAPE Length field
        shp_len_fld = arcpy.da.Describe(hydro_lines)['lengthFieldName']
        # Create feature layer of hydro lines where
        # length of segment < seg_length and Name field
        # is an empty string or NULL
        arcpy.AddMessage("Making hydro feature layer...")
    
        where = f"{shp_len_fld} < {seg_length}"
        if sql:
            sql += " AND "  + "(" + where + ")"

        arcpy.management.MakeFeatureLayer(hydro_lines, "hydro", where)
        feature_count = int(arcpy.management.GetCount("hydro")[0])
        if feature_count >= 1:
            if recursive == "true":
                count = 1
                while feature_count >= 1:
                    arcpy.AddMessage("Deleting dangles loop " + str(count))
                    feature_count = delete_dangles("hydro", dangles, seg_length, compare_fcs, working_gdb)
                    arcpy.management.SelectLayerByAttribute("hydro", "NEW_SELECTION", where)
                    count += 1
        else:
            delete_dangles("hydro", dangles, seg_length, compare_fcs, working_gdb)
            arcpy.AddMessage("Dangles Deleted")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete dangles error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def count_features(fc):
    return len([row for row in arcpy.da.SearchCursor(fc, ["OID@"])])

def update_veg_lyr_with_hydro_lyr(hydro_fc_list, veg_lyr_list, working_gdb):
    try:
        # Create a temporary erase layer
        temp_erase_layer = f"{working_gdb}\\temp_erase_layer"
        # Loop through each hydro feature class and vegetation layer
        for hydro_fc in hydro_fc_list:
            for veg_lyr in veg_lyr_list:  
                selected_fcs = arcpy.management.SelectLayerByLocation(veg_lyr, "INTERSECT", hydro_fc, None, "NEW_SELECTION")
                if count_features(selected_fcs) >= 1:
                    temp_erase_layer = arcpy.analysis.PairwiseErase(veg_lyr, hydro_fc, temp_erase_layer)
                    # Delete existing features
                    arcpy.management.DeleteFeatures(veg_lyr)
                    # Append simplified features with main fc
                    arcpy.management.Append(temp_erase_layer, veg_lyr, 'NO_TEST')
        # Delete temp files
        arcpy.management.Delete([temp_erase_layer])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Erase vegetation layer error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


"""
delete_small_hydro_: Used till - 16th February 2026
"""
def delete_small_hydro_(fc_list, delete_small_bldgs, del_min_area, working_gdb, intersecting_fc = None, replacing_fcs = None):
    try:
        delete_small_bldgs = list(filter(str.strip, delete_small_bldgs))
        delete_small_bldgs = [fc for a_lyr in delete_small_bldgs for fc in fc_list if str(a_lyr) in fc]
        # arcpy.AddMessage(f"del_min_area: {del_min_area}")
        # arcpy.AddMessage(f"replacing_fcs: {replacing_fcs}")
        for polygon_fc in delete_small_bldgs:
            if has_features(polygon_fc):
                # Create query
                desc = arcpy.da.Describe(polygon_fc)
                fc_name = desc['name']
                shape_area = desc['areaFieldName']
                query = f"{shape_area} <= {del_min_area}"
                features_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, f"{fc_name}_layer", query)
                # Delete features
                arcpy.management.DeleteFeatures(features_lyr)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete small building error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

"""
delete_small_hydro: Used from 17th February 2026
"""
def delete_small_hydro(
    fc_list,
    delete_small_hydros,
    del_min_area,
    working_gdb,
    intersecting_fc=None,
    replacing_fcs=None
):
    """
    Extends the user's function:
      - delete small hydro polygons
      - if intersecting_fc provided, replace small hydro that intersects intersecting_fc
        with adjacent polygons from replacing_fcs (union hydro geom into best neighbor)
      - modified on: 17th February 2026
    """
    try:
        def _msg(t): arcpy.AddMessage(f"[delete_small_hydro] {t}")
        def _warn(t): arcpy.AddWarning(f"[delete_small_hydro] {t}")
        def _err(t): arcpy.AddError(f"[delete_small_hydro] {t}")

        def _as_list(x):
            if x is None:
                return []
            if isinstance(x, str):
                return [x]
            return list(x)

        # Normalize list inputs
        fc_list = _as_list(fc_list)
        delete_small_hydros = _as_list(delete_small_hydros)
        intersecting_fc = _as_list(intersecting_fc)
        replacing_fcs = _as_list(replacing_fcs)

        # _msg(f"del_min_area={del_min_area}")
        # _msg(f"working_gdb={working_gdb}")
        # _msg(f"intersecting_fc count={len(intersecting_fc)} | replacing_fcs count={len(replacing_fcs)}")

        # Keep your existing filtering logic (substring match)
        delete_small_hydros = list(filter(str.strip, delete_small_hydros))
        delete_small_hydros = [fc for a_lyr in delete_small_hydros for fc in fc_list if str(a_lyr) in fc]
        # _msg(f"Hydro FCs matched from fc_list: {len(delete_small_hydros)}")

        # Replacement enabled only if both intersecting_fc and replacing_fcs exist
        do_replace = bool(intersecting_fc) and bool(replacing_fcs)
        if intersecting_fc and not replacing_fcs:
            raise ValueError("intersecting_fc provided but replacing_fcs is empty. Provide replacing_fcs for replacement.")

        # Build intersecting layer(s) once (as layers) if provided
        intersect_layers = []
        if intersecting_fc:
            for i, ifc in enumerate(intersecting_fc, start=1):
                if not arcpy.Exists(ifc):
                    _warn(f"Intersecting FC missing, skipping: {ifc}")
                    continue
                lyr = f"temp_intersect_{i}"
                arcpy.management.MakeFeatureLayer(ifc, lyr)
                intersect_layers.append(lyr)
            _msg(f"Intersecting layers created: {len(intersect_layers)}")

        # Build replacement layers once
        rep_layers = []
        if do_replace:
            for i, rfc in enumerate(replacing_fcs, start=1):
                if not arcpy.Exists(rfc):
                    _warn(f"Replacing FC missing, skipping: {rfc}")
                    continue
                lyr = f"temp_rep_{i}"
                arcpy.management.MakeFeatureLayer(rfc, lyr)
                rep_layers.append((rfc, lyr))
            _msg(f"Replacement layers created: {len(rep_layers)}")
            if not rep_layers:
                _warn("No valid replacement FCs exist after filtering; replacement will be skipped.")
                do_replace = False

        # Summary for debugging
        summary = {}

        for polygon_fc in delete_small_hydros:
            summary[polygon_fc] = {"small": 0, "deleted": 0, "replace_candidates": 0, "replaced": 0, "skipped_no_neighbor": 0}

            if not arcpy.Exists(polygon_fc):
                _warn(f"Hydro FC missing, skipping: {polygon_fc}")
                continue

            if not has_features(polygon_fc):
                _msg(f"No features in hydro FC, skipping: {polygon_fc}")
                continue

            desc = arcpy.da.Describe(polygon_fc)
            fc_name = desc["name"]
            shape_area = desc["areaFieldName"]  # uses dataset's area field name
            oid_field = desc["OIDFieldName"]

            query = f"{shape_area} <= {float(del_min_area)}"
            _msg(f"Processing hydro FC: {polygon_fc}")
            _msg(f"Small-area query: {query}")

            # Layer of small hydros
            features_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, f"{fc_name}_small_lyr", query)

            small_count = int(arcpy.management.GetCount(features_lyr)[0])
            summary[polygon_fc]["small"] = small_count
            _msg(f"Small hydro selected: {small_count}")

            if small_count == 0:
                arcpy.management.Delete(features_lyr)
                continue

            # If no intersecting_fc: delete all small and continue
            if not do_replace or not intersect_layers:
                _msg("Replacement disabled (no intersecting_fc or no replacing_fcs). Deleting all small hydro.")
                summary[polygon_fc]["deleted"] += small_count
                arcpy.management.DeleteFeatures(features_lyr)
                arcpy.management.Delete(features_lyr)
                continue

            # 1) Identify replace candidates = small hydros that intersect any intersecting layer
            # Start from small selection already in features_lyr
            arcpy.management.SelectLayerByAttribute(features_lyr, "NEW_SELECTION", query)

            # Subset selection to INTERSECT first layer, then ADD from others
            arcpy.management.SelectLayerByLocation(
                features_lyr, "INTERSECT", intersect_layers[0], selection_type="SUBSET_SELECTION"
            )
            for extra in intersect_layers[1:]:
                arcpy.management.SelectLayerByLocation(
                    features_lyr, "INTERSECT", extra, selection_type="ADD_TO_SELECTION"
                )

            replace_count = int(arcpy.management.GetCount(features_lyr)[0])
            summary[polygon_fc]["replace_candidates"] = replace_count
            _msg(f"Replace candidates (small ∩ intersecting): {replace_count}")

            # Capture replace candidate OIDs from the ORIGINAL FC
            replace_oids = [r[0] for r in arcpy.da.SearchCursor(features_lyr, [oid_field])]
            replace_oid_set = set(replace_oids)

            # 2) Delete small hydros NOT intersecting: (small) minus (replace candidates)
            # Re-select all small
            arcpy.management.SelectLayerByAttribute(features_lyr, "NEW_SELECTION", query)

            if replace_oid_set:
                # Remove candidates -> left with delete set
                chunks = [replace_oids[i:i+999] for i in range(0, len(replace_oids), 999)]
                for ch in chunks:
                    arcpy.management.SelectLayerByAttribute(
                        features_lyr, "REMOVE_FROM_SELECTION",
                        f"{oid_field} IN ({','.join(map(str, ch))})"
                    )

            delete_count = int(arcpy.management.GetCount(features_lyr)[0])
            _msg(f"Delete-set (small \\ replace): {delete_count}")

            if delete_count > 0:
                summary[polygon_fc]["deleted"] += delete_count
                _msg(f"Deleting {delete_count} small hydro (non-intersecting).")
                arcpy.management.DeleteFeatures(features_lyr)

            # 3) Replacement pass for replace candidates
            if not replace_oid_set:
                _msg("No replace candidates; done with this hydro FC.")
                arcpy.management.Delete(features_lyr)
                continue

            _msg(f"Starting replacement pass for {len(replace_oid_set)} hydro polygons.")

            # Read hydro geometries by OID
            # (Do it with a where clause in chunks to avoid very long IN clauses.)
            hyd_geom_by_oid = {}
            replace_oid_list = list(replace_oid_set)
            for i in range(0, len(replace_oid_list), 999):
                chunk = replace_oid_list[i:i+999]
                where = f"{oid_field} IN ({','.join(map(str, chunk))})"
                with arcpy.da.SearchCursor(polygon_fc, [oid_field, "SHAPE@"], where_clause=where) as scur:
                    for oid, geom in scur:
                        hyd_geom_by_oid[oid] = geom

            _msg(f"Loaded hydro geometries for replacement: {len(hyd_geom_by_oid)}")

            # For each hydro poly, choose best adjacent polygon among replacing_fcs
            # Metric: shared boundary length (hydro boundary ∩ candidate boundary).
            # If nothing shares boundary, we skip (and log).
            hydros_replaced = []

            for hyd_oid, hyd_geom in hyd_geom_by_oid.items():
                if not hyd_geom:
                    continue

                hyd_boundary = hyd_geom.boundary()

                best = None  # (shared_len, rep_fc, rep_oid)
                for rep_fc, rep_lyr in rep_layers:
                    # Select candidates that at least intersect hydro (fast prefilter)
                    arcpy.management.SelectLayerByAttribute(rep_lyr, "CLEAR_SELECTION")
                    arcpy.management.SelectLayerByLocation(rep_lyr, "INTERSECT", hyd_geom, selection_type="NEW_SELECTION")

                    cand_count = int(arcpy.management.GetCount(rep_lyr)[0])
                    if cand_count == 0:
                        continue

                    rep_oid_field = arcpy.da.Describe(rep_fc)["OIDFieldName"]
                    with arcpy.da.SearchCursor(rep_lyr, [rep_oid_field, "SHAPE@"]) as rcur:
                        for roid, rgeom in rcur:
                            if not rgeom:
                                continue

                            # shared boundary length
                            inter_line = hyd_boundary.intersect(rgeom.boundary(), 2)  # 2 = polyline
                            shared_len = inter_line.length if inter_line else 0.0
                            if shared_len <= 0:
                                continue

                            if (best is None) or (shared_len > best[0]):
                                best = (shared_len, rep_fc, roid)

                if best is None:
                    summary[polygon_fc]["skipped_no_neighbor"] += 1
                    continue

                shared_len, best_rep_fc, best_rep_oid = best
                rep_oid_field = arcpy.da.Describe(best_rep_fc)["OIDFieldName"]

                # Update the best replacement polygon: union with hydro geometry
                where = f"{rep_oid_field} = {int(best_rep_oid)}"
                updated = False
                with arcpy.da.UpdateCursor(best_rep_fc, ["SHAPE@"], where_clause=where) as ucur:
                    for (g,) in ucur:
                        if g:
                            ucur.updateRow((g.union(hyd_geom),))
                            updated = True

                if updated:
                    hydros_replaced.append(hyd_oid)

            replaced_count = len(hydros_replaced)
            summary[polygon_fc]["replaced"] += replaced_count
            _msg(f"Replacement polygons updated: {replaced_count}")
            if summary[polygon_fc]["skipped_no_neighbor"]:
                _warn(f"Hydro polys skipped (no adjacent replacement polygon): {summary[polygon_fc]['skipped_no_neighbor']}")

            # Delete the hydro polygons that were successfully replaced
            if hydros_replaced:
                del_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, f"{fc_name}_del_replaced_lyr")
                arcpy.management.SelectLayerByAttribute(del_lyr, "CLEAR_SELECTION")
                for i in range(0, len(hydros_replaced), 999):
                    chunk = hydros_replaced[i:i+999]
                    arcpy.management.SelectLayerByAttribute(
                        del_lyr, "ADD_TO_SELECTION",
                        f"{oid_field} IN ({','.join(map(str, chunk))})"
                    )
                _msg(f"Deleting replaced hydro polygons: {len(hydros_replaced)}")
                arcpy.management.DeleteFeatures(del_lyr)
                arcpy.management.Delete(del_lyr)

            # Cleanup
            arcpy.management.Delete(features_lyr)
            _msg(f"Done hydro FC: {polygon_fc} | {summary[polygon_fc]}")

        # Cleanup shared layers
        for lyr in intersect_layers:
            try:
                arcpy.management.Delete(lyr)
            except Exception:
                pass
        for _, lyr in rep_layers:
            try:
                arcpy.management.Delete(lyr)
            except Exception:
                pass

        _msg(f"ALL DONE. Summary: {summary}")
        return summary

    except Exception as ex:
        arcpy.AddError(f"[delete_small_hydro] Failed: {ex}")
        raise



def replace_polygon_with_line_hydro_feature(poly_feature, line_feature, working_gdb, logger, smooth_tolerance = 10):
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True

    # Spatial Join between lake and river
    lake_river_spatial_joined_fc = arcpy.analysis.SpatialJoin(poly_feature, line_feature, f"{working_gdb}\\lake_river_spatial_joined_fc")
    # Make feature layer from spatially joined layer
    arcpy.management.MakeFeatureLayer(lake_river_spatial_joined_fc, "lake_river_spatial_joined_fc")
    
    # Query gte 2
    arcpy.management.SelectLayerByAttribute("lake_river_spatial_joined_fc","NEW_SELECTION", f"Join_Count < 2")
    # Delete selected features
    arcpy.management.DeleteFeatures("lake_river_spatial_joined_fc")
    # Collapse hydro polygon
    collapsed_hydro_polygon = arcpy.cartography.CollapseHydroPolygon(lake_river_spatial_joined_fc, f"{working_gdb}\\collapsed_hydro_polygon", "NO_MERGE", [line_feature])
    # Smooth line
    hydro_smooth_line = arcpy.cartography.SmoothLine(collapsed_hydro_polygon, f"{working_gdb}\\hydro_smooth_line", "PAEK", f"{smooth_tolerance} Meters")
    # Append smooth line with river
    arcpy.management.Append(hydro_smooth_line, line_feature, "NO_TEST")

    arcpy.topographic.MergeLinesByPseudoNode(line_feature)

    return None


# # Hydrography Generalization
def gen_hydrography(fc_list, hydro_prep_fc_list, working_gdb, polygon_input_list, centerline_input_list, 
                            topo_fcs_list, generalize_operations, in_feature_loc, hydro_remove_near_poly_list, 
                            hydro_enlarge_poly_list, hydro_remove_small_poly_list,  hydro_erase_poly_list,  hydro_small_line_fc_list, hydro_small_point_fc_list, 
                            hydro_delete_small_pools, val_dict, logger):
    arcpy.AddMessage('Starting hydrography features generalization.....')
    # Set the workspace
    arcpy.env.overwriteOutput = True
    dynamic_fc_names = resolve_lyr()
    try:
        
        hydro_prep_fc_list = list(filter(str.strip, hydro_prep_fc_list))
        hydro_prep_fc_list = [fc for a_lyr in hydro_prep_fc_list for fc in fc_list if str(a_lyr) in fc]
        river = [fc for fc in fc_list if dynamic_fc_names.River_L in fc][0]
        river_bank = [fc for fc in fc_list if dynamic_fc_names.River_Bank_L in fc][0]
        irrigation = [fc for fc in fc_list if dynamic_fc_names.Irrigation_Canal_L in fc][0]
        irrigation_edge = [fc for fc in fc_list if dynamic_fc_names.Irrigation_Canal_Edge_L in fc][0]
        sea_coverage = [fc for fc in fc_list if dynamic_fc_names.Sea_Coverage_A in fc][0]
        irrigation_canal_cover_list = [fc for fc in fc_list if dynamic_fc_names.Irrigation_Canal_Coverage_A in fc]
        topo_fcs_list = list(filter(str.strip, topo_fcs_list))
        topo_fcs = [fc for a_lyr in topo_fcs_list for fc in fc_list if str(a_lyr) in fc]
        pond = [fc for fc in fc_list if dynamic_fc_names.Pond_A in fc][0]
        lake = [fc for fc in fc_list if dynamic_fc_names.Lake_A in fc][0]
        river_coverage = [fc for fc in fc_list if dynamic_fc_names.River_Coverage_A in fc][0]
        irrigation_canal_cover = [fc for fc in fc_list if dynamic_fc_names.Irrigation_Canal_Coverage_A in fc][0]
        topology_fcs = [fc for a_lyr in topo_fcs_list for fc in fc_list if str(a_lyr) in fc]

        aoi = f"{in_feature_loc}\\AOI"
        topology_fcs01 = [river_coverage, sea_coverage, aoi]
        topology_fcs02 = [irrigation_canal_cover, sea_coverage, aoi]

        hydro_remove_near_poly_list = list(filter(str.strip, hydro_remove_near_poly_list))
        hydro_remove_near_poly_list = [fc for a_lyr in hydro_remove_near_poly_list for fc in fc_list if str(a_lyr) in fc]
        # # Hydro preparation
        hydro_prep(river, hydro_prep_fc_list)
        hydro_prep(irrigation, irrigation_canal_cover_list)

        replace_polygon_with_line_hydro_feature(lake, river, working_gdb, logger, val_dict["Hydrography_replace_poly_with_line_smooth_tolerance"])
   
        # # Hydro Remove Short Lines Connecting Polygons
        
        river_coverage = [fc for fc in fc_list if dynamic_fc_names.River_Coverage_A in fc][0]
        remove_short_lines_connecting_polys(river, pond, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_remove_short_line_line_length'], working_gdb)
        remove_short_lines_connecting_polys(river, lake, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_remove_short_line_line_length'], working_gdb)

        # Hydro narrow polygons
        narrow_polygons_new(fc_list, polygon_input_list, centerline_input_list, val_dict['Hydrography_np_polygon_width'], 
                            val_dict['Hydrography_np_polygon_percentage'], val_dict['Resolve_conflict_build_visible_field'], topo_fcs, working_gdb, logger)
        # # Hydro generalize shared
        polygon_input_list = [fc for topo in [dynamic_fc_names.River_Coverage_A, dynamic_fc_names.Irrigation_Canal_Coverage_A, dynamic_fc_names.Pond_A, dynamic_fc_names.Lake_A, 
                                              dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A, dynamic_fc_names.Log_Pond_A] for fc in fc_list if str(topo) in fc]
        centerline_input_list = [fc for topo in [dynamic_fc_names.River_L, dynamic_fc_names.Irrigation_Canal_L] for fc in fc_list if str(topo) in fc]
        for line_fc, poly_fc in zip(centerline_input_list, polygon_input_list):
            desc = arcpy.da.Describe(line_fc)
            fc_name = desc['name']
            features_lyr = arcpy.management.MakeFeatureLayer(line_fc, f"features_lyr_{fc_name}")
            arcpy.management.SelectLayerByLocation(features_lyr, 'INTERSECT', poly_fc, "2 Meters", 'NEW_SELECTION')
            arcpy.management.SelectLayerByLocation(features_lyr, 'INTERSECT', poly_fc, "0 Meters", 'REMOVE_FROM_SELECTION')
            snap_env = [poly_fc, "EDGE", "2 Meters"]
            arcpy.edit.Snap(features_lyr, [snap_env])

        # # Delete Small Hydro features that are connected to river and needs to be replaced with nearby vegetation
        delete_small_hydro(fc_list, hydro_delete_small_pools, val_dict["Hydrography_delete_small_pool_min_area"], working_gdb, river, topo_fcs)
        # # Delete Small Hydro features that are not connected to river and needs to be replaced with nearby vegetation
        delete_small_hydro(fc_list, [dynamic_fc_names.Pond_A, dynamic_fc_names.Lake_A], val_dict["Hydrography_delete_small_pool_min_area"], working_gdb, None, topo_fcs)

        update_veg_lyr_with_hydro_lyr(polygon_input_list, topo_fcs, working_gdb)
        # Generalize shared features
        topology_fcs.insert(0, lake)
        gen_shared_features(lake, generalize_operations, val_dict['Hydrography_Hydro_Gen_simple_tolerance'], val_dict['Hydrography_Hydro_Gen_smooth_tolerance'], working_gdb, topology_fcs, None)
        topology_fcs.remove(lake)
        topology_fcs.insert(0, pond)
        gen_shared_features(pond, generalize_operations, val_dict['Hydrography_Hydro_Gen_simple_tolerance'], val_dict['Hydrography_Hydro_Gen_smooth_tolerance'], working_gdb, topology_fcs, None)
        topology_fcs.remove(pond)
        topology_fcs.insert(0, river_coverage)
        gen_shared_features(river_coverage, generalize_operations, val_dict['Hydrography_Hydro_Gen_simple_tolerance'], val_dict['Hydrography_Hydro_Gen_smooth_tolerance'], working_gdb, topology_fcs, None)
        topology_fcs.remove(river_coverage)
        topology_fcs.insert(0, irrigation_canal_cover)
        gen_shared_features(irrigation_canal_cover, generalize_operations, val_dict['Hydrography_Hydro_Gen_simple_tolerance'], val_dict['Hydrography_Hydro_Gen_smooth_tolerance'], working_gdb, topology_fcs, None)
        topology_fcs.remove(irrigation_canal_cover)
        
        # Determination and Reconnecting
        part_01_lk = (arcpy.da.Describe(lake)['name']).split("_")[1]
        part_01_pnd = (arcpy.da.Describe(pond)['name']).split("_")[1]
        part_02_r = (arcpy.da.Describe(river)['name']).split("_")[1]

        out_name_1 = f"{part_01_lk}_{part_02_r}_rlc"
        out_name_2 = f"{part_02_r}_{part_01_pnd}_rlc"

        out_table1 = working_gdb + "\\" + out_name_1
        out_table2 = working_gdb + "\\" + out_name_2

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

        line_field_river = "FID_" + arcpy.da.Describe(river)['name']
        poly_field_lake = "FID_" + arcpy.da.Describe(lake)['name']
        poly_field_pond = "FID_" + arcpy.da.Describe(pond)['name']

        # Run the determine function
        # For River-Pond
        determine(river, pond, out_table2, line_field_river, poly_field_pond, working_gdb)
        reconnect_touching(pond, river, out_table2, val_dict['Hydrography_hydro_trim_update_val'])
        # For River-Lake
        determine(river, lake, out_table1, line_field_river, poly_field_lake, working_gdb)
        reconnect_touching(lake, river, out_table1, val_dict['Hydrography_hydro_trim_update_val'])

        # Recreate boundary lines
        
        recreate_boundary_lines(river_bank, river_coverage, topology_fcs01)
        recreate_boundary_lines(irrigation_edge, irrigation_canal_cover, topology_fcs02)

        # Make feature layer
        arcpy.management.MakeFeatureLayer(irrigation_edge, "irrigation_bnd_edge")
        arcpy.management.MakeFeatureLayer(river_bank, "river_bank_bnd")
        # Selection and delete features
        selected_irrigation_edge = arcpy.management.SelectLayerByLocation("irrigation_bnd_edge", 'SHARE_A_LINE_SEGMENT_WITH', aoi, None, 'NEW_SELECTION')
        selected_river_bank = arcpy.management.SelectLayerByLocation("river_bank_bnd", 'SHARE_A_LINE_SEGMENT_WITH', aoi, None, 'NEW_SELECTION')
        arcpy.management.DeleteFeatures(selected_irrigation_edge)
        arcpy.management.DeleteFeatures(selected_river_bank)
        # Ditermine touching hydro
        part_01_lk = (arcpy.da.Describe(lake)['name']).split("_")[1]
        part_01_pnd = (arcpy.da.Describe(pond)['name']).split("_")[1]
        part_02_r = (arcpy.da.Describe(river)['name']).split("_")[1]
 
        out_name_1 = f"{part_01_lk}_{part_02_r}_touch"
        out_name_2 = f"{part_01_pnd}_{part_02_r}_touch"

        out_table1 = working_gdb + "\\" + out_name_1
        out_table2 = working_gdb + "\\" + out_name_2

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

        line_field_river = "FID_" + arcpy.da.Describe(river)['name']
        poly_field_lake = "FID_" + arcpy.da.Describe(lake)['name']
        poly_field_pond = "FID_" + arcpy.da.Describe(pond)['name']

        ##---Run the determine function---##
        # For River-Pond
        determine(river, pond, out_table2, line_field_river, poly_field_pond, working_gdb)
        # For River-Lake
        determine(river, lake, out_table1, line_field_river, poly_field_lake, working_gdb)        
        ## Hydro remove small polygons between lines
        #  Make feature layer
        arcpy.management.MakeFeatureLayer(lake, "lake_lyr", val_dict['Hydrography_hydro_remove_small_poly_exp'])
        arcpy.management.MakeFeatureLayer(pond, "pond_lyr", val_dict['Hydrography_hydro_remove_small_poly_exp'])
        river_fc1 = extend_lines_remove_poly(river, "lake_lyr", val_dict['Hydrography_hydro_remove_small_poly_mim_area'], False, topo_fcs, working_gdb)
        river_fc2 = extend_lines_remove_poly(river_fc1, "pond_lyr", val_dict['Hydrography_hydro_remove_small_poly_mim_area'], False, topo_fcs, working_gdb)

        # Hydro enlarge polygons touching lines
        global_position_station = [fc for fc in fc_list if dynamic_fc_names.Global_Navigation_Satellite_System_Station_P in fc][0]
        base_pont = [fc for fc in fc_list if dynamic_fc_names.Base_Point_P in fc][0]
        trigonometric_station = [fc for fc in fc_list if dynamic_fc_names.Trigonometry_Station_P in fc][0]

        # CHANGES IN 100K COMMENT OUT THE REPAIR GEOMETRY SECTION FOR GEN_HYDRO
        # # Repair geometry
        # arcpy.management.RepairGeometry(river, "DELETE_NULL", "ESRI")
        # arcpy.management.RepairGeometry(pond, "DELETE_NULL", "ESRI")
        # arcpy.management.RepairGeometry(lake, "DELETE_NULL", "ESRI")
        # arcpy.management.RepairGeometry(global_position_station, "DELETE_NULL", "ESRI")
        # arcpy.management.RepairGeometry(base_pont, "DELETE_NULL", "ESRI")
        # arcpy.management.RepairGeometry(trigonometric_station, "DELETE_NULL", "ESRI")
        # CHANGES END IN 100K FOR GEN_HYDRO

        # Make feature layer
        river_lyr = arcpy.management.MakeFeatureLayer(river, f"{working_gdb}\\river_lyr")
        pond_lyr = arcpy.management.MakeFeatureLayer(pond, f"{working_gdb}\\pond_lyr")
        lake_lyr = arcpy.management.MakeFeatureLayer(lake, f"{working_gdb}\\lake_lyr")
        global_position_station_lyr = arcpy.management.MakeFeatureLayer(global_position_station, f"{working_gdb}\\global_position_station_lyr")
        base_pont_lyr = arcpy.management.MakeFeatureLayer(base_pont, f"{working_gdb}\\base_pont_lyr")
        trigonometric_station_lyr = arcpy.management.MakeFeatureLayer(trigonometric_station, f"{working_gdb}\\trigonometric_station_lyr")

        enlarge_barrier_fcs01 = [global_position_station_lyr, base_pont_lyr, trigonometric_station_lyr]
        enlarge_barrier_fcs02 = [pond_lyr, base_pont_lyr, global_position_station_lyr, lake_lyr]
   
        enlarge_polygon_barrier(lake_lyr, None, river_lyr, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_poly_buffer_dist'], enlarge_barrier_fcs01, working_gdb)
        enlarge_polygon_barrier(pond_lyr, None, river_lyr, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_poly_buffer_dist'], enlarge_barrier_fcs02, working_gdb)

        # Hydro remove near polygons

        for polygon_fc in hydro_remove_near_poly_list:
            if any(island in polygon_fc for island in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A]):
                delete_small_fc_near_large_fc(polygon_fc, None, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_delete_size'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], 
                                          None, working_gdb)
            elif dynamic_fc_names.Pond_A in polygon_fc:
                delete_small_fc_near_large_fc(polygon_fc, None, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_delete_size'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], 
                                        topo_fcs, working_gdb)
            elif dynamic_fc_names.Lake_A in polygon_fc:
                delete_small_fc_near_large_fc(polygon_fc, None, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_delete_size'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], 
                                        topo_fcs, working_gdb)
            else:
                delete_small_fc_near_large_fc(polygon_fc, None, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_delete_size'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], 
                                        None, working_gdb)
            
        # Hydro merge near polygons
        for input_polygons in hydro_remove_near_poly_list:
            if any(island in input_polygons for island in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A]):
                aggregare_polygons(input_polygons, val_dict['Hydrography_hydro_remove_near_poly_sql'], val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], working_gdb)
            else:
                aggregare_polygons(input_polygons, None, val_dict['Resolve_conflict_line_name_field'], val_dict['Hydrography_hydro_remove_near_poly_min_size'], val_dict['Hydrography_hydro_remove_near_poly_dist'], working_gdb)

        # Hydro enlarge polygons untouching
        hydro_enlarge_untch_poly_list = list(filter(str.strip, hydro_enlarge_poly_list))
        hydro_enlarge_untch_poly_list = [fc for a_lyr in hydro_enlarge_untch_poly_list for fc in fc_list if str(a_lyr) in fc]
        for polygon_fc in hydro_enlarge_untch_poly_list:
            if any(island in polygon_fc for island in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A]):
                enlarge_barrier_fcs01.append(polygon_fc)
                polygon_fc_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, "island_fc_lyr", val_dict['Hydrography_hydro_enlarge_poly_sql'])
                enlarge_polygon_barrier(polygon_fc_lyr, None, None, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_untouch_poly_buffer_dist'], enlarge_barrier_fcs01, working_gdb)
                enlarge_barrier_fcs01.remove(polygon_fc)
            elif dynamic_fc_names.Pond_A in polygon_fc:
                enlarge_barrier_fcs01.append(pond)
                enlarge_barrier_fcs01.append(lake)
                polygon_fc_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, "pond_fc_lyr", val_dict['Hydrography_hydro_enlarge_poly_sql'])
                enlarge_polygon_barrier(polygon_fc_lyr, None, None, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_untouch_poly_buffer_dist'], enlarge_barrier_fcs01, working_gdb)
                enlarge_barrier_fcs01.remove(pond)
                enlarge_barrier_fcs01.remove(lake)
            elif dynamic_fc_names.Lake_A in polygon_fc:
                enlarge_barrier_fcs01.append(pond)
                enlarge_barrier_fcs01.append(lake)
                polygon_fc_lyr = arcpy.management.MakeFeatureLayer(polygon_fc, "lake_fc_lyr", val_dict['Hydrography_hydro_enlarge_poly_sql'])
                enlarge_polygon_barrier(polygon_fc_lyr, None, None, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_untouch_poly_buffer_dist'], enlarge_barrier_fcs01, working_gdb)
                enlarge_barrier_fcs01.remove(pond)
                enlarge_barrier_fcs01.remove(lake)
            else:
                enlarge_barrier_fcs01.append(polygon_fc)
                enlarge_polygon_barrier(polygon_fc, None, None, val_dict['Hydrography_hydro_enlarge_poly_min_size'], val_dict['Hydrography_hydro_enlarge_untouch_poly_buffer_dist'], enlarge_barrier_fcs01, working_gdb)
                enlarge_barrier_fcs01.remove(polygon_fc)
        # Hydro dissolve touching polygons
        for poly_fc in hydro_enlarge_untch_poly_list:
            merge_touching_features_new(poly_fc, None, val_dict['Resolve_conflict_line_name_field'], working_gdb)
        # # Hydro trim between polygons
        island = [fc for fc in fc_list if any(islandelm in fc for islandelm in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A])][0]
        trim_polygon_within_distance(island, val_dict['Resolve_conflict_line_name_field'], None, 
                                     val_dict['Hydrography_hydro_trim_between_polygon_distance'], val_dict['Hydrography_hydro_trim_between_polygon_min_area'], val_dict['Hydrography_hydro_trim_update_val'], working_gdb)
        trim_polygon_within_distance(lake, val_dict['Resolve_conflict_line_name_field'], None, 
                                     val_dict['Hydrography_hydro_trim_between_polygon_distance'], val_dict['Hydrography_hydro_trim_between_polygon_min_area'], val_dict['Hydrography_hydro_trim_update_val'], working_gdb)
        trim_polygon_within_distance(pond, val_dict['Resolve_conflict_line_name_field'], None, 
                                     val_dict['Hydrography_hydro_trim_between_polygon_distance'], val_dict['Hydrography_hydro_trim_between_polygon_min_area'], val_dict['Hydrography_hydro_trim_update_val'], working_gdb)

        # Reconnect Touching Hydro
        pond = [fc for fc in fc_list if dynamic_fc_names.Pond_A in fc][0]
        lake = [fc for fc in fc_list if dynamic_fc_names.Lake_A in fc][0]
        river = [fc for fc in fc_list if dynamic_fc_names.River_L in fc][0]
        out_table1 = f"{working_gdb}\\Pond_River_Touch"
        out_table2 = f"{working_gdb}\\Lake_River_Touch"  
        reconnect_touching(pond, river, out_table1, val_dict['Hydrography_hydro_trim_update_val'])
        reconnect_touching(lake, river, out_table2, val_dict['Hydrography_hydro_trim_update_val'])

        # Hydro remove small polygon by converting
        hydro_remove_small_poly_list = list(filter(str.strip, hydro_remove_small_poly_list))
        hydro_remove_small_poly_list = [fc for a_lyr in hydro_remove_small_poly_list for fc in fc_list if str(a_lyr) in fc]

        input_secondary01 = [lake, sea_coverage, river_coverage, pond]
        input_secondary02 = [lake, river_coverage] + topo_fcs
        
        # Convert polygons
        for p_fc in hydro_remove_small_poly_list:
            if any(island in p_fc for island in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A]):
                convert_polygon(p_fc, input_secondary01, val_dict['Hydrography_hydro_remove_small_min_size'], val_dict['Hydrography_hydro_remove_small_sql'], working_gdb)
            elif 'HA0130_Intertidal_Flat_A' in p_fc:
                convert_polygon(p_fc, input_secondary02, val_dict['Hydrography_hydro_remove_small_min_size'], None, working_gdb)
            elif 'HH0310_Swamp_A' in p_fc:
                convert_polygon(p_fc, topo_fcs, val_dict['Hydrography_hydro_remove_small_min_size'], None, working_gdb)
            elif 'HH0080_Sand_Bar_A' in p_fc:
                convert_polygon(p_fc, topo_fcs, val_dict['Hydrography_hydro_remove_small_min_size'], None, working_gdb)
            else:
                convert_polygon(p_fc, topo_fcs, val_dict['Hydrography_hydro_remove_small_min_size'], val_dict['Hydrography_hydro_remove_small_sql'], working_gdb)

        # Hydro erase polygons
        hydro_erase_poly_list = list(filter(str.strip, hydro_erase_poly_list))
        hydro_erase_poly_list = [fc for a_lyr in hydro_erase_poly_list for fc in fc_list if str(a_lyr) in fc]
        track = [fc for fc in fc_list if dynamic_fc_names.Track_L in fc][0]
        temp_list = [river, track] + topo_fcs
        temp_list01 = [river] + topo_fcs
        temp_list02 = [lake, river_coverage]
        for enlarge_fc in hydro_erase_poly_list:
            if dynamic_fc_names.Pond_A in enlarge_fc:
                erase_polygons_by_replace(enlarge_fc, temp_list, None, working_gdb)
            elif dynamic_fc_names.Lake_A in enlarge_fc:
                erase_polygons_by_replace(enlarge_fc, temp_list01, None, working_gdb)
            elif any(island in enlarge_fc for island in [dynamic_fc_names.Inland_Island_A, dynamic_fc_names.Coastal_Island_A, dynamic_fc_names.Offshore_Island_A]):
                erase_polygons_by_replace(enlarge_fc, temp_list02, None, working_gdb)
            else:
                erase_polygons_by_replace(enlarge_fc, topo_fcs, None, working_gdb)

        # # Fill gaps
        lake = [fc for fc in fc_list if dynamic_fc_names.Lake_A in fc][0]
        forest = dynamic_fc_names.Forest_A
        arcpy.topographic.FillGaps(lake, val_dict['Hydrography_hydro_erase_poly_max_gap_area'], "FILL_BY_LENGTH")

        # Remove shoreline not on hydro area feature boundary
        shore_line = [fc for fc in fc_list if dynamic_fc_names.Shoreline_L in fc][0]
        shore_line_lyr = arcpy.management.MakeFeatureLayer(shore_line, "shore_line")
        selected_shore_line_island = arcpy.management.SelectLayerByLocation(shore_line_lyr, 'CROSSED_BY_THE_OUTLINE_OF', island, None, 'ADD_TO_SELECTION', 'INVERT')
        selected_shore_line_pond = arcpy.management.SelectLayerByLocation(shore_line_lyr, 'CROSSED_BY_THE_OUTLINE_OF', pond, None, 'ADD_TO_SELECTION', 'INVERT')
        selected_shore_line_lake = arcpy.management.SelectLayerByLocation(shore_line_lyr, 'CROSSED_BY_THE_OUTLINE_OF', lake, None, 'ADD_TO_SELECTION', 'INVERT')
        # Calculate Field
        arcpy.management.CalculateField(in_table=selected_shore_line_island, field=val_dict['Resolve_conflict_build_visible_field'], expression=1, expression_type='PYTHON3')
        arcpy.management.CalculateField(in_table=selected_shore_line_pond, field=val_dict['Resolve_conflict_build_visible_field'], expression=1, expression_type='PYTHON3')
        arcpy.management.CalculateField(in_table=selected_shore_line_lake, field=val_dict['Resolve_conflict_build_visible_field'], expression=1, expression_type='PYTHON3')

        # Convert underground river
        under_ground_river = [fc for fc in fc_list if dynamic_fc_names.Under_Ground_River_L in fc][0]
        river = [fc for fc in fc_list if dynamic_fc_names.River_L in fc][0]
        connect = True
        convert_type(under_ground_river, None, val_dict['Hydrography_hydro_convert_ungr_river_min_length'], river, None, connect, working_gdb)
        # Increase hydro line length
        dam = [fc for fc in fc_list if dynamic_fc_names.Dam_L in fc][0]
        under_ground_river = [fc for fc in fc_list if dynamic_fc_names.Under_Ground_River_L in fc][0]
        under_ground_river_lyr = arcpy.management.MakeFeatureLayer(under_ground_river, "under_ground_river_lyr", val_dict['Hydrography_hydro_enlarge_poly_sql'])
        increase_line_length(under_ground_river_lyr, None, val_dict['Hydrography_increase_hydro_line_min_length'], working_gdb)
        increase_line_length(dam, None, val_dict['Hydrography_increase_hydro_line_min_length'], working_gdb)
        # Remove close hydro lines
        dangles1 = "true"
        dangles2 = "false"
        delete = "false"
        check_connect1="false"
        check_connect2="true"
        connect_angle1 = 0
        connect_angle2 = 5
        comp_lines = []
        
        # For river fc
        river = [fc for fc in fc_list if dynamic_fc_names.River_L in fc][0]
        arcpy.management.Integrate([river], val_dict['Hydrography_remove_close_tolerance'])
        h_river_l_intg_repare_geom = arcpy.management.RepairGeometry(in_features=river, delete_null=True, validation_method="ESRI")
        remove_close_lines(h_river_l_intg_repare_geom, val_dict['Hydrography_hydro_remove_small_sql'], val_dict['Hydrography_remove_close_dist'], 
                           val_dict['Hydrography_remove_close_parallel_per_min'], dangles1, delete, val_dict['Resolve_conflict_build_visible_field'], 
                           check_connect1, connect_angle1, 
                           comp_lines, working_gdb)
        # For irrigation canal fc
        irrigation = [fc for fc in fc_list if dynamic_fc_names.Irrigation_Canal_L in fc][0]
        arcpy.management.Integrate([irrigation], val_dict['Hydrography_remove_close_tolerance'])
        h_river_l_intg_repare_geom = arcpy.management.RepairGeometry(in_features=irrigation, delete_null=True, validation_method="ESRI")
        remove_close_lines(h_river_l_intg_repare_geom, val_dict['Hydrography_hydro_remove_small_sql'], val_dict['Hydrography_remove_close_dist'], 
                           val_dict['Hydrography_remove_close_parallel_per_max'], dangles2, delete, 
                           val_dict['Resolve_conflict_build_visible_field'], check_connect2, connect_angle2, 
                           comp_lines, working_gdb)
        
        # Hydro line dangles
        compare_fcs = [fc for a_lyr in [dynamic_fc_names.Irrigation_Canal_Coverage_A, dynamic_fc_names.Lake_A, dynamic_fc_names.Pond_A, dynamic_fc_names.River_Coverage_A] for fc in fc_list if str(a_lyr) in fc]
        hydro_lines_list = [fc for a_lyr in [dynamic_fc_names.Irrigation_Canal_L, dynamic_fc_names.River_L] for fc in fc_list if str(a_lyr) in fc]
        aoi = f"{in_feature_loc}\\AOI_L"
        compare_fcs.append(aoi)
        recursive = "true"
        for hydro_lines in hydro_lines_list:
            remove_dangles_lines(working_gdb, hydro_lines, val_dict['Hydrography_hydro_remove_small_sql'], val_dict['Hydrography_hydro_line_dangle_min_length'], compare_fcs, recursive)
        # Hydro small feature to point
        hydro_small_line_fc_list = list(filter(str.strip, hydro_small_line_fc_list))
        hydro_small_line_fc_list = [fc for a_lyr in hydro_small_line_fc_list for fc in fc_list if str(a_lyr) in fc]
        hydro_small_point_fc_list = list(filter(str.strip, hydro_small_point_fc_list))
        hydro_small_point_fc_list = [fc for a_lyr in hydro_small_point_fc_list for fc in fc_list if str(a_lyr) in fc]

        sql = None
        for line_fc, point_fc in zip(hydro_small_line_fc_list, hydro_small_point_fc_list):
            feature2point(working_gdb, line_fc, point_fc, val_dict['Hydrography_hydro_small_fc_min_length'], val_dict['Hydrography_hydro_delete_input'], val_dict['Hydrography_hydro_create_one_point'], val_dict['Hydrography_hydro_unique_field'], sql)
        

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Hydrograpy generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Hydrograpy generalisation', f'{exc_value}\n')
