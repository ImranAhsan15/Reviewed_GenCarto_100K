import arcpy
import traceback
import sys
from common_utils import *

def timestamp():
    """Return current timestamp as string."""
    return datetime.now()

def has_features(fc):
    with arcpy.da.SearchCursor(fc, ["OID@"]) as cursor:
        return next(cursor, None) is not None  # True if at least one feature

def split_at_intersection(input_line, split_lines, working_gdb):
    arcpy.env.overwriteOutput = True
    try:
        spat_ref = arcpy.da.Describe(input_line)['spatialReference']

        split_points = []
        for split_line in split_lines:
            # Determine intersections
            arcpy.AddMessage("Determining intersections with " + str(split_line))
            near = arcpy.analysis.GenerateNearTable(input_line, split_line, f"{working_gdb}\\near", "0 Meters", closest="ALL")

            arcpy.AddMessage(str(int(arcpy.management.GetCount(near)[0])))
            near_dict = {} 
            split_oids = []
            in_oids = []
            if has_features(near):
                with arcpy.da.SearchCursor(near, ["IN_FID", "NEAR_FID"]) as cursor:
                    for row in cursor:
                        # If this is the first record for that in_fid value
                        if row[0] not in in_oids:
                            #add to the touching_ids list and near dictionary
                            in_oids.append(row[0])
                            near_dict[row[0]] = [row[1]]
                            split_oids.append(row[1])
                        else:
                            # Updated the dictionary to add the new near id
                            cur_list = near_dict[row[0]]
                            cur_list.append(row[1])
                            near_dict[row[0]] = cur_list
                            split_oids.append(row[1])

            split_oids = set(split_oids)
            if len(in_oids) >= 1:
                arcpy.AddMessage("Getting intersection points")
                split_geos = {}
                # Get the geometries for the split features
                if has_features(split_line):
                    with arcpy.da.SearchCursor(split_line, ['OID@', 'SHAPE@']) as cursor:
                        for row in cursor:
                            if row[0] in split_oids:
                                split_geos[row[0]] = row[1]
                if has_features(input_line):
                    with arcpy.da.SearchCursor(input_line, ['OID@', 'SHAPE@']) as cursor:
                        for row in cursor:
                            if row[0] in in_oids:
                                near_oid = near_dict[row[0]]
                                for oid in near_oid:
                                    near_geo = split_geos[oid]
                                    pts = row[1].intersect(near_geo, 1)
                                    for pt in pts:
                                        split_points.append(arcpy.PointGeometry(pt, spat_ref))

        if len(split_points) >= 1:
            arcpy.AddMessage("Splitting at intersections")
            pt_fc = arcpy.management.CopyFeatures(split_points, "intersect_pts_split")
            split_line_result = arcpy.management.SplitLineAtPoint(input_line, pt_fc, "split_at_intersect", "1 Meters")
            arcpy.management.RepairGeometry(split_line_result)
            arcpy.management.DeleteFeatures(input_line)
            arcpy.management.Append(split_line_result, input_line, "NO_TEST")
            split_lines.append(input_line)
            arcpy.management.Integrate(split_lines)

        if arcpy.Exists(f"{working_gdb}\\near"):
            arcpy.management.Delete(f"{working_gdb}\\near")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Split at intersection error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def hide_near_lines_by_count(hide_line, vis_field, distance, test_value, comp_lines, working_gdb):
    # Set environment
    arcpy.env.overwriteOutput = True
    try:
        max_val = 0
        rank_dict = {}
        higher_list = []
        hide_cnt = 0
        near_dict = {}

        close_lines = f"{working_gdb}\\close_lines"

        near = arcpy.analysis.GenerateNearTable(hide_line, comp_lines, close_lines, distance, closest="ALL")

        with arcpy.da.SearchCursor(near, ("IN_FID", "NEAR_RANK")) as cursor:
            for row in cursor:
                id_val = row[0]
                rank = row[1]
                if id_val not in near_dict:
                    near_dict[id_val] = rank
                else:
                    cur_rank = near_dict[id_val]
                    if rank > cur_rank:
                        near_dict[id_val] = rank

        for key, value in near_dict.items():
            if value > max_val:
                max_val = value

            if value in rank_dict:
                cur_cnt = rank_dict[value]
                rank_dict[value] = cur_cnt + 1
            else:
                rank_dict[value] = 1

            if value > test_value:
                higher_list.append(key)
                hide_cnt += 1

        arcpy.AddMessage("Maximum near line count is " + str(max_val))
        arcpy.AddMessage("Features to hide count is " + str(hide_cnt))
        arcpy.AddMessage(str(rank_dict))

        with arcpy.da.UpdateCursor(hide_line, ('OID@', vis_field)) as cursor:
            for row in cursor:
                if row[0] in higher_list:
                    row[1] = 1
                    cursor.updateRow(row)

        if arcpy.Exists(close_lines):
            arcpy.management.Delete(close_lines)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Hide near lines by count error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def add_layers(fc_list, map_name):
    try:
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]
        for fc in fc_list:
            if has_features(fc):
                maps.addDataFromPath(fc)
        aprx.save()
        del aprx
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Add layers error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def apply_attribution(fc_list, attribution_fc_list, express_list, query_list, field_list):
    try:
        # Get feature list
        attribution_fc_list = list(filter(str.strip, attribution_fc_list))
        attribution_fc_list = [fc for attr_lyr in attribution_fc_list for fc in fc_list if str(attr_lyr) in fc]
        # Remove unneccesary character
        query_list = list(filter(str.strip, query_list))
        field_list = list(filter(str.strip, field_list))
        for attr_fc, express, query, field in zip(attribution_fc_list, express_list, query_list, field_list):
            fc_name = arcpy.da.Describe(attr_fc)['name']
            if(query):
                fcfields = [f.name for f in arcpy.ListFields(attr_fc)]
                if ' ' in field.strip():
                    multiple_fields = field.strip().split(' ')
                    if len(multiple_fields) > 1:
                        for fld in multiple_fields:
                            if fld not in fcfields:
                                arcpy.management.AddField(
                                    in_table=attr_fc,
                                    field_name=fld,
                                    field_type='SHORT'
                                )
                else:
                    if field not in fcfields:
                        arcpy.management.AddField(
                            in_table=attr_fc,
                            field_name=field,
                            field_type='SHORT'
                        )       
            feature_layer = arcpy.management.MakeFeatureLayer(attr_fc, f"{fc_name}_layer")
            selected_road_fc = arcpy.management.SelectLayerByAttribute(feature_layer, "NEW_SELECTION", query)
            if ' ' in field.strip():
                multiple_fields = field.strip().split(' ')
                if len(multiple_fields) > 1:
                    for fld in multiple_fields:
                        arcpy.management.CalculateField(in_table=selected_road_fc, field=fld, expression=express, expression_type='PYTHON3')

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Apply attribution error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def embankment_cutting(fc_list, intersecting_fc_list, working_gdb):
    try:
        intersecting_fc_list = list(filter(str.strip, intersecting_fc_list))
        intersecting_fc_list = [fc for intersect_fc in intersecting_fc_list for fc in fc_list if str(intersect_fc) in fc]
        cutting = [fc for fc in fc_list if resolve_lyr().Cutting_L in fc][0]
        embankment = [fc for fc in fc_list if resolve_lyr().Embankment_L in fc][0]
        split_at_intersection(cutting, intersecting_fc_list, working_gdb)
        split_at_intersection(embankment, intersecting_fc_list, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Embankment Cutting error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def prep_4_line_resolve(fc_list, query, visible_field, distance, mx_no_close_fcs_l, mx_no_close_fcs_m, mx_no_close_fcs_u, prep_line_resolve_fcs_list, working_gdb):
    try:
        prep_line_resolve_fcs_list = list(filter(str.strip, prep_line_resolve_fcs_list))
        prep_line_resolve_fcs_list = [fc for prep_line in prep_line_resolve_fcs_list for fc in fc_list if str(prep_line) in fc]
        # Required feature class
        generalised_bldg = [fc for fc in fc_list if resolve_lyr().Generalised_Buildings_A in fc][0]
        town_built_up = [fc for fc in fc_list if resolve_lyr().Town_Built_up_A in fc][0]
        cutting = [fc for fc in fc_list if resolve_lyr().Cutting_L in fc][0]
        feature_layer_cutting = arcpy.management.MakeFeatureLayer(cutting, "cutting_layer")
        embankment = [fc for fc in fc_list if resolve_lyr().Embankment_L in fc][0]

        feature_layer_embankment = arcpy.management.MakeFeatureLayer(embankment, "embankment_layer")
        sel_generalised_bldg_em = arcpy.management.SelectLayerByLocation(feature_layer_embankment, "WITHIN", generalised_bldg, "", "NEW_SELECTION")
        sel_town_built_em = arcpy.management.SelectLayerByLocation(feature_layer_embankment, "WITHIN", town_built_up, "", "NEW_SELECTION")
        sel_generalised_bldg_cut = arcpy.management.SelectLayerByLocation(feature_layer_cutting, "WITHIN", generalised_bldg, "", "NEW_SELECTION")
        sel_town_built_cut = arcpy.management.SelectLayerByLocation(feature_layer_cutting, "WITHIN", town_built_up, "", "NEW_SELECTION")
        # Delete features
        arcpy.management.DeleteFeatures(sel_generalised_bldg_em)
        arcpy.management.DeleteFeatures(sel_town_built_em)
        arcpy.management.DeleteFeatures(sel_generalised_bldg_cut)
        arcpy.management.DeleteFeatures(sel_town_built_cut)

        compare_layers = []
        for in_features in prep_line_resolve_fcs_list:
            if has_features(in_features):
                fc_name = arcpy.da.Describe(in_features)['name']
                if resolve_lyr().Road_L in in_features:
                    desc = arcpy.da.Describe(in_features)
                    oid_fld_name = desc["OIDFieldName"]
                    feature_layer = arcpy.management.MakeFeatureLayer(in_features, f"feature_layer_{fc_name}", query)
                    compare_layers.append(feature_layer)
                    # Unsplit line
                    road_unsplit = arcpy.management.UnsplitLine(feature_layer, f"memory\\road_unsplit", ['RCS', 'NAM'], [['OBJECTID', 'FIRST']])
                    # Join field
                    fields_list_selected_fcs = [field.name for field in desc['fields'] if not field.required and "SDE_" not in field.name]
                    joined_layer = arcpy.management.JoinField(road_unsplit, 'FIRST_OBJECTID', in_features, oid_fld_name, fields_list_selected_fcs)
                    # Delete features
                    arcpy.management.DeleteFeatures(feature_layer)
                    append_layer = arcpy.management.Append(joined_layer, feature_layer, 'NO_TEST')
                    arcpy.management.RepairGeometry(append_layer)

                elif resolve_lyr().Track_L in in_features:
                    desc = arcpy.da.Describe(in_features)
                    oid_fld_name = desc["OIDFieldName"]
                    feature_layer = arcpy.management.MakeFeatureLayer(in_features, f"feature_layer_{fc_name}", query)
                    compare_layers.append(feature_layer)
                    # Unsplit line
                    track_unsplit = arcpy.management.UnsplitLine(feature_layer, f"memory\\track_unsplit", ['NAM'], [['OBJECTID', 'FIRST']])
                    # Join field
                    fields_list_selected_fcs = [field.name for field in (arcpy.da.Describe(in_features))['fields'] if not field.required and "SDE_" not in field.name]
                    joined_layer = arcpy.management.JoinField(track_unsplit, 'FIRST_OBJECTID', in_features, oid_fld_name, fields_list_selected_fcs)
                    # Delete features
                    arcpy.management.DeleteFeatures(feature_layer)
                    append_layer = arcpy.management.Append(joined_layer, feature_layer, 'NO_TEST')
                    arcpy.management.RepairGeometry(append_layer)

                elif resolve_lyr().Irrigation_Canal_L in in_features:
                    desc = arcpy.da.Describe(in_features)
                    oid_fld_name = desc["OIDFieldName"]
                    feature_layer = arcpy.management.MakeFeatureLayer(in_features, f"feature_layer_{fc_name}", query)
                    compare_layers.append(feature_layer)
                    # Unsplit line
                    irrigation_unsplit = arcpy.management.UnsplitLine(feature_layer, f"memory\\irrigation_unsplit", ['NAM'], [['OBJECTID', 'FIRST']])
                    # Join field
                    fields_list_selected_fcs = [field.name for field in (arcpy.da.Describe(in_features))['fields'] if not field.required and "SDE_" not in field.name]
                    joined_layer = arcpy.management.JoinField(irrigation_unsplit, 'FIRST_OBJECTID', in_features, oid_fld_name, fields_list_selected_fcs)
                    # Delete features
                    arcpy.management.DeleteFeatures(feature_layer)
                    append_layer = arcpy.management.Append(joined_layer, feature_layer, 'NO_TEST')
                    arcpy.management.RepairGeometry(append_layer)
                else:
                    try: 
                        feature_layer = arcpy.management.MakeFeatureLayer(in_features, f"feature_layer_{fc_name}", query)
                        compare_layers.append(feature_layer)
                    except Exception as e:
                        tb = traceback.format_exc()
                        error_message = f"Make feature layer error for {fc_name}: {e}\nTraceback details:\n{tb}"
                        arcpy.AddError(error_message)
                        return False
        # Hide close feature by count
        if f"feature_layer_{resolve_lyr().Embankment_L}" in compare_layers:
            ind = compare_layers.index(f"feature_layer_{resolve_lyr().Embankment_L}")
            compare_layers.pop(ind)
            hide_near_lines_by_count(f"feature_layer_{resolve_lyr().Embankment_L}", visible_field, distance, mx_no_close_fcs_m, compare_layers, working_gdb)
        if f"feature_layer_{resolve_lyr().Cutting_L}" in compare_layers:
            ind = compare_layers.index(f"feature_layer_{resolve_lyr().Cutting_L}")
            compare_layers.pop(ind)
            hide_near_lines_by_count(f"feature_layer_{resolve_lyr().Cutting_L}", visible_field, distance, mx_no_close_fcs_m, compare_layers, working_gdb)
        if f"feature_layer_{resolve_lyr().Irrigation_Canal_L}" in compare_layers:
            ind = compare_layers.index(f"feature_layer_{resolve_lyr().Irrigation_Canal_L}")
            compare_layers.pop(ind)
            hide_near_lines_by_count(f"feature_layer_{resolve_lyr().Irrigation_Canal_L}", visible_field, distance, mx_no_close_fcs_u, compare_layers, working_gdb)
        if f"feature_layer_{resolve_lyr().Fence_L}" in compare_layers:
            ind = compare_layers.index(f"feature_layer_{resolve_lyr().Fence_L}")
            compare_layers.pop(ind)
            hide_near_lines_by_count(f"feature_layer_{resolve_lyr().Fence_L}", visible_field, distance, mx_no_close_fcs_l, compare_layers, working_gdb)
        if f"feature_layer_{resolve_lyr().Wall_L}" in compare_layers:
            ind = compare_layers.index(f"feature_layer_{resolve_lyr().Wall_L}")
            compare_layers.pop(ind)
            hide_near_lines_by_count(f"feature_layer_{resolve_lyr().Wall_L}", visible_field, distance, mx_no_close_fcs_l, compare_layers, working_gdb)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Preparation for line resolving error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def split_explode_lines(fc_list, working_gdb):
    arcpy.AddMessage('Splitting and exploding lines ....')
    try:
        for fc in fc_list:
            fc_name = os.path.basename(fc)
            if fc_name.endswith("_L") and has_features(fc):
                # Process
                feature2line = arcpy.management.FeatureToLine(fc, f"{working_gdb}\\{fc_name}_feature2line")
                single_part = arcpy.management.MultipartToSinglepart(feature2line, f"{working_gdb}\\{fc_name}_single")

                # Replace features safely
                arcpy.management.TruncateTable(fc)
                arcpy.management.Append(single_part, fc, 'NO_TEST')

        # Cleanup temp layers
        for lyr in [f"{working_gdb}\\{fc_name}_feature2line", f"{working_gdb}\\{fc_name}_single"]:
                if arcpy.Exists(lyr):
                    arcpy.management.Delete(lyr)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Split explode line error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def create_carto_partition(fc_list, feature_loc, feature_count):
    try:
        # The partition FC created by theme 1 is in fc_list; it is deleted
        # below, so keep it out of the input list (ERROR 000732 otherwise).
        # fc_list can hold Layer objects (from the map) or path strings.
        def _fc_name(fc):
            return fc.name if hasattr(fc, "name") else os.path.basename(str(fc))
        fc_list = [fc for fc in fc_list if _fc_name(fc) != 'CartoPartitionA']
        if arcpy.Exists(f"{feature_loc}\\CartoPartitionA"):
            arcpy.management.Delete(f"{feature_loc}\\CartoPartitionA")
        arcpy.cartography.CreateCartographicPartitions(fc_list, f"{feature_loc}\\CartoPartitionA", feature_count, 'Features')
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Create carto partition error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        # Re-raise: without the partition FC the conflict-resolution themes
        # would run unpartitioned (memory/crash risk), so stop here instead.
        raise

def calc_vst_on_workspace(fc_list, symbology_file_path, apply_symbology_layers_list, map_name, feature_loc):
    try:
        # Define environment variables
        arcpy.env.overwriteOutput = 1
        # add_layers(fc_list, map_name)
        # Get feature list
        apply_symbology_fc_name = list(filter(str.strip, apply_symbology_layers_list))
        apply_symbology_fc_list = [fc for sym_app_lyr in apply_symbology_fc_name for fc in fc_list if str(sym_app_lyr) in fc]
        apply_symbology_fc_list.append(f"{feature_loc}\\AOI")
        # Calculate VST
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name)[0]
        lyrx = [os.path.basename(k)[:-5] for k in glob.glob(os.path.join(symbology_file_path, "*.lyrx"))]

        for fc in apply_symbology_fc_list:
            fc_name = os.path.basename(fc)
            if fc_name in lyrx:
                layer = maps.addDataFromPath(fc)
                # Run Calc Visual Specification
                symbology_layerx = maps.addDataFromPath(f"{symbology_file_path}\\{fc_name}.lyrx")
                in_symbology = symbology_layerx.symbology
                layer.symbology = in_symbology
                maps.removeLayer(symbology_layerx)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Apply visual specification error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def generate_near_and_calculate_orientation(
    in_features,
    near_features,
    near_table_name,
    val_dict,
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
    Ensures Orientation_Degree fields exist in `in_features`,
    generates a near table with `near_features`, joins the table,
    calculates Orientation_Degree, then removes the join.
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
        (val_dict['Applycarto_Required_Field'], val_dict['Applycarto_Field_Type'])
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

    arcpy.management.JoinField(in_features, "OBJECTID", near_table, "IN_FID")
    # --- Step 4: Calculate Fields ---
    logger.info("Calculating Orientation_Degree...")
    
    arcpy.management.CalculateField(
        in_table=in_features,
        field="Orientation_Degree",
        expression=f"!NEAR_ANGLE! + 90",
        expression_type="PYTHON3"
    )
    logger.info(f"Deleting Extra fields from {in_features}")
    arcpy.management.DeleteField(in_features, in_feature_field_names, method="KEEP_FIELDS")

def anchor_relative_to_map_units(
    x_percent: float,
    y_percent: float,
    marker_width_pt: float,
    marker_height_pt: float,
    scale_denom: float,
    dataset_unit: str = "Meter",
):
    INCH_TO_M = 0.0254
    M_TO_FT = 3.280839895
    # 1) percent of marker size -> points
    offx_pt = (x_percent / 100.0) * marker_width_pt
    offy_pt = (y_percent / 100.0) * marker_height_pt

    # 2) points -> inches on paper
    offx_in = offx_pt / 72.0
    offy_in = offy_pt / 72.0

    # 3) inches on paper -> inches on ground (scale)
    offx_in_ground = offx_in * scale_denom
    offy_in_ground = offy_in * scale_denom

    # 4) inches -> meters
    offx_m = offx_in_ground * INCH_TO_M
    offy_m = offy_in_ground * INCH_TO_M

    # 5) meters -> dataset units if needed
    du = (dataset_unit or "Meter").lower()
    if "foot" in du:
        return offx_m * M_TO_FT, offy_m * M_TO_FT
    return offx_m, offy_m

def duplicate_all_points_and_shift(in_fc, out_fc, dx, dy):
    arcpy.env.overwriteOutput = True

    # Convert GP Result -> path string if needed
    in_path = in_fc.getOutput(0) if hasattr(in_fc, "getOutput") else str(in_fc)

    # Copy original points to out_fc
    arcpy.management.CopyFeatures(in_path, out_fc)

    desc = arcpy.Describe(out_fc)
    sr = desc.spatialReference

    # Read all original geometries, then insert shifted duplicates
    geoms = []
    with arcpy.da.SearchCursor(out_fc, ["SHAPE@"]) as sc:
        for (g,) in sc:
            if g:
                geoms.append(g)

    with arcpy.da.InsertCursor(out_fc, ["SHAPE@"]) as ic:
        for g in geoms:
            p = g.centroid
            moved = arcpy.PointGeometry(arcpy.Point(p.X + dx, p.Y + dy), sr)
            ic.insertRow([moved])

    return out_fc

# def vegetation_symbol_create(fc_list, map_name_symbology, layer_details, map_scale, map_unit, logger):

#     arcpy.env.overwriteOutput = True
#     arcpy.env.workspace = arcpy.env.scratchGDB
#     scratch = arcpy.env.scratchGDB
#     arcpy.env.referenceScale = 50000

#     try: 

#         poly_data_location = [fc for fc in fc_list if layer_details["name"] in fc][0]

#         poly_data_path = os.path.dirname(poly_data_location)
        
#         poly_layer_name = os.path.basename(poly_data_location)
#         poly_layer_name_part = poly_layer_name[:-1]
#         point_name = f"{poly_layer_name_part}P"

#         road_data = [fc for fc in fc_list if resolve_lyr().Road_Surface_Physical_A in fc][0]

#         track_data = [fc for fc in fc_list if resolve_lyr().Track_Surface_Physical_A in fc][0]

#         point_data = next((fc for fc in fc_list if os.path.basename(fc) == point_name), None)

#         arcpy.AddMessage(f"point_data checking : {point_data}")

#         # If not found, create it
#         if not point_data:
#             out_gdb = os.path.dirname(poly_data_location)  # same container as VA1060_Oil_Palm_A
#             out_name = f"{poly_layer_name_part}P"
#             point_data = os.path.join(out_gdb, out_name)

#             if not arcpy.Exists(point_data):
#                 # Use spatial reference (and Z/M settings) from the polygon FC to keep consistency
#                 d = arcpy.da.Describe(poly_data_location)
#                 sr = d["spatialReference"]
#                 has_z = "ENABLED" if d.get("hasZ") else "DISABLED"
#                 has_m = "ENABLED" if d.get("hasM") else "DISABLED"

#                 created_feature = arcpy.management.CreateFeatureclass(
#                     out_path=out_gdb,
#                     out_name=out_name,
#                     geometry_type="POINT",
#                     spatial_reference=sr,
#                     has_z=has_z,
#                     has_m=has_m,
#                 )

#                 arcpy.AddMessage(f"feature created added for {out_name}")

#                 arcpy.management.AddField(created_feature, "MARKER_ID", "LONG", field_is_required= "NON_REQUIRED")
#                 arcpy.AddMessage(f"field added for {out_name}")
#         else:
#             arcpy.AddMessage(f"{point_name} already exists")

#         merged_data = arcpy.management.Merge([road_data, track_data], f"{scratch}//merged_features")
        
#         dissolved_merged_data = arcpy.analysis.PairwiseDissolve(merged_data, f"{scratch}//dissolved_merged_features")

#         arcpy.AddMessage(f"poly_data_location: {poly_data_location}")

#         poly_data_path = os.path.dirname(poly_data_location)
#         arcpy.AddMessage(f"poly_data_path: {poly_data_path}")

#         aprx = arcpy.mp.ArcGISProject('CURRENT')
#         maps = aprx.listMaps(map_name_symbology)[0]

#         layer_list = maps.listLayers()
#         poly_layer = [layer for layer in layer_list if layer.name == layer_details["name"]][0]
#         arcpy.AddMessage(f"poly_layer: {poly_layer}")
#         road_layer = [layer for layer in layer_list if layer.name == resolve_lyr().Road_Surface_Physical_A][0]
#         arcpy.AddMessage(f"road_layer: {road_layer}")
#         track_layer = [layer for layer in layer_list if layer.name == resolve_lyr().Track_Surface_Physical_A][0]
#         arcpy.AddMessage(f"track_layer: {track_layer}")
#         point_layer = [layer for layer in layer_list if layer.name == point_name][0]
#         arcpy.AddMessage(f"point_layer: {point_layer}")

#         arcpy.AddMessage(f"started ConvertMarkerPlacementToPoints")

#         converted_point = arcpy.cartography.ConvertMarkerPlacementToPoints(
#             in_layer=poly_layer,
#             out_feature_class=f"{scratch}\\{poly_layer_name}_marker_point",
#             create_multipoints="CREATE_POINTS",
#             boundary_option="MAY_CROSS_BOUNDARY",
#             boundary_distance=0,
#             boundary_distance_field=None,
#             boundary_distance_unit="Points",
#             in_barriers=None,
#             keep_at_least_one_marker="KEEP_AT_LEAST_ONE_MARKER",
#             displacement_method="DO_NOT_DISPLACE",
#             minimum_marker_distance="0 Points"
#         )

#         arcpy.AddMessage(f"converted_point: {converted_point}")
        
#         arcpy.AddMessage(f"ended ConvertMarkerPlacementToPoints")

#         dx, dy = anchor_relative_to_map_units(layer_details["x_anchor"], layer_details["y_anchor"], layer_details["marker_width"], layer_details["marker_height"], map_scale, map_unit)
        
#         moved_point = duplicate_all_points_and_shift(
#             in_fc=converted_point,
#             out_fc=f"{scratch}\\{poly_layer_name}_moved_P",
#             dx=-dx, dy=-dy
#         )
        
#         arcpy.AddMessage(f"out_fc: {moved_point}")
#         arcpy.AddMessage(f"duplicate_point_and_shift done")


#         selected_moved_point = arcpy.management.SelectLayerByLocation(moved_point, 
#                                                                     "INTERSECT",
#                                                                     dissolved_merged_data,
#                                                                     )
        
#         arcpy.management.DeleteFeatures(selected_moved_point)

#         arcpy.AddMessage(f"deleted overlapping points")

#         clipped_point = arcpy.analysis.PairwiseClip(selected_moved_point, poly_data_location, f"{scratch}\\{poly_layer_name_part}P")

#         point_data = [fc for fc in fc_list if point_name in fc][0]

#         field_list = arcpy.ListFields(point_data)
#         if "MARKER_ID" not in field_list:
#             arcpy.management.AddField(point_data, "MARKER_ID", "LONG", field_is_required= "NON_REQUIRED")
#             arcpy.AddMessage(f"field added for exisitng {point_name}")


#         if int(arcpy.management.GetCount(point_data)[0]) >= 0:
#             arcpy.management.Append(clipped_point, point_data, "NO_TEST")
            
            
#             arcpy.AddMessage(f"appended data")

#         # arcpy.AddMessage(f"feature added to GDB")

#         point_layer = [layer for layer in layer_list if layer.name == point_name][0]
#         arcpy.AddMessage(f"point_layer after marker point: {point_layer}")


#         arcpy.AddMessage("Ended vegetation_symbol_create")

#     except Exception as e:
#         exc_type, exc_value, exc_traceback = sys.exc_info()
#         tb = traceback.format_exc()
#         error_message = f'vegetation_symbol_create error: {e}\nTraceback details:\n{tb}'
#         logger.error(error_message)

def vegetation_symbol_create(fc_list, map_name_symbology,  layer_details, map_scale, road_track_buffer_distance, map_unit, logger):

    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = arcpy.env.scratchGDB
    scratch = arcpy.env.scratchGDB
    arcpy.env.referenceScale = map_scale



    arcpy.AddMessage(f"Started vegetation_symbol_create for {layer_details['name']}")

    try: 
        layer_names = resolve_lyr()

        poly_data_location = [fc for fc in fc_list if layer_details["name"] in fc][0]

        poly_data_path = os.path.dirname(poly_data_location)
        
        poly_layer_name = os.path.basename(poly_data_location)
        poly_layer_name_part = poly_layer_name[:-1]
        point_name = f"{poly_layer_name_part}P"

        road_line = [fc for fc in fc_list if layer_names.Road_L in fc][0]
        track_line = [fc for fc in fc_list if layer_names.Track_L in fc][0]

        road_data = arcpy.analysis.PairwiseBuffer(road_line, "road_line_buffer", f"{road_track_buffer_distance} Meters")
        track_data = arcpy.analysis.PairwiseBuffer(track_line, "track_line_buffer", f"{road_track_buffer_distance} Meters")

        point_data = next((fc for fc in fc_list if os.path.basename(fc) == point_name), None)

        arcpy.AddMessage(f"point_data checking : {point_data}")

        # If not found, create it
        if not point_data:
            out_gdb = os.path.dirname(poly_data_location)  # same container as VA1060_Oil_Palm_A
            out_name = f"{poly_layer_name_part}P"
            point_data = os.path.join(out_gdb, out_name)

            if not arcpy.Exists(point_data):
                # Use spatial reference (and Z/M settings) from the polygon FC to keep consistency
                d = arcpy.da.Describe(poly_data_location)
                sr = d["spatialReference"]
                has_z = "ENABLED" if d.get("hasZ") else "DISABLED"
                has_m = "ENABLED" if d.get("hasM") else "DISABLED"

                created_feature = arcpy.management.CreateFeatureclass(
                    out_path=out_gdb,
                    out_name=out_name,
                    geometry_type="POINT",
                    spatial_reference=sr,
                    has_z=has_z,
                    has_m=has_m,
                )

                arcpy.AddMessage(f"feature created added for {out_name}")

                arcpy.management.AddField(created_feature, "MARKER_ID", "LONG", field_is_required= "NON_REQUIRED")
                arcpy.AddMessage(f"field added for {out_name}")

                fc_list.append(point_data)

        else:
            arcpy.AddMessage(f"{point_name} already exists")

        merged_data = arcpy.management.Merge([road_data, track_data], f"{scratch}//merged_features")
        
        dissolved_merged_data = arcpy.analysis.PairwiseDissolve(merged_data, f"{scratch}//dissolved_merged_features")

        arcpy.AddMessage(f"poly_data_location: {poly_data_location}")

        poly_data_path = os.path.dirname(poly_data_location)
        arcpy.AddMessage(f"poly_data_path: {poly_data_path}")

        aprx = arcpy.mp.ArcGISProject('CURRENT')
        maps = aprx.listMaps(map_name_symbology)[0]

        layer_list = maps.listLayers()
        layer_list = [lyr for lyr in layer_list if not lyr.isGroupLayer]
        poly_layer = [layer for layer in layer_list if layer.name == layer_details["name"]][0]
        arcpy.AddMessage(f"poly_layer: {poly_layer}")
        # point_layer is diagnostic only (never used by a tool); the point FC
        # may not be a map layer (e.g. it was just created in the GDB), so look
        # it up safely instead of indexing an empty list.
        point_layer = next((layer for layer in layer_list if layer.name == point_name), None)
        arcpy.AddMessage(f"point_layer: {point_layer}")

        arcpy.AddMessage(f"started ConvertMarkerPlacementToPoints")

        converted_point = arcpy.cartography.ConvertMarkerPlacementToPoints(
            in_layer=poly_layer,
            out_feature_class=f"{scratch}\\{poly_layer_name}_marker_point",
            create_multipoints="CREATE_POINTS",
            boundary_option="MAY_CROSS_BOUNDARY",
            boundary_distance=0,
            boundary_distance_field=None,
            boundary_distance_unit="Points",
            in_barriers=None,
            keep_at_least_one_marker="KEEP_AT_LEAST_ONE_MARKER",
            displacement_method="DO_NOT_DISPLACE",
            minimum_marker_distance="0 Points"
        )

        arcpy.AddMessage(f"converted_point: {converted_point}")
        
        arcpy.AddMessage(f"ended ConvertMarkerPlacementToPoints")

        dx, dy = anchor_relative_to_map_units(layer_details["x_anchor"], layer_details["y_anchor"], layer_details["marker_width"], layer_details["marker_height"], map_scale, map_unit)
        
        moved_point = duplicate_all_points_and_shift(
            in_fc=converted_point,
            out_fc=f"{scratch}\\{poly_layer_name}_moved_P",
            dx=-dx, dy=-dy
        )
        
        arcpy.AddMessage(f"out_fc: {moved_point}")
        arcpy.AddMessage(f"duplicate_point_and_shift done")


        selected_moved_point = arcpy.management.SelectLayerByLocation(moved_point, 
                                                                    "INTERSECT",
                                                                    dissolved_merged_data,
                                                                    )
        
        arcpy.management.DeleteFeatures(selected_moved_point)

        arcpy.AddMessage(f"deleted overlapping points")
        field = "MARKER_ID"
        expression = 1 
        # selected_moved_point = arcpy.management.SelectLayerByAttribute(moved_point, "NEW_SELECTION", where_clause=f"{field} = {expression}")
        clipped_point = arcpy.analysis.PairwiseClip(selected_moved_point, poly_data_location, f"{scratch}\\{poly_layer_name_part}P")

        
        arcpy.management.CalculateField(clipped_point, field, expression)
        arcpy.AddMessage(f"calculated field for clipped features")

        arcpy.AddMessage(f"point_name: {point_name}")

        for fc in fc_list:
            arcpy.AddMessage(f"fc_name: {os.path.basename(fc)}")
            

        arcpy.AddMessage(f"point_name: {point_name}")
        point_data = [fc for fc in fc_list if point_name in fc][0]

        field_list = arcpy.ListFields(point_data)
        if "MARKER_ID" not in field_list:
            arcpy.management.AddField(point_data, "MARKER_ID", "LONG", field_is_required= "NON_REQUIRED")
            arcpy.AddMessage(f"field added for exisitng {point_name}")


        if int(arcpy.management.GetCount(point_data)[0]) >= 0:
            arcpy.management.Append(clipped_point, point_data, "NO_TEST", expression = f"{field} = 1")
            
            
            arcpy.AddMessage(f"appended data")

        # arcpy.AddMessage(f"feature added to GDB")

        point_layer = next((layer for layer in layer_list if layer.name == point_name), None)
        arcpy.AddMessage(f"point_layer after marker point: {point_layer}")


        arcpy.AddMessage("Ended vegetation_symbol_create")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'vegetation_symbol_create error: {e}\nTraceback details:\n{tb}'
        logger.error(error_message)
        arcpy.AddError(error_message)
        raise


# # Aligning Bridge Points respect to Roads / Tracks / Waterbodies
def align_bridge_point(fc_list, align_bridge_point_input, align_bridge_point_waterbody, align_bridge_point_surface, working_gdb, map_name, logger):
 
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True
    arcpy.env.transferDomains = True
    arcpy.env.referenceScale = "50000"
 
    try:
        arcpy.AddMessage("Started align_bridge_point tool")
 
        #Bridge layer
        
        Bridge_P = next((fc for fc in fc_list if os.path.basename(fc) == align_bridge_point_input), None)
 
        arcpy.AddMessage(f"Bridge_P: {Bridge_P}")
 
        #All waterbody layers
        Waterbody = [fc for fc in fc_list if os.path.basename(fc) in align_bridge_point_waterbody]
 
        #All Transportation polygon surface layers
        Surfaces = [fc for fc in fc_list if os.path.basename(fc) in align_bridge_point_surface]
 
        if Bridge_P and has_features(Bridge_P):
 
            OUTPUT_GDB  = working_gdb
            #Merge all waterbody in one layer
            wb_name = "Waterbody_merged"
            wb_fc = os.path.join(OUTPUT_GDB,wb_name)
            #delete_lyr.append(wb_fc)
            arcpy.management.Merge(
                inputs=Waterbody,
                output=wb_fc,
                field_mappings=None,
                add_source="NO_SOURCE_INFO",
                field_match_mode="AUTOMATIC"
            )

            wb_lyr = arcpy.management.MakeFeatureLayer(wb_fc,"wb_lyr")
            # wb_lyr = aprxMap.addDataFromPath(wb_fc)
            arcpy.AddMessage(f"wb_lyr path: {wb_lyr}")
 
            #Merge all transportation surfaces in one layer
            srf_name = "T_surfaces_merged"
            srf_fc = os.path.join(OUTPUT_GDB,srf_name)
            arcpy.management.Merge(
                inputs=Surfaces,
                output=srf_fc,
                field_mappings=None,
                add_source="NO_SOURCE_INFO",
                field_match_mode="AUTOMATIC"
            )
            srf_lyr = arcpy.management.MakeFeatureLayer(srf_fc,"srf_lyr")
 
            #Select Bridge_P by transportation surface merged
            # bridge_lyr = arcpy.management.MakeFeatureLayer(Bridge_P,"bridge_fc_lyr")
            
            bridge_lyr = get_feature_layer_by_feature_class(os.path.basename(Bridge_P), map_name)[0]
            
            arcpy.AddMessage(f"bridge_lyr: {bridge_lyr}")
            bridge_slct=arcpy.management.SelectLayerByLocation(
                in_layer=bridge_lyr,
                overlap_type="INTERSECT",
                select_features=srf_lyr,
                search_distance=None,
                selection_type="NEW_SELECTION",
                invert_spatial_relationship="NOT_INVERT"
            )
 
            # Align the Bridge_P along with Transportation
            arcpy.cartography.AlignMarkerToStrokeOrFill(
                in_point_features=bridge_lyr,
                in_line_or_polygon_features=srf_lyr,
                search_distance="10 Meters",
                marker_orientation="PERPENDICULAR"
            )
 
            #Select Bridge_P by transportation surface merged
            # bridge_lyr = get_feature_layer_by_feature_class(os.path.basename(Bridge_P), map_name)[0]
            bridge_slct=arcpy.management.SelectLayerByLocation(
                in_layer=bridge_lyr,
                overlap_type="INTERSECT",
                select_features=srf_fc,
                search_distance=None,
                selection_type="NEW_SELECTION",
                invert_spatial_relationship="INVERT"
            )
 
            # Align the Bridge_P along with Transportation
            arcpy.cartography.AlignMarkerToStrokeOrFill(
                in_point_features=bridge_lyr,
                in_line_or_polygon_features=wb_lyr,
                search_distance="10 Meters",
                marker_orientation="PARALLEL",
            )
 
            arcpy.AddMessage(r"Aligning Bridge Points in respect to Road / Track / Waterbodies Completed Successfully")
 
        else:
            arcpy.AddMessage(f"{os.path.basename(Bridge_P)} has no feature, skipping tool.")
 
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Aligning Bridge Point error: {e}\nTraceback details:\n{tb}"
        logger.error(error_message)
        arcpy.AddError(error_message)
        raise

def remove_duplicate_vegetation_marker_point(vegetation_fc, logger, xy_tolerance="1 Meters"):
    if(vegetation_fc):
        logger.info("Proceeding to remove duplicate points from Vegetation marker Points")
        if not has_features(vegetation_fc):
            arcpy.AddMessage(f"No feature was found for {vegetation_fc}")
            return
        else:
            try:
                vegetation_desc = arcpy.Describe(vegetation_fc)
                arcpy.AddMessage(f"Removing duplicate marker points for {vegetation_desc.name}")
                arcpy.management.DeleteIdentical(
                    in_dataset=vegetation_fc,
                    fields=[vegetation_desc.shapeFieldName],
                    xy_tolerance=xy_tolerance,
                )
                arcpy.AddMessage(f"Successfully Deleted geometrical Identical Marker Points for {vegetation_desc.name} ")
                return True
            except Exception as e:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                tb = traceback.format_exc()
                error_message = f"Removing Duplicate Vegetation Marker error: {e}\nTraceback details:\n{tb}"
                arcpy.AddError(error_message)
                logger.error(error_message)
                simplified_msgs('Apply carto symbology', f'{exc_value}\n')
            



def manage_vegetation_symbology(vegetation_detail, input_fc_list, map_name, road_track_distance, logger, map_scale="50000", map_unit="Meter", remove_duplicate_xy_tolerance="1 Meters"):
    arcpy.env.overwriteOutput = True
    if not vegetation_detail:
        return False
    if not input_fc_list or not map_name:
        return
    for vegetation_fc in vegetation_detail:
        vegetation_operation = vegetation_symbol_create(input_fc_list, map_name, vegetation_fc, map_scale, road_track_distance, map_unit, logger)
        if(vegetation_operation):
            vegetation_features = [fc for fc in input_fc_list if vegetation_fc["name"].replace("_A", "_P") == arcpy.Describe(fc).name]
            vegetation_features = vegetation_features[0]
            remove_duplicate_vegetation_marker_point(vegetation_features, logger, remove_duplicate_xy_tolerance)
    return None

# # Apply Cartographic Symbologies
def apply_carto_symbology(fc_list, attribution_fc_list, express_list, query_list, field_list, intersecting_fc_list, working_gdb, feature_loc, vst_workspace, hierarchy_file, prep_line_resolve_fcs_list, 
                          carto_partition, symbology_file_path, map_name, apply_symbology_layers_list, create_vegetation_symbol_detail, align_bridge_config, val_dict, logger):
    arcpy.AddMessage('Starting carto symbolisation application .....')
    logger.info('Starting carto symbolisation application .....')
    # Set the workspace
    arcpy.env.overwriteOutput = True
    try:
        # Apply attribution
        logger.info('Applying Attribution for ApplyCarto .....')
        apply_attribution(fc_list, attribution_fc_list, express_list, query_list, field_list)
        # Skip embankments and cuttings
        logger.info('Applying Attribution for ApplyCarto .....')
        embankment_cutting(fc_list, intersecting_fc_list, working_gdb)
        # Prep for line resolve
        prep_4_line_resolve(fc_list, val_dict['Applycarto_query_acs'], val_dict['Resolve_conflict_build_visible_field'], val_dict['Applycarto_distance_between_features'], val_dict['Applycarto_maximum_number_of_close_features_l'], val_dict['Applycarto_maximum_number_of_close_features_m'], val_dict['Applycarto_maximum_number_of_close_features_u'], prep_line_resolve_fcs_list, working_gdb)
        # Split and Explode lines
        split_explode_lines(fc_list, working_gdb)
        # Calculating values For Tidal Gate Symbology
        HJ0070_Tidal_Gate_P_fc = [fc for fc in fc_list if  resolve_lyr().Tidal_Gate_P in fc][0]
        HH0041_River_Bank_L_fc = [fc for fc in fc_list if resolve_lyr().River_Bank_L in fc][0]
        HM0030_Water_Flow_P_fc = [fc for fc in fc_list if  resolve_lyr().Water_Flow_P in fc][0]

        # arcpy.AddMessage("Calculating Values for Tidal Gate Symbologies...")
        generate_near_and_join_tidal_gate(in_features=HJ0070_Tidal_Gate_P_fc, near_features=HH0041_River_Bank_L_fc, near_table_name="HJ0070_Tidal_Gate_River_Bank_NearT2", logger = logger, output_gdb = working_gdb)
        
        generate_near_and_calculate_orientation(in_features=HM0030_Water_Flow_P_fc, near_features=HH0041_River_Bank_L_fc, near_table_name=resolve_lyr().Water_flow_River_Bank_Near, val_dict=val_dict, logger = logger, output_gdb = working_gdb)
        # # Create carto partition
        aprx = arcpy.mp.ArcGISProject('CURRENT')
        fc_layers = None
        maps = aprx.listMaps(map_name)
        if(maps):
            fc_layers = [lyr for lyr in maps[0].listLayers() if not lyr.isGroupLayer and not lyr.isBroken and lyr.isFeatureLayer] 
        create_carto_partition(fc_layers, feature_loc, val_dict['Applycarto_feature_count'])
        # # Calculate VST on workspace
        # calc_vst_on_workspace(fc_list, symbology_file_path, apply_symbology_layers_list, map_name, feature_loc)
        # Populate hierarchy
        populate_hierarchy_new(hierarchy_file, feature_loc, val_dict['Resolve_conflict_build_hierarchy_field'], working_gdb)
        ## Manage Vegetation Point Features from Markers
        # Marker duplicate tolerance was added later; fall back to the function
        # default when an older config file does not have the key.
        manage_vegetation_symbology(create_vegetation_symbol_detail, fc_list, map_name, val_dict['ApplyCarto_Vegetation_RoadTrackDistance'], logger, val_dict['ApplyCarto_Map_Scale'], val_dict['ApplyCarto_Map_Unit_of_Dataset'], val_dict.get("ApplyCarto_Marker_Duplicate_XY_Tolerance", "1 Meters"))
        # # Aligning Bridge Points
        align_bridge_point(fc_list, align_bridge_config['align_bridge_point_input'], align_bridge_config['align_bridge_point_waterbody'], align_bridge_config['align_bridge_point_surface'], working_gdb, map_name, logger)
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Apply carto symbology error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Apply carto symbology', f'{exc_value}\n')