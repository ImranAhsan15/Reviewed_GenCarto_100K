import arcpy
import math
import traceback

# def straight_lines(outlines, polygon, spat_ref):
#     try:
#         arcpy.env.overwriteOutput = True
#         poly_id_field = "FID_" + arcpy.da.Describe(polygon)['name']
#         new_split_lines = ""
#         new_lines = []
#         with arcpy.da.SearchCursor(outlines, ['shape@', poly_id_field]) as cursor:
#             for row in cursor:
#                 line = arcpy.Polyline(arcpy.Array([row[0].firstPoint, row[0].lastPoint]), spat_ref)
#                 if line.length > 0:
#                     new_lines.append(line)

#         print(str(len(new_lines)) + " new lines")
#         if len(new_lines) >= 0:
#             new_split_lines = arcpy.management.CopyFeatures(new_lines, "new_split_lines")
#             arcpy.management.RepairGeometry(new_split_lines)

#         return new_split_lines
    
#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Straight lines error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)

def straight_line(pnt_data, line_data, OFFSETDIST, working_gdb):
    RIGHTANGLE = 90.0
    try:
        arcpy.AddMessage("Determining where to split features")
        seg = arcpy.management.SplitLine(line_data, f"{working_gdb}\\SplitLineSegs")
        # Near tool
        arcpy.analysis.Near(pnt_data, seg, "0 Meters")

        sr = arcpy.da.Describe(line_data)['spatialReference']
        many_lines = []
        # Loop through each point feature
        with arcpy.da.SearchCursor(pnt_data, ["OID@", "SHAPE@", "NEAR_FID"]) as cur:
            for row in cur:
                # Get field values
                oid, shp = row[0], row[1]
                near_id = row[2]
                new_shp = shp.centroid
                x_coord = new_shp.X
                y_coord = new_shp.Y

                if near_id:
                    where = "OBJECTID = " + str(near_id)
                    segments = [s_row[0] for s_row in arcpy.da.SearchCursor(seg, ["SHAPE@", "OID@"], where)]
                    for polyline in segments:
                        if not polyline.disjoint(shp):

                            # Get angle of line segment in radians
                            delta_y = polyline.lastPoint.Y - polyline.firstPoint.Y
                            delta_x = polyline.lastPoint.X - polyline.firstPoint.X

                            angle = math.degrees(math.atan2(delta_y, delta_x))

                            if angle < 0:
                                angle = 360 + angle

                            if not angle:
                                angle = 0
                            # Calculate angle of offset
                            offset_angle = abs(RIGHTANGLE - abs(angle))
                            # Calculate x and y offset
                            x_offset = abs(math.cos(math.radians(offset_angle)) * OFFSETDIST)
                            y_offset = abs(math.sin(math.radians(offset_angle)) * OFFSETDIST)
                            # Get coordinates of offset point based on quadrant angle falls in
                            if angle >= 0 and angle < 90:
                                # 1st quad
                                new_x = x_coord - x_offset
                                new_y = y_coord + y_offset
                                new2_x = x_coord + x_offset
                                new2_y = y_coord - y_offset
                            elif angle >= 90 and angle < 180:
                                # 2nd quad
                                new_x = x_coord - x_offset
                                new_y = y_coord - y_offset
                                new2_x = x_coord + x_offset
                                new2_y = y_coord + y_offset
                            elif angle >= 180 and angle < 270:
                                # 3rd quad
                                new_x = x_coord + x_offset
                                new_y = y_coord - y_offset
                                new2_x = x_coord - x_offset
                                new2_y = y_coord + y_offset

                            else:
                                # 4th quad
                                new_x = x_coord + x_offset
                                new_y = y_coord + y_offset
                                new2_x = x_coord - x_offset
                                new2_y = y_coord - y_offset

                            array = arcpy.Array()

                            array.append(arcpy.Point(new_x, new_y))
                            array.append(arcpy.Point(x_coord, y_coord))
                            array.append(arcpy.Point(new2_x, new2_y))


                            test_line = arcpy.Polyline(array, sr)
                            many_lines.append(test_line)
                            break
        arcpy.AddMessage("Copying.....")
        if len(many_lines) >= 1:
            new_lines = arcpy.management.CopyFeatures(many_lines, f"{working_gdb}\\lines_for_split")
        else:
            arcpy.AddWarning("No lines were generated for splitting polygons")
            new_lines = None
        return new_lines
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Straight lines (Split hydro) error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def create_near_polys(polygons, spat_ref, working_gdb):
    try:
        touching_ids = []
        near_dict = {}
        arcpy.env.overwriteOutput = True
        if arcpy.Exists(f"{working_gdb}\\near_grids"):
            arcpy.management.Delete(f"{working_gdb}\\near_grids")

        near_dangles = arcpy.analysis.GenerateNearTable(polygons, polygons, f"{working_gdb}\\near_grids", "0 Meters", closest="ALL", closest_count="0", method="PLANAR")

        #get a list of the lines close to other lines...
        with arcpy.da.SearchCursor(near_dangles, ["IN_FID", "NEAR_FID", "NEAR_DIST"], sql_clause=(None, 'ORDER BY IN_FID')) as cursor:
            for row in cursor:
                #if this is the first record for that in_fid value
                if row[0] not in touching_ids:
                    if row[1] > (row[0] + 1) or row[1] < (row[0] - 1):
                        #add to the touching_ids list and near dictionary
                        touching_ids.append(row[0])
                        near_dict[row[0]] = [row[1]]
                # if this is not the first record
                else:
                    if row[1] > (row[0] + 1) or row[1] < (row[0] - 1):
                        #updated the dictionary to add the new near id
                        cur_list = near_dict[row[0]]
                        cur_list.append(row[1])
                        near_dict[row[0]] = cur_list


        poly_geos = []
        with arcpy.da.SearchCursor(polygons, ['OID@', 'SHAPE@']) as cursor:
            for row in cursor:
                if row[0] in touching_ids:
                    poly_geos.append(row[1])

        undissolved = arcpy.management.CopyFeatures(poly_geos, f"{working_gdb}\\undissolved_polys")
        if arcpy.Exists(f"{working_gdb}\\near_grids"):
            arcpy.management.Delete(f"{working_gdb}\\near_grids")

        return undissolved
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Create near polygons error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def split(polygon, centerline, width, working_gdb):
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    try:
        final_dissolv_polys = arcpy.topographic.IdentifyNarrowPolygons(in_features=polygon, out_feature_class=f"{working_gdb}\\final_dissolv_polys", min_width=f"{width} Meters",
            min_length="0.01 Meters", taper_length="0 Meters", connecting_features=None)
        query = arcpy.da.Describe(final_dissolv_polys)['areaFieldName'] + " < " + str(width * width * 2)
        arcpy.management.MakeFeatureLayer(final_dissolv_polys, "final_dissolv_polys")
        arcpy.management.SelectLayerByAttribute("final_dissolv_polys", "NEW_SELECTION", query)
        result = arcpy.management.Eliminate("final_dissolv_polys", f"{working_gdb}\\eliminate")
        # Split the center lines
        temp_splitcenter = arcpy.management.FeatureToLine([final_dissolv_polys, centerline], f"{working_gdb}\\temp_splitcenter")
        splitcenter = arcpy.analysis.Identity(centerline, temp_splitcenter, f"{working_gdb}\\splitcenter")

        if int(arcpy.management.GetCount(polygon)[0]) < int(arcpy.management.GetCount(final_dissolv_polys)[0]):
            is_split = True
        else:
            result = polygon
            is_split = False
        return result, splitcenter, is_split

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Split (Split hydro) error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)



# def split(polygon, center, width_val, working_gdb):
#     try:
#         """ Split the hydro polygons along the narrow branches """
#         arcpy.env.overwriteOutput = True
#         arcpy.env.workspace = working_gdb
#         is_split = True

#         width = str(width_val * 1) + " Meters"

#         orig_lines = f"{working_gdb}\\Polygon_Line"
#         split_lines = f"{working_gdb}\\Split_Line"
#         merge_lines = f"{working_gdb}\\Merged_Lines"
#         split_merge = f"{working_gdb}\\Merged_Split_Lines"
#         unsplit_lines = f"{working_gdb}\\Final_Unsplit"
#         merge_poly = f"{working_gdb}\\merge_fc"
#         split_polys = f"{working_gdb}\\Split_polygons"
#         split_polys2 = f"{working_gdb}\\Split_polygons2"
#         explode = f"{working_gdb}\\explode_fc"
#         single_points = f"{working_gdb}\\explode_fc_points"

#         arcpy.AddMessage("Splitting narrow polygons...")

#         # Convert the boundary of the polygon to a line
#         arcpy.AddMessage("  ...Determining boundary of polygon")
#         arcpy.management.PolygonToLine(polygon, orig_lines, "IDENTIFY_NEIGHBORS")
#         field = "Right_FID"

#         # Split the boundary line at each vertex
#         arcpy.AddMessage("  ...Splitting boundary at vertices")
#         arcpy.management.SplitLine(orig_lines, split_lines)
#         arcpy.management.AddField(split_lines, "MDR_Type", 'LONG', '#', '#', '#', '#', 'NULLABLE', 'NON_REQUIRED', '#')

#         """There are memory issues if there are too many features being split so if there are more that 500000 features, will write to disk
#         when less than this number, use in_memory because it's faster"""

#         if int(arcpy.management.GetCount(split_lines)[0]) <= 50000:
#             merge_lines = f"{working_gdb}\\Merged_Lines"
#             split_merge = f"{working_gdb}\\Merged_Split_Lines"
#             unsplit_lines = f"{working_gdb}\\Final_Unsplit"
#             merge_poly = f"{working_gdb}\\merge_fc"
#             single_points = f"{working_gdb}\\explode_fc_points"

#         # Merge Divided Roads - merges based on a provided distance
#         arcpy.AddMessage("  ... Merge divided lines...")
#         arcpy.cartography.MergeDividedRoads(split_lines, field, width, merge_lines, "")
#         # arcpy.AddMessage(arcpy.GetMessages())

#         # Selecting Features to make Polygons
#         arcpy.AddMessage("  ...Selecting Lines")
#         arcpy.management.SplitLine(merge_lines, split_merge)
#         arcpy.management.UnsplitLine(split_merge, unsplit_lines, field, "MDR_Type MIN")

#         merge_lyr2 = arcpy.management.MakeFeatureLayer(unsplit_lines, "merge_lyr2")

#         arcpy.management.SelectLayerByAttribute(merge_lyr2, "", "MIN_MDR_TYPE = 0")
#         arcpy.management.SelectLayerByLocation(merge_lyr2, "SHARE_A_LINE_SEGMENT_WITH", polygon, "", "REMOVE_FROM_SELECTION")
#         arcpy.management.DeleteFeatures(merge_lyr2)

#         # Create Polygons
#         arcpy.AddMessage("  ...Building Polygons")
#         arcpy.management.SelectLayerByAttribute(merge_lyr2, "", "MIN_MDR_TYPE = 0")

#         # Split the orgiginal lines
#         arcpy.analysis.Identity(center, merge_lyr2, split_polys)
#         arcpy.management.FeatureVerticesToPoints(split_polys, "split_verts", "BOTH_ENDS")
#         vert_lyrs = arcpy.management.MakeFeatureLayer("split_verts", "split_ver_lyr")
#         arcpy.management.SelectLayerByLocation(vert_lyrs, "INTERSECT", merge_lyr2)

#         new_lines = straight_line(vert_lyrs, center, width_val, working_gdb)

#         if new_lines:
#             arcpy.AddMessage("Creating Polys")
#             merge_poly = arcpy.management.FeatureToPolygon([new_lines, orig_lines], merge_poly, "", "ATTRIBUTES")
#             split_polys = arcpy.analysis.Identity(polygon, merge_poly, split_polys)

#             single_polys = arcpy.management.MultipartToSinglepart(split_polys, explode)
#             split_layer = arcpy.management.MakeFeatureLayer(single_polys, "split_layer")

#             # Eliminate parts that fall outside original polygons
#             arcpy.management.FeatureToPoint(split_layer, single_points, "INSIDE")
#             pt_lyr = arcpy.management.MakeFeatureLayer(single_points, "pt_layer")
#             arcpy.management.SelectLayerByLocation(pt_lyr, "WITHIN", polygon)
#             arcpy.management.SelectLayerByAttribute(pt_lyr, "SWITCH_SELECTION")

#             del_ids = [str(row[0]) for row in arcpy.da.SearchCursor(pt_lyr, ["ORIG_FID"])]
#             if len(del_ids) >= 1:
#                 query = "OBJECTID = "
#                 query += " OR OBJECTID = ".join(del_ids)

#                 arcpy.management.SelectLayerByAttribute(split_layer, "NEW_SELECTION", query)
#                 arcpy.management.DeleteFeatures(split_layer)
#                 arcpy.management.SelectLayerByAttribute(split_layer, "CLEAR_SELECTION")

#             # Eliminate the small slivers
#             arcpy.AddMessage("  ...Removing slivers")
#             query = arcpy.da.Describe(split_polys)['areaFieldName'] + " < " + str(width_val * width_val * 2)
#             arcpy.management.SelectLayerByAttribute(split_layer, "NEW_SELECTION", query)
#             result = arcpy.management.Eliminate(split_layer, f"{working_gdb}\\elmiminate")
#         else:
#             result = polygon
#             is_split = False

#         # Split the center lines
#         splitcenter = arcpy.management.FeatureToLine([merge_lyr2, center], f"{working_gdb}\\splitcenter")
#         arcpy.management.MakeFeatureLayer(splitcenter, "split_cent_lyr", "FID_Final_Unsplit = -1")

#         # Delete temp files
#         clean_list = [orig_lines, split_lines, merge_lines, split_merge,
#         unsplit_lines, merge_poly, split_polys, explode, "split_verts"]
#         # arcpy.management.Delete(clean_list)

#         return result, splitcenter, is_split

#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Split (Split hydro) error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)       

# def split(centerline, polygon, width, scale_val, working_gdb, in_field=None):
#     try:
#         is_split = True
#         width_val = width
#         spat_ref = arcpy.da.Describe(polygon)['spatialReference']

#         arcpy.env.overwriteOutput = True


#         #create line from polygon
#         arcpy.AddMessage("Getting boundary of Poly")
#         boundary = arcpy.management.PolygonToLine(polygon, "poly_bnd", neighbor_option="IGNORE_NEIGHBORS")

#         #create tiles
#         arcpy.AddMessage("Creating Boxes")
#         strips = arcpy.cartography.StripMapIndexFeatures(boundary, "boundary_boxes", use_page_unit="NO_USEPAGEUNIT", scale=scale_val, length_along_line=width, length_perpendicular_to_line=width, page_orientation="HORIZONTAL", overlap_percentage="0", starting_page_number="1", direction_type="WE_NS")

#         undissolve = create_near_polys(strips, spat_ref, working_gdb)
#         #match boxes to centerlines
#         arcpy.AddMessage("Determine centerlines")
#         arcpy.analysis.Near(undissolve, centerline, "0 Meters")

#         arcpy.AddMessage("Dissolve")
#         dissolve = arcpy.management.Dissolve(undissolve, "dissolved", dissolve_field="NEAR_FID", multi_part="SINGLE_PART")

#         d_layer = arcpy.management.MakeFeatureLayer(dissolve, "diss_layer")

#         masks = arcpy.cartography.FeatureOutlineMasks(d_layer, "masks", scale_val, spat_ref, margin="0 Points", method="EXACT", attributes="ALL")

#         arcpy.AddMessage("Determine areas to split")
#         outlines = arcpy.management.PolygonToLine(masks, "mask_outlines", neighbor_option="IGNORE_NEIGHBORS")

#         outlines_ident = arcpy.analysis.Identity(outlines, polygon, "mask_ident", "ONLY_FID")
#         outlines_sing = arcpy.management.MultipartToSinglepart(outlines_ident, "Interset_outlines_single")

#         fields = arcpy.ListFields(outlines_sing, "FID_*")
#         field_names = []
#         for field in fields:
#             field_names.append(field.name)
#         poly_id_field = "FID_" + arcpy.da.Describe(polygon)['name']
#         outline_lyr = arcpy.management.MakeFeatureLayer(outlines_sing, "outline_lyr", poly_id_field + " <> -1")
#         unsplit = arcpy.management.UnsplitLine(outline_lyr, "unsplitOutlines", field_names)

#         arcpy.AddMessage("Create lines for splitting")
#         new_lines = straight_lines(unsplit, polygon, spat_ref)

#         where = "FID_" + arcpy.da.Describe(new_lines)['name'] + " <> -1"

#         split_new_lines = arcpy.management.FeatureToLine([new_lines, polygon], "Split_new_lines")
#         split_layer = arcpy.management.MakeFeatureLayer(split_new_lines, "split_layer", where)
#         arcpy.management.SelectLayerByLocation(split_layer, "WITHIN", polygon)

#         final_lines = arcpy.management.CopyFeatures(split_layer, "Final_split_lines")

#         if int(arcpy.management.GetCount(final_lines)[0]) >= 1:

#             arcpy.AddMessage("Creating Polys")
#             merge_poly = arcpy.management.FeatureToPolygon([final_lines, polygon], "merge_poly", "", "ATTRIBUTES")
#             split_polys = arcpy.analysis.Identity(polygon, merge_poly, "split_polys")
#             single_polys = arcpy.management.MultipartToSinglepart(split_polys , "explode")
#             split_layer = arcpy.management.MakeFeatureLayer(single_polys, "split_layer", poly_id_field + " <> -1")

#             #Eliminate the small slivers
#             arcpy.AddMessage("  ...Removing slivers")

#             query = arcpy.da.Describe(split_polys)["areaFieldName"] + " < " + str(width_val * width_val)
#             arcpy.management.SelectLayerByAttribute(split_layer, "NEW_SELECTION", query)
#             elim = arcpy.management.Eliminate(split_layer, "elmiminate")

#             select_where = ""
#             if in_field:
#                 select_where = in_field + " = 1 OR " + in_field + " IS NULL"

#             inside_lines = arcpy.management.MakeFeatureLayer(centerline, "cent_select_lyr", select_where)
#             near_tab2 = arcpy.analysis.GenerateNearTable(elim, inside_lines, "near_tab2", "0 Meters", closest="ALL")
#             line_ids = {}
#             max_id = 0
#             with arcpy.da.SearchCursor(near_tab2, ['IN_FID', 'NEAR_FID']) as cur:
#                 for row in cur:
#                     if row[0] in line_ids:
#                         line_ids[row[0]] = 0
#                     else:
#                         line_ids[row[0]] = row[1]
#                     if row[1] > max_id:
#                         max_id = row[1]

#             contain_field = "contain_id"
#             arcpy.management.AddField(elim, contain_field, "LONG")
#             with arcpy.da.UpdateCursor(elim, ['oid@', contain_field]) as u_cur:
#                 for u_row in u_cur:
#                     if u_row[0] in line_ids:
#                         contain_id = line_ids[u_row[0]]
#                         if contain_id != 0:
#                             u_row[1] = contain_id
#                             u_cur.updateRow(u_row)
#                         else:
#                             max_id += 1
#                             u_row[1] = max_id
#                             u_cur.updateRow(u_row)
#             result = arcpy.management.Dissolve(elim, "final_dissolv_polys", [poly_id_field, contain_field], multi_part="SINGLE_PART")
#             arcpy.analysis.Near(result, inside_lines, "0 Meters")

#             #get just the polygon geometries
#             splitcenter = arcpy.management.FeatureToLine([centerline, result], "splitcenter")
#             center_where = "FID_" + arcpy.da.Describe(centerline)['name'] + " <> -1"

#             arcpy.management.MakeFeatureLayer(splitcenter, "split_cent_lyr", center_where)
#             center_final = arcpy.management.CopyFeatures("split_cent_lyr", "final_centerlines")

#         else:
#             result = polygon
#             is_split = False
#             center_final = centerline

#         return result, center_final, is_split
    
#     except Exception as e:
#         tb = traceback.format_exc()
#         error_message = f"Split error: {e}\nTraceback details:\n{tb}"
#         arcpy.AddMessage(error_message)

