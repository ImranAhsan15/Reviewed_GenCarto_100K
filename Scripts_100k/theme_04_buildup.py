import arcpy
import traceback
import sys
import os
import datetime as _dt
from common_utils import *

def area_based_delete(path, townbuiltup_min_area):
    sql_query = f"Shape_Area < {townbuiltup_min_area}"
    arcpy.AddMessage(f"{sql_query}") 
    selected_temp_dis_layer = arcpy.management.SelectLayerByAttribute(path,"NEW_SELECTION",sql_query)
    arcpy.AddMessage(f"Features count before processing: {count_features(path)}")
    if int(arcpy.management.GetCount(selected_temp_dis_layer)[0]) > 0:
        arcpy.management.DeleteFeatures(selected_temp_dis_layer)
        arcpy.AddMessage(f"Features count after processing: {count_features(path)}")

def delete_if_exists(path):
    if arcpy.Exists(path):
        arcpy.management.Delete(path)



def convert_small_bldg_2_point(fc_list, small_bldg_2_point_a, small_bldg_2_point_p, min_size_bldg, delete_input, one_point, unique_field, working_gdb):
    try:
        small_bldg_2_point_a = list(filter(str.strip, small_bldg_2_point_a))
        small_bldg_2_point_a = [fc for a_lyr in small_bldg_2_point_a for fc in fc_list if str(a_lyr) in fc]
        small_bldg_2_point_p = list(filter(str.strip, small_bldg_2_point_p))
        small_bldg_2_point_p = [fc for p_lyr in small_bldg_2_point_p for fc in fc_list if str(p_lyr) in fc]

        # Small building to point. Note: the configured one_point value is
        # deliberately overridden - buildings always create one point per feature.
        for inFc, point_fc in zip(small_bldg_2_point_a, small_bldg_2_point_p):
            if has_features(inFc):
                feature2point_bldg(inFc, point_fc, min_size_bldg, delete_input, True, unique_field, working_gdb)
    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Convert small building to point error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def delete_features_in_poly(features_in_cemetery, poly_fc, poly_size):
    # Set the workspace
    arcpy.env.overwriteOutput = True
    try:
        desc = arcpy.da.Describe(poly_fc)
        shape_delim = desc['areaFieldName']
        sizeQuery = shape_delim + " <= " + str(poly_size)
        arcpy.AddMessage(f'sizeQuery is: {sizeQuery}')
        for pt_fc in features_in_cemetery:
            if not has_features(pt_fc):
                arcpy.AddMessage(f"Skipping {pt_fc} as it has no features") #new continue block added to avoid operation on empty feature classes 
                continue
            # Make Feature Layer for the input feature class
            pt_lyr = arcpy.management.MakeFeatureLayer(pt_fc, "point_lyr")

            point_count = int(arcpy.management.GetCount("point_lyr").getOutput(0))
            if point_count >= 1:
                poly_lyr = arcpy.management.MakeFeatureLayer(poly_fc, "polygon_lyr")
                arcpy.management.SelectLayerByAttribute(poly_lyr, "", sizeQuery)

                # Find all features that fall within the selected polygons
                arcpy.management.SelectLayerByLocation(pt_lyr, "INTERSECT", poly_lyr)
                point_count = int(arcpy.management.GetCount("point_lyr").getOutput(0))
                arcpy.AddMessage(str(point_count))
                # Determine if pt_fc is a point or polygon feature class
                desc = arcpy.da.Describe(pt_fc)
                if desc['shapeType'] == 'Polygon':
                    # Optional code to fil holes in cemetery if topology exists between the two
                    point_count = int(arcpy.management.GetCount("point_lyr").getOutput(0))
                    if point_count >= 1:
                        # If features still remain, just delete them
                        arcpy.AddMessage(str(point_count) + " features will be deleted")
                        arcpy.management.DeleteFeatures("point_lyr")

                if desc['shapeType'] == 'Point':
                    # If features are selected, delete them
                    point_count = int(arcpy.management.GetCount("point_lyr").getOutput(0))
                    if point_count >= 1:
                        arcpy.AddMessage(str(point_count) + " features will be deleted")
                        arcpy.management.DeleteFeatures("point_lyr")
                    else:
                        arcpy.AddMessage("No features will be deleted")
                # Delete temp files
                delete_list = ["point_lyr", "polygon_lyr"]
                arcpy.management.Delete(delete_list)
                
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete features in polygon error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def delete_small_building(fc_list, delete_small_bldgs, del_min_area):
    try:
        delete_small_bldgs = list(filter(str.strip, delete_small_bldgs))
        delete_small_bldgs = [fc for a_lyr in delete_small_bldgs for fc in fc_list if str(a_lyr) in fc]

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


def simplify_buildings(polygon_fc, distance, working_gdb):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    try:
        field = "BLD_STATUS"

        desc = arcpy.da.Describe(polygon_fc)
        fc_name = desc['name']
        oid_field = desc['OIDFieldName']
        simple_fc = working_gdb + "\\"+ fc_name + "_Simple"
        if arcpy.Exists(simple_fc):
            arcpy.management.Delete(simple_fc)

        smooth_fc = working_gdb + "\\"+ fc_name + "_Smooth"
        if arcpy.Exists(smooth_fc):
            arcpy.management.Delete(smooth_fc)

        arcpy.AddMessage("running simplify")
        arcpy.management.AddField(polygon_fc, "BLD_STATUS", "LONG")
        simplify_features = arcpy.management.MakeFeatureLayer(polygon_fc, "simplify_features")

    # Check for feature classes with no features
        # result = arcpy.management.GetCount(simplify_features)
        # count = int(result.getOutput(0))

        if has_features(simplify_features):   #added has_features check instead of count check
            # Run simplify buildings
            arcpy.cartography.SimplifyBuilding(simplify_features, simple_fc, distance)
            field_delimited = arcpy.AddFieldDelimiters(simple_fc, field)
            query = field_delimited + " <> 5"
            simple_lyr = arcpy.management.MakeFeatureLayer(simple_fc, "simple_lyr")
            arcpy.management.SelectLayerByAttribute(simple_lyr, "", query)

            # Replace the original features with simplified geometries
            with arcpy.da.SearchCursor(simple_lyr, ['SHAPE@', 'InBld_FID']) as cursor:
                arcpy.AddMessage("replacing geometries")
                for row in cursor:
                    update_sql = oid_field + " = " + str(row[1])

                    with arcpy.da.UpdateCursor(polygon_fc, ['SHAPE@', 'oid@'], update_sql) as uCursor:
                        for uRow in uCursor:
                            #replace the shape if it is not identical
                            if not uRow[0].equals(row[0]):
                                uRow[0] = row[0]
                                uCursor.updateRow(uRow)
            # Delete temp files 
            arcpy.management.DeleteField(polygon_fc, field)

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Simplify buildings error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def delineate_built_up_area(fc_list, in_buildings_list, edge_features_list, grouping_distance, minimum_detail_size, minimum_building_count, working_gdb, delineate_ref_scale, townbuiltup_min_area):
    # Define environment variables
    arcpy.env.overwriteOutput = True
    arcpy.env.referenceScale = delineate_ref_scale
    dynamic_fc_names = resolve_lyr()
    try:
        
        in_buildings_list = list(filter(str.strip, in_buildings_list))
        in_buildings = [fc for a_lyr in in_buildings_list for fc in fc_list if str(a_lyr) in fc]
        edge_features_list = list(filter(str.strip, edge_features_list))
        edge_features = [fc for a_lyr in edge_features_list for fc in fc_list if str(a_lyr) in fc]
        # Town Built Up Fc Layer
        town_buil_up = [fc for fc in fc_list if dynamic_fc_names.Town_Built_up_A in fc][0]
        out_feature_class = f"{working_gdb}\\temp_built_up"
        temp_elim_layer = f"{working_gdb}\\temp_elm_fc"
        append_sr = arcpy.Describe(town_buil_up).spatialReference
        # Set output coordinate system same as append_layer
        arcpy.env.outputCoordinateSystem = append_sr
        arcpy.cartography.DelineateBuiltUpAreas(in_buildings, "", edge_features, f"{grouping_distance} Meters", f"{minimum_detail_size} Millimeters", out_feature_class, minimum_building_count)
        
        
        # Append with town build-up layer
        arcpy.management.Append(inputs=out_feature_class, target=town_buil_up, schema_type="NO_TEST")
        arcpy.management.Dissolve(in_features=town_buil_up, out_feature_class=f"{working_gdb}\\temp_dissolve_town", dissolve_field=None, statistics_fields=None, multi_part="SINGLE_PART")
        arcpy.management.EliminatePolygonPart(in_features=f"{working_gdb}\\temp_dissolve_town",out_feature_class=temp_elim_layer,condition="AREA",part_area="150000 SquareMeters",part_area_percent=0,
                                              part_option="CONTAINED_ONLY")
        arcpy.management.TruncateTable(town_buil_up)
        arcpy.management.Append(inputs=temp_elim_layer, target=town_buil_up, schema_type="NO_TEST")
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delineate built-up area error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)




def generalised_buildings(fc_list, general_builtup_min_area):
    dynamic_fc_names = resolve_lyr()
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        local_authoruty_cover = [fc for fc in fc_list if dynamic_fc_names.Local_Authority_Area_A in fc][0]
        if count_features(local_authoruty_cover)>0:
            town_buil_up = [fc for fc in fc_list if dynamic_fc_names.Town_Built_up_A in fc][0]
            generalised_building = [fc for fc in fc_list if dynamic_fc_names.Generalised_Buildings_A in fc][0]
            # Make feature layers
            local_authoruty_cover = arcpy.management.MakeFeatureLayer(local_authoruty_cover, "local_authoruty_cover")
            town_buil_up = arcpy.management.MakeFeatureLayer(town_buil_up, "town_buil_up")
            # Town buil up selection by Local Authority Cover Layer
            selected_townbuildup = arcpy.management.SelectLayerByLocation(town_buil_up, 'INTERSECT', local_authoruty_cover, None, 'NEW_SELECTION')
            if count_features(selected_townbuildup)>0:
                arcpy.management.SelectLayerByAttribute(selected_townbuildup, "SWITCH_SELECTION")
                # Append town built up areas with generalised building
                arcpy.management.Append(selected_townbuildup, generalised_building, 'NO_TEST')
                # Delete features from town built up areas
                arcpy.management.DeleteFeatures(selected_townbuildup)
                gen_sql_query = F"Shape_Area < {general_builtup_min_area}"
                selected_gen_temp_dis_layer=arcpy.management.SelectLayerByAttribute(generalised_building,"NEW_SELECTION",gen_sql_query)
                if count_features(selected_gen_temp_dis_layer)>0:
                    arcpy.management.DeleteFeatures(selected_gen_temp_dis_layer)
                arcpy.AddMessage("Successfull to append features from Town Builtup to Generalised Buildings layer")
            else:
                arcpy.AddMessage("no features are selected within local authority boundary")
        else:arcpy.AddMessage("no features are found within local authority boundary, skipping the generalised building function")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Generalised building error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def resolve_fence(pond, fence, scratch_gdb, distance="17 Meters", map_name = "04 Built-Up Generalization"):
    arcpy.env.overwriteOutput = 1
    arcpy.AddMessage("Starting Resolving Fence")
    try:
        pond_base_name = os.path.basename(pond)
        fence_base_name = os.path.basename(fence)
        pond_layer = get_feature_layer_by_feature_class(os.path.basename(pond), map_name)[0]
        fence_layer = get_feature_layer_by_feature_class(os.path.basename(fence), map_name)[0]
        if has_features(pond) and has_features(fence):
            fence_line_to_move = arcpy.management.SelectLayerByLocation(
                in_layer=fence_layer,
                overlap_type="INTERSECT",
                select_features=pond_layer,
                search_distance=distance,
                selection_type="NEW_SELECTION",
                invert_spatial_relationship="NOT_INVERT"
            )

            pond_buffer = arcpy.analysis.PairwiseBuffer(
                in_features=pond_layer,
                out_feature_class=f"{scratch_gdb}\\{pond_base_name}_Buffer",
                buffer_distance_or_field=distance,
                method="PLANAR"
            )
            
            fence_vertices = arcpy.management.FeatureVerticesToPoints(
                in_features=fence_layer,
                out_feature_class=f"{scratch_gdb}\\{fence_base_name}_FeatureVertice",
                point_location="ALL"
            )

            pond_buffer_line = arcpy.management.FeatureToLine(
                in_features=pond_buffer,
                out_feature_class=f"{scratch_gdb}\\{pond_base_name}_Buff_Line",
                cluster_tolerance=None,
                attributes="ATTRIBUTES"
            )

            arcpy.analysis.Near(
                in_features=fence_vertices,
                near_features=pond_buffer_line,
                search_radius=None,
                location="LOCATION",
                angle="NO_ANGLE",
                method="PLANAR",
                distance_unit="",
                match_fields=None
            )

            arcpy.management.SelectLayerByAttribute(
                in_layer_or_view=fence_vertices,
                selection_type="NEW_SELECTION",
                where_clause="NEAR_DIST <> -1",
                invert_where_clause=None
            )

            new_fence_points = arcpy.management.XYTableToPoint(
                in_table=fence_vertices,
                out_feature_class=f"{scratch_gdb}\\{fence_base_name}_FeatureVertice_XYTableToPoint",
                x_field="NEAR_X",
                y_field="NEAR_Y",
                coordinate_system='PROJCS["GDM_2000_MRSO_Peninsular_Malaysia",GEOGCS["GCS_GDM_2000",DATUM["D_GDM_2000",SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Rectified_Skew_Orthomorphic_Natural_Origin"],PARAMETER["False_Easting",804671.0],PARAMETER["False_Northing",0.0],PARAMETER["Scale_Factor",0.99984],PARAMETER["Azimuth",323.0257964666666],PARAMETER["Longitude_Of_Center",102.25],PARAMETER["Latitude_Of_Center",4.0],PARAMETER["XY_Plane_Rotation",-36.86989764584402],UNIT["Meter",1.0]];-30656400 -28732700 10000;-100000 10000;-100000 10000;0.001;0.001;0.001;IsHighPrecision'
            )
            modified_fence_line = arcpy.management.PointsToLine(
                Input_Features=new_fence_points,
                Output_Feature_Class=f"{scratch_gdb}\\{fence_base_name}_FeatureVertice_XYTableToPoint_PointsToLine",
                Line_Field="ORIG_FID",
                Sort_Field="NEAR_FID",
                Close_Line="NO_CLOSE",
                Line_Construction_Method="CONTINUOUS",
                Attribute_Source="NONE",
                Transfer_Fields=None
            )
            
            modified_fence_line_fields = [fld.name for fld in arcpy.ListFields(modified_fence_line) if fld.name not in ["OBJECTID", "Shape", "Shape_Length"]]
            modified_joined_fence_line = arcpy.analysis.SpatialJoin(
                target_features=modified_fence_line,
                join_features=fence_layer,
                out_feature_class=f"{scratch_gdb}\\{fence_base_name}_F_SpatialJoin",
                join_operation="JOIN_ONE_TO_ONE",
                join_type="KEEP_ALL",
                match_option="WITHIN_A_DISTANCE",
                search_radius=distance,
                distance_field_name="",
                match_fields=None
            )

            arcpy.management.DeleteField(
                in_table=modified_joined_fence_line,
                drop_field=modified_fence_line_fields,
                method="DELETE_FIELDS"
            )
            # Clearing Selection
            arcpy.management.SelectLayerByAttribute(fence_layer, "CLEAR_SELECTION")
            arcpy.management.SelectLayerByLocation(
                in_layer=fence_layer,
                overlap_type="INTERSECT",
                select_features=pond_layer,
                search_distance=distance,
                selection_type="NEW_SELECTION",
                invert_spatial_relationship="NOT_INVERT"
            )
            arcpy.management.DeleteRows(
                in_rows=fence_line_to_move
            )
            arcpy.management.Append(
                inputs = [modified_joined_fence_line], 
                target= fence, 
                schema_type="NO_TEST"
            )
            arcpy.AddMessage(f"Resolving conflict between {pond} and {fence} are successful.")
        else:
            arcpy.AddMessage(f"Data could not be found in either {pond} or {fence} feature classes.")
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"resolve_fence error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def get_xy(pt_or_geom):
    """
    Returns X and Y as a point geometry
    Author: Shahmin Aurnov
    """
    if hasattr(pt_or_geom, "firstPoint") and pt_or_geom.firstPoint:
        p = pt_or_geom.firstPoint
        return p.X, p.Y
    else:
        return pt_or_geom.X, pt_or_geom.Y

def assign_distance(fc, class_field, rules_dict):
    """
    Assigns distance values based on class field
    Author: Shahmin Aurnov
    """
    if "DIST_M" not in [f.name for f in arcpy.ListFields(fc)]:
        arcpy.management.AddField(fc, "DIST_M", "DOUBLE")
    
    with arcpy.da.UpdateCursor(fc, [class_field, "DIST_M"]) as cur:
        for cls, dist_m in cur:
            try:
                cls_code = int(cls)
                new_dist = rules_dict.get(cls_code)
            except (TypeError, ValueError):
                new_dist = None
            cur.updateRow((cls, new_dist))

def adjust_based_on_distance(road_fc, track_fc, target_fc, road_class_field, track_class_field,
                                    road_distance_rules, track_distance_rules, working_gdb):
    """
    Adjusts fence geometries based on proximity to roads and tracks, applying distance rules.
    Author: Shahmin Aurnov
    """
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True

    ### Step-1: Preparing Datasets for conflict resolution between fence and road/track
    # Temporary feature classes for roads and tracks
    road_tmp = fr"{working_gdb}\roads_tmp"
    track_tmp = fr"{working_gdb}\tracks_tmp"


    # Copying features for roads and tracks
    arcpy.conversion.ExportFeatures(road_fc, road_tmp)
    arcpy.conversion.ExportFeatures(track_fc, track_tmp)

    # Assigning distances
    assign_distance(road_tmp, road_class_field, road_distance_rules)
    assign_distance(track_tmp, track_class_field, track_distance_rules)

    # Merging road and track features into one base layer
    base_all_fc = fr"{working_gdb}\base_all"
    arcpy.management.Merge([road_tmp, track_tmp], base_all_fc)

    # Copying target fences to avoid modifying the originals
    out_layer = fr"{working_gdb}\BJ0400_Fence_L_moved"
    arcpy.conversion.ExportFeatures(target_fc, out_layer)

    ### Step-2: Starting work to identify the side and position of fences along road and track lines
    # Running Near analysis between fences and merged base features
    search_radius = max(max(road_distance_rules.values()), max(track_distance_rules.values()))
    arcpy.analysis.Near(out_layer, base_all_fc, search_radius=search_radius)

    # Extracting base feature geometries and their distance rules into a dictionary
    base_oid_field = arcpy.Describe(base_all_fc).oidFieldName
    base_geom_dict = {
        oid: (geom, dist_m)
        for oid, geom, dist_m in arcpy.da.SearchCursor(base_all_fc, [base_oid_field, "SHAPE@", "DIST_M"])
    }

    ### Step-3: Updating fence geometries based on proximity to base features
    fields = ["OID@", "SHAPE@", "NEAR_FID", "NEAR_DIST"]
    moved, skipped_no_road, skipped_degenerate, skipped_no_rule = 0, 0, 0, 0

    with arcpy.da.UpdateCursor(out_layer, fields) as cur:
        for oid, geom, near_fid, near_dist in cur:
            if near_fid == -1 or geom is None:
                skipped_no_road += 1
                continue

            base_info = base_geom_dict.get(near_fid)
            if not base_info:
                skipped_no_road += 1
                continue

            base_geom, move_distance = base_info
            if move_distance is None:
                skipped_no_rule += 1
                continue

            # Calculating fence axis and normalization
            fp = geom.firstPoint
            lp = geom.lastPoint
            vx, vy = lp.X - fp.X, lp.Y - fp.Y
            axis_len = math.hypot(vx, vy)
            if axis_len == 0:
                ext = geom.extent
                vx, vy = ext.XMax - ext.XMin, ext.YMax - ext.YMin
                axis_len = math.hypot(vx, vy)
                if axis_len == 0:
                    skipped_degenerate += 1
                    continue

            vx, vy = vx / axis_len, vy / axis_len
            nLx, nLy, nRx, nRy = -vy, vx, vy, -vx

            # Getting the base point for the fence
            ref_pt_geom = geom.labelPoint
            try:
                base_pt_geom, _, _, _ = base_geom.queryPointAndDistance(ref_pt_geom)
            except SystemError:
                base_pt_geom = base_geom.centroid

            rx, ry = get_xy(base_pt_geom)

            # Determining which side of the fence to adjust
            ox, oy = fp.X, fp.Y
            vrx, vry = rx - ox, ry - oy
            cross = vx * vry - vy * vrx
            nx, ny = (nRx, nRy) if cross > 0 else (nLx, nLy)

            # Calculating the required translation offset
            offset = move_distance - near_dist
            if offset <= 0:
                continue

            tx, ty = nx * offset, ny * offset

            # Applying translation to fence vertices
            new_parts = []
            for part in geom:
                arr = arcpy.Array()
                for pt in part:
                    if pt:
                        arr.add(arcpy.Point(pt.X + tx, pt.Y + ty, pt.Z, pt.M))
                    else:
                        arr.add(pt)
                new_parts.append(arr)

            new_geom = arcpy.Polyline(arcpy.Array(new_parts), geom.spatialReference)
            cur.updateRow((oid, new_geom, near_fid, near_dist))
            moved += 1

    ### Step-4: Cleaning up output by deleting unwanted fields
    reference_fields = [f.name for f in arcpy.ListFields(target_fc)]
    output_fields = [f.name for f in arcpy.ListFields(out_layer)]
    fields_to_delete = [f.name for f in arcpy.ListFields(out_layer) if f.name not in reference_fields and not f.required]
    if fields_to_delete:
        arcpy.management.DeleteField(out_layer, fields_to_delete)

    ### Step-5: Appending the adjusted fences back to the original target feature class
    arcpy.management.DeleteRows(target_fc)
    arcpy.management.Append(inputs=[out_layer], target=target_fc, schema_type="NO_TEST")

    return {
        "moved": moved,
        "skipped_no_road": skipped_no_road,
        "skipped_degenerate": skipped_degenerate,
        "skipped_no_rule": skipped_no_rule,
        "output_fc": target_fc
    }


def layer_from_map(layer_name, map_name):
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    m = next(mp for mp in aprx.listMaps() if mp.name == map_name)

    lyr = next(
        lyr for lyr in m.listLayers()
        if lyr.isFeatureLayer and lyr.name == layer_name
    )
    return lyr.dataSource


def fix_wall_fence_conflict_with_road(road_fc, track_fc, target_fc, road_distance_rules, 
                                      track_distance_rules, working_gdb, logger, 
                                      road_class_field='RCS', track_class_field='TCS'):
    try:
        # map_name = "04 Built-Up Generalization"
        # road_layer = layer_from_map(os.path.basename(road_fc), map_name)
        # track_layer = layer_from_map(os.path.basename(track_fc), map_name)
        # target_layer = layer_from_map(os.path.basename(target_fc), map_name)

        result = adjust_based_on_distance(
                    road_fc=road_fc,
                    track_fc=track_fc,
                    target_fc=target_fc,
                    road_class_field=road_class_field, 
                    track_class_field=track_class_field,
                    road_distance_rules=road_distance_rules,
                    track_distance_rules=track_distance_rules,
                    working_gdb=working_gdb
                )
        return result

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Built-Up Area Generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Built-Up Area Generalisation', f'{exc_value}\n')


def merge_buildings_too_closed_between_building_and_street(in_fc_name, building_fc_name, fc_list, working_gdb, logger, area_threshold = 100000):
    arcpy.env.workspace = working_gdb
    count_overlap_output = f"{working_gdb}\\{building_fc_name}_CountOverlap"
    backup_fc = f"{working_gdb}\\{building_fc_name}_backup"
    in_feature = None
    building_feature = None
    in_feature_list = [fc for fc in fc_list if in_fc_name in fc]
    building_feature_list = [fc for fc in fc_list if building_fc_name in fc]
    if(in_feature_list):
        in_feature = in_feature_list[0]
    if(building_feature_list):
        building_feature = building_feature_list[0]

    arcpy.AddMessage(f"in_feature: {in_feature} and building_feature: {building_feature}")
    in_feature_to_polygon = f"{working_gdb}\\{in_fc_name}_FeatureToPolygon"
    # # Convert Line to Polygon
    arcpy.management.FeatureToPolygon(in_feature, in_feature_to_polygon, None, "ATTRIBUTES")
    arcpy.management.MakeFeatureLayer(in_feature_to_polygon, "in_feature_to_poly_layer")
    # # Delete polygons > area threshold
    arcpy.management.SelectLayerByAttribute("in_feature_to_poly_layer", "NEW_SELECTION", f"Shape_Area > {area_threshold}")
    arcpy.management.DeleteFeatures("in_feature_to_poly_layer")
    # # Delete polygons that intersect buildings
    arcpy.management.SelectLayerByLocation(
        "in_feature_to_poly_layer", "INTERSECT", building_feature, selection_type="NEW_SELECTION", invert_spatial_relationship="INVERT"
    )
    arcpy.management.DeleteFeatures("in_feature_to_poly_layer")
    # # Count overlapping buildings
    arcpy.analysis.CountOverlappingFeatures(building_feature, count_overlap_output, 1)
    arcpy.management.MakeFeatureLayer(count_overlap_output, "count_overlap_lyr")
    # # Select buildings with Count_ > 1
    arcpy.management.SelectLayerByAttribute("count_overlap_lyr", "NEW_SELECTION", "Count_ > 1")
    # # Delete polygons intersecting overlapping buildings
    arcpy.management.SelectLayerByLocation(
        "in_feature_to_poly_layer", "INTERSECT", "count_overlap_lyr", selection_type="NEW_SELECTION", invert_spatial_relationship="INVERT"
    )
    arcpy.management.DeleteFeatures("in_feature_to_poly_layer")
    if not arcpy.Exists(backup_fc):
        arcpy.management.CopyFeatures(building_feature, backup_fc)
        logger.info(f"Backup created: {backup_fc}")
    else:
        logger.info(f"Backup already exists: {backup_fc}")
    # # Delete buildings that intersect cleaned polygons
    arcpy.management.MakeFeatureLayer(building_feature, "building_lyr")
    arcpy.management.SelectLayerByLocation("building_lyr", "INTERSECT", "in_feature_to_poly_layer")
    arcpy.management.DeleteFeatures("building_lyr")
    logger.info("Deleted buildings intersecting polygons")
    arcpy.management.Append("in_feature_to_poly_layer", building_feature, "NO_TEST")
    logger.info("Polygons appended into building feature class")
    return None


def align_feature_with_reference_fc(align_fc, referenece_fc, fc_list, working_gdb, logger):
    align_feature = None
    reference_feature = None
    if(align_fc):
        # Check If Align FC exists in Feature Class List
        align_feature_list = [fc for fc in fc_list if align_fc in fc]
        if(align_feature_list):
          align_feature =  align_feature_list[0]
    if(referenece_fc):
        # Check If Align FC exists in Feature Class List
        reference_feature_list = [fc for fc in fc_list if referenece_fc in fc]
        if(reference_feature_list):
          reference_feature =  reference_feature_list[0]
    ref_fc_buffer=arcpy.analysis.PairwiseBuffer(
        in_features=reference_feature,
        out_feature_class=rf"{working_gdb}\\ref_fc_buffer",
        buffer_distance_or_field="30 Meters",
        dissolve_option="ALL",
        dissolve_field=None,
        method="PLANAR",
        max_deviation="0 Meters"
    )
    logger.info(f"The buffer for {reference_feature} has been created")
    ref_fc_buffer_toline=arcpy.management.FeatureToLine(
        in_features=ref_fc_buffer,
        out_feature_class=rf"{working_gdb}\\ref_fc_buffer_toline",
        cluster_tolerance=None,
        attributes="NO_ATTRIBUTES"
    )
    arcpy.edit.AlignFeatures(
        in_features=align_feature,
        target_features=ref_fc_buffer_toline,
        search_distance="50 Meters",
        match_fields=None
    )
    logger.info(f"The {align_fc} has been aligned with {referenece_fc} successfully.")


def move_feature_1_around_feature_2_to_specific_distance(fc_list, ref_fc_classes: list, to_move_fc_classes : list, working_gdb, logger, min_distance = "12.5") -> None:
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    logger.info(f"Starting moving {to_move_fc_classes} around {ref_fc_classes} to {min_distance} meters distance")
    
    ref_original_fcs = []
    to_move_original_fcs = []
    if(fc_list == None or ref_fc_classes == None):
        logger.error("No feature class list was provided. Exiting move_feature_1_around_feature_2_to_specific_distance..")
        return None
    
    # Validate Inputs
    for fc in ref_fc_classes + to_move_fc_classes:
        if fc not in [os.path.basename(fc_name) for fc_name in fc_list]:
            raise ValueError(f"Feature class not found in fc_list: {fc}")
        else:
            for temp_fc in fc_list:
                if fc == os.path.basename(temp_fc)  and fc in ref_fc_classes:
                    ref_original_fcs.append(temp_fc)
                if fc == os.path.basename(temp_fc) and fc in to_move_fc_classes:
                    to_move_original_fcs.append(temp_fc)
    logger.info("Creating working copies")
    arcpy.AddMessage(f"ref_original_fcs: {(ref_original_fcs)}")
    arcpy.AddMessage(f"to_move_original_fcs: {(to_move_original_fcs)}")
    ref_wrk = []
    move_wrk = []
    for fc in to_move_original_fcs:
        add_source_tracking(fc)
    
    # Create Working Copies (Preserve Attributes)
    for fc in ref_original_fcs:
        if(has_features(fc)):
            out_fc = os.path.join(working_gdb, f"{os.path.basename(fc)}_wrk")
            arcpy.management.CopyFeatures(fc, out_fc)
            ref_wrk.append(out_fc)

    for fc in to_move_original_fcs:
        if(has_features(fc)):
            out_fc = os.path.join(working_gdb, f"{os.path.basename(fc)}_wrk")
            arcpy.management.CopyFeatures(fc, out_fc)
            move_wrk.append(out_fc)

    # Merge Reference Features
    logger.info("Merging reference feature classes")

    ref_merged = os.path.join(working_gdb, "ref_merged")
    if(len(ref_wrk) < 1):
        logger.warning(f"No feature could be found for reference feature classes {ref_fc_classes}. Skipping...")
        return None
    arcpy.management.Merge(ref_wrk, ref_merged)
    
    
    # Create Buffer at Required Distance
    logger.info(f"Creating buffer at {min_distance}")

    ref_buffer = os.path.join(working_gdb, "ref_buffer")
    arcpy.analysis.Buffer(
        ref_merged,
        ref_buffer,
        min_distance,
        dissolve_option="ALL"
    )

    # Convert Buffer to Boundary Line
    logger.info("Extracting buffer boundary")

    buffer_boundary = os.path.join(working_gdb, "buffer_boundary")
    arcpy.management.PolygonToLine(ref_buffer, buffer_boundary)

    # Merge Move Feature Classes
    logger.info("Merging features to move")

    move_merged = os.path.join(working_gdb, "move_merged")
    arcpy.management.Merge(move_wrk, move_merged)

    # Select Only Features Violating Distance
    logger.info("Selecting features inside buffer")

    arcpy.management.MakeFeatureLayer(move_merged, "move_lyr")

    arcpy.management.SelectLayerByLocation(
        "move_lyr",
        "INTERSECT",
        ref_buffer
    )
    # Near Analysis to Buffer Boundary
    logger.info("Running Near analysis")

    arcpy.analysis.Near(
        "move_lyr",
        buffer_boundary,
        location="LOCATION"
    )

    # Move Features (Preserve Geometry)
    logger.info("Moving geometries")

    with arcpy.da.UpdateCursor(
        "move_lyr",
        ["OID@", "SHAPE@", "NEAR_X", "NEAR_Y"]
    ) as cursor:

        for oid, shape, nx, ny in cursor:
            if nx is None or ny is None:
                continue

            if shape.type == "polygon":
                ref_pt = shape.centroid
            else:
                ref_pt = shape

            dx = nx - ref_pt.X
            dy = ny - ref_pt.Y

            new_shape = shape.move(dx, dy)
            cursor.updateRow([oid, new_shape, nx, ny])

    # Split Moved Results by Geometry Type
    logger.info("Separating moved features by geometry")

    moved_geom_dict = {}
    with arcpy.da.SearchCursor(
        move_merged,
        ["SRC_FC", "SRC_OID", "SHAPE@"]
    ) as cursor:
        for src_fc, src_oid, geom in cursor:
            moved_geom_dict[(src_fc, src_oid)] = geom

    # Replace Geometry in Original Feature Classes
    logger.info("Updating original feature classes")

    for fc in to_move_original_fcs:
        desc = arcpy.Describe(fc)
        fc_name = desc.baseName
        with arcpy.da.UpdateCursor(fc, ["OID@", "SHAPE@"]) as uc:
            for oid, shape in uc:
                key = (fc_name, oid)
                if key in moved_geom_dict:
                    uc.updateRow([oid, moved_geom_dict[key]])
    return None


def extend_cemetery_with_road_river(
    cemetery,              # Cemetery layer that needs to be fixed
    road,       # extend layer with which to fix cemetery
    gdb):

    arcpy.env.overwriteOutput = True
    
    distance_m=20.0
    prefix_base="BH0010_fix"
    # ----------------------------
    # Inputs (dynamic)
    # ----------------------------
    dist_txt = f"{distance_m} Meters"

    # Unique suffix so repeated runs do not collide
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{prefix_base}_{ts}"

    # ----------------------------
    # Output / intermediate paths
    # ----------------------------
    road_buffer_fc = os.path.join(gdb, f"{prefix}_Road_Buffer")

    # Working copies 
    cem_work_fc = os.path.join(gdb, f"{prefix}_Cemetery_WORK")  # copy of entire cemetery
    cem_sel_fc = os.path.join(gdb, f"{prefix}_Cemetery_SEL")    # selected (within buffer) copy
    cem_line_fc = os.path.join(gdb, f"{prefix}_Cemeter_FeatureToLine")
    cem_poly_fc = os.path.join(gdb, f"{prefix}_Cemeter_FeatureToPoly")
    sj_fc = os.path.join(gdb, f"{prefix}_Cemeter_SpatialJoin")
    merge_fc = os.path.join(gdb, f"{prefix}_Cemeter_Merge_FINAL")

    # Backup 
    cem_backup_fc = os.path.join(gdb, f"{prefix}_Cemetery_BACKUP")

    arcpy.AddMessage("Starting safe run...")
    arcpy.AddMessage("Creating backup copy of cemetery...")
    arcpy.management.CopyFeatures(cemetery, cem_backup_fc)

    arcpy.AddMessage("Creating full working copy of cemetery...")
    arcpy.management.CopyFeatures(cemetery, cem_work_fc)

    # ----------------------------
    # STEP 1: Buffer road
    # ----------------------------
    arcpy.analysis.PairwiseBuffer(
        in_features=road,
        out_feature_class=road_buffer_fc,
        buffer_distance_or_field=dist_txt,
        dissolve_option="NONE",
        dissolve_field=None,
        method="PLANAR"
    )
    arcpy.AddMessage(f"Layer buffer created: {road_buffer_fc}")

    # ----------------------------
    # STEP 2: Select cemetery within buffer
    # ----------------------------
    cem_lyr = arcpy.management.MakeFeatureLayer(cemetery, f"{prefix}_cem_lyr")
    arcpy.management.SelectLayerByLocation(
        in_layer=cem_lyr,
        overlap_type="INTERSECT",
        select_features=road_buffer_fc,
        search_distance=None,
        selection_type="NEW_SELECTION",
        invert_spatial_relationship="NOT_INVERT"
    )
    sel_count = int(arcpy.management.GetCount(cem_lyr)[0])
    arcpy.AddMessage(f"Cemetery selected within {dist_txt}: {sel_count}")

    if sel_count>0:
        # Copy selected to a separate FC (so downstream tools are bounded to only what you intended)
        arcpy.management.CopyFeatures(cem_lyr, cem_sel_fc)

        # ----------------------------
        # STEP 3: Snap selected cemetery (on selected copy, not on original)
        # ----------------------------

        snap_env = [
        [road, "VERTEX", dist_txt], 
        [road, "EDGE", dist_txt]
        ]
        arcpy.edit.Snap(cem_sel_fc, snap_env)
        arcpy.AddMessage("Selected cemetery snapped")

        # ----------------------------
        # STEP 4: FeatureToLine (from selected copy)
        # ----------------------------
        arcpy.management.FeatureToLine(
            in_features=cem_sel_fc,
            out_feature_class=cem_line_fc,
            cluster_tolerance=None,
            attributes="ATTRIBUTES"
        )
        arcpy.AddMessage(f"Selected cemetery converted to line: {cem_line_fc}")

        # ----------------------------
        # STEP 5: AlignFeatures 
        # ----------------------------
        arcpy.edit.AlignFeatures(
            in_features=cem_line_fc,
            target_features=road,
            search_distance=dist_txt,
            match_fields=None
        )
        arcpy.AddMessage("Line cemetery aligned to extending layer")

        # ----------------------------
        # STEP 6: FeatureToPolygon
        # ----------------------------
        arcpy.management.FeatureToPolygon(
            in_features=cem_line_fc,
            out_feature_class=cem_poly_fc,
            cluster_tolerance=None,
            attributes="ATTRIBUTES",
            label_features=None
        )
        arcpy.AddMessage(f"Line converted to polygon: {cem_poly_fc}")

        # ----------------------------
        # STEP 7: Snap polygon again (vertex)
        # ----------------------------
        snap_env_poly = [
        [road, "VERTEX", dist_txt]
        ]

        arcpy.edit.Snap(cem_poly_fc, snap_env_poly)
        arcpy.AddMessage("Polygon snapped again to extending layer")

        # ----------------------------
        # STEP 8: SpatialJoin 
        # ----------------------------
        arcpy.analysis.SpatialJoin(
            target_features=cem_poly_fc,
            join_features=cem_sel_fc,  # join from selected original subset 
            out_feature_class=sj_fc,
            join_operation="JOIN_ONE_TO_ONE",
            join_type="KEEP_ALL",
            match_option="HAVE_THEIR_CENTER_IN",
            search_radius=None,
            distance_field_name="",
            match_fields=None
        )
        arcpy.AddMessage(f"Spatial join done: {sj_fc}")

        # Safety check (non-destructive): ensure the join actually matched
        # If Join_Count is 0 for any row, attributes can become NULL/Unknown.
        zero_join = 0
        with arcpy.da.SearchCursor(sj_fc, ["Join_Count"]) as cur:
            for (jc,) in cur:
                if jc == 0:
                    zero_join += 1
        arcpy.AddMessage(f"SpatialJoin Join_Count=0 rows: {zero_join}")

        # Now delete extra fields added by SpatialJoin
        arcpy.management.DeleteField(
            in_table=sj_fc,
            drop_field="Join_Count;TARGET_FID",
            method="DELETE_FIELDS"
        )
        arcpy.AddMessage("Extra fields deleted from SpatialJoin output")

        # ----------------------------
        # STEP 9: Delete matching features from WORKING cemetery copy (not the original)
        # This preserves your process, but avoids destructive edits on the real layer mid-run.
        # ----------------------------
        cem_work_lyr = arcpy.management.MakeFeatureLayer(cem_work_fc, f"{prefix}_cem_work_lyr")

        arcpy.management.SelectLayerByLocation(
            in_layer=cem_work_lyr,
            overlap_type="HAVE_THEIR_CENTER_IN",
            select_features=sj_fc,
            search_distance=None,
            selection_type="NEW_SELECTION",
            invert_spatial_relationship="NOT_INVERT"
        )
        del_count = int(arcpy.management.GetCount(cem_work_lyr)[0])
        arcpy.AddMessage(f"Features to delete from WORK copy: {del_count}")

        arcpy.management.DeleteFeatures(cem_work_lyr)
        arcpy.AddMessage("Deleted selected features from WORK copy")

        arcpy.management.SelectLayerByAttribute(
            in_layer_or_view=cem_work_lyr,
            selection_type="CLEAR_SELECTION"
        )
        arcpy.AddMessage("Selection cleared for WORK copy")

        # ----------------------------
        # STEP 10: Merge WORK remainder + SpatialJoin output 
        # ----------------------------
        arcpy.management.Merge(
            inputs=f"{cem_work_fc};{sj_fc}",
            output=merge_fc,
            field_mappings=None,
            add_source="NO_SOURCE_INFO",
            field_match_mode="USE_FIRST_SCHEMA"
        )
        arcpy.AddMessage(f"Merged into final output: {merge_fc}")

        # ----------------------------
        # STEP 11: Update the ORIGINAL cemetery layer at the very end, inside a transaction
        # If anything fails here, edits are rolled back.
        # ----------------------------
        arcpy.AddMessage("Updating original cemetery layer inside a single edit transaction...")

        editor = arcpy.da.Editor(gdb)
        editor.startEditing(False, True)  # False=not multiuser mode; True=with undo (works well for file gdb)
        editor.startOperation()

        try:
            # Delete all from original cemetery layer 
            arcpy.management.DeleteFeatures(cemetery)

            # Append merged output back to original cemetery layer
            arcpy.management.Append(
                inputs=merge_fc,
                target=cemetery,
                schema_type="NO_TEST",
                field_mapping=None,
                subtype="",
                expression="",
                match_fields=None,
                update_geometry="NOT_UPDATE_GEOMETRY",
                enforce_domains="NO_ENFORCE_DOMAINS",
                feature_service_mode="USE_FEATURE_SERVICE_MODE"
            )

            editor.stopOperation()
            editor.stopEditing(True)  # commit
            arcpy.AddMessage("Append completed. Process finished safely.")

        except Exception as ex:
            # Rollback in-transaction edits
            editor.abortOperation()
            editor.stopEditing(False)  # discard
            arcpy.AddMessage("ERROR occurred; all edits were rolled back. Original layer remains unchanged.")
            arcpy.AddMessage(f"Exception: {ex}")
            raise

        arcpy.AddMessage(f"Backup created at: {cem_backup_fc}")
        arcpy.AddMessage(f"Final merged output: {merge_fc}")

    else:
        arcpy.AddMessage(f"No cemetery features found within {dist_txt} meter of the road. Skipping the process and going to next step...")


def calculate_poly_angle(p1, p2, p3):
    v1 = (p1.X - p2.X, p1.Y - p2.Y)
    v2 = (p3.X - p2.X, p3.Y - p2.Y)

    dot = v1[0] * v2[0] + v1[1] * v2[1]
    mag1 = math.hypot(v1[0], v1[1])
    mag2 = math.hypot(v2[0], v2[1])

    cosang = dot / (mag1 * mag2)
    cosang = max(-1, min(1, cosang))
    return math.degrees(math.acos(cosang))


def count_true_angles(points, tolerance=5):
    """
    tolerance = degrees from 180 considered 'straight'
    """
    true_angles = 0

    for i in range(len(points)):
        p1 = points[i - 1]
        p2 = points[i]
        p3 = points[(i + 1) % len(points)]

        ang = calculate_poly_angle(p1, p2, p3)

        # ignore nearly straight angles
        if abs(ang - 180) > tolerance:
            true_angles += 1

    return true_angles


def get_polygon_oids_with_four_angles(fc):
    four_angle_oids = set()
    with arcpy.da.SearchCursor(fc, ["OID@", "SHAPE@"]) as cursor:
        for oid, geom in cursor:
            for part in geom:
                points = [p for p in part if p]

                # remove closing point
                if points[0].equals(points[-1]):
                    points = points[:-1]

                angle_count = count_true_angles(points)

                if angle_count == 4:
                    four_angle_oids.add(oid)
    four_angle_oids = f"({','.join(map(str, four_angle_oids))})"
    return four_angle_oids
# end function for identify_polygon_oids_with_four_angles


# function for enlarge polygon from all side
def get_edge_lengths(polygon):
    lengths = []
    for part in polygon:
        for i in range(len(part) - 1):
            p1 = part[i]
            p2 = part[i + 1]
            if p1 and p2:
                length = math.hypot(p2.X - p1.X, p2.Y - p1.Y)
                lengths.append(length)
    return lengths


def enlarge_polygon_side(fc, TARGET_LENGTH):
    with arcpy.da.UpdateCursor(fc, ["SHAPE@"]) as cursor:
        for row in cursor:
            geom = row[0]

            # Get all edge lengths
            edge_lengths = get_edge_lengths(geom)

            if not edge_lengths:
                continue

            shortest_edge = min(edge_lengths)
            # Only scale if under 35 meters
            if shortest_edge < TARGET_LENGTH:
                scale_ratio = TARGET_LENGTH / shortest_edge
                center = geom.centroid
                scaled_geom = geom.scale(center, scale_ratio, scale_ratio)
                row[0] = scaled_geom
                cursor.updateRow(row)
    arcpy.management.RepairGeometry(in_features=fc, delete_null="DELETE_NULL", validation_method="ESRI")


# end of function for enlarge polygon from all side

def main_enlarge_building_polygon_side(primary_polygon_fc, tolerance, working_gdb):
    simplify_building_polygons = arcpy.cartography.SimplifyBuilding(in_features=primary_polygon_fc,out_feature_class=rf"{working_gdb}\SimplifyBuild",simplification_tolerance=f"{tolerance} Meters",
                                                                    minimum_area="0 SquareMeters",conflict_option="NO_CHECK",in_barriers=None,collapsed_point_option="NO_KEEP")
    arcpy.management.DeleteFeatures(primary_polygon_fc)
    arcpy.management.Append(simplify_building_polygons, primary_polygon_fc, "NO_TEST")
    polygon_ids = get_polygon_oids_with_four_angles(primary_polygon_fc)
    if len(polygon_ids) != 0:
        # for exact 4 angles polygon
        polygon_with_four_angles=arcpy.management.MakeFeatureLayer(in_features=primary_polygon_fc, out_layer="polygon_with_four_angles", where_clause=f"OBJECTID IN {polygon_ids}")
        primary_fc_minimumbound = arcpy.management.MinimumBoundingGeometry(in_features=polygon_with_four_angles,out_feature_class=rf"{working_gdb}\Polygons_MinimumBoundi",
                                                                           geometry_type="RECTANGLE_BY_AREA",group_option="NONE", group_field=None,mbg_fields_option="NO_MBG_FIELDS")
        enlarge_polygon_side(primary_fc_minimumbound, tolerance)
        selected_primary_polygon_fc = arcpy.management.SelectLayerByAttribute(in_layer_or_view=primary_polygon_fc,selection_type="NEW_SELECTION",where_clause=f"OBJECTID IN {polygon_ids}",
                                                                              invert_where_clause=None)
        arcpy.management.DeleteFeatures(selected_primary_polygon_fc)
        arcpy.management.Append(primary_fc_minimumbound, primary_polygon_fc, "NO_TEST")

def terrace_buildings_to_builtup_area_(fc_list, building_fc_name, road_fc_name, built_up_fc_name, working_gdb, field_name="RET"):
    building_fc = resolve_fc_from_fc_list(building_fc_name, fc_list)
    road_fc = resolve_fc_from_fc_list(road_fc_name, fc_list)
    built_up_fc = resolve_fc_from_fc_list(built_up_fc_name, fc_list)
    terrace_fc = arcpy.management.SelectLayerByAttribute(building_fc, "NEW_SELECTION", f"{field_name} = 3")
    
    # Aggreate selected terrace houses
    aggregated_terrace_houses = arcpy.cartography.AggregatePolygons(in_features=terrace_fc, out_feature_class=f"{working_gdb}\\aggregated_terrace_houses", aggregation_distance="60 Meters", orthogonality_option="ORTHOGONAL")

    # Make feature layer
    area_field = arcpy.da.Describe(aggregated_terrace_houses)['areaFieldName']
    selected_aggregate_features = arcpy.conversion.ExportFeatures(aggregated_terrace_houses, f"{working_gdb}\\selected_aggregate_features", f"{area_field} > 8600")

    
    for row in arcpy.da.SearchCursor(selected_aggregate_features, ["SHAPE@", "OID@"]):
        geom = row[0]
        oid = row[1]
        terrace_fc = arcpy.management.SelectLayerByLocation(building_fc, "WITHIN", geom, None, "NEW_SELECTION")
        # Dissolved selected terrace house
        dissolved_terrace_house = arcpy.analysis.PairwiseDissolve(terrace_fc, f"{working_gdb}\\dissolved_terrace_house")
        # Select road feature 
        selected_road_fc = arcpy.management.SelectLayerByLocation(road_fc, "WITHIN_A_DISTANCE", dissolved_terrace_house, "25 Meters", "NEW_SELECTION")
        arcpy.AddMessage(f"Selected road features count: {count_features(selected_road_fc)}")
        # Dissolved selected road feature
        if count_features(selected_road_fc) > 0:
            arcpy.AddMessage(f"Object ID: {oid}")
            dissolved_road_feature = arcpy.analysis.PairwiseDissolve(selected_road_fc, f"{working_gdb}\\dissolved_road_feature_{oid}")
            # Buffer the dissolved road
            road_buffer_fc = arcpy.analysis.Buffer(dissolved_road_feature, f"{working_gdb}\\road_buffer_fc_{oid}", "50 Meters","FULL","FLAT","ALL", None,"PLANAR")
            # Convert polygon to line
            buffer_poly_to_line = arcpy.management.PolygonToLine(road_buffer_fc,f"{working_gdb}\\buffer_poly_to_line_{oid}", "IDENTIFY_NEIGHBORS")
            # Merge buffer polygon and dissolved road feature
            merged_road_fc = arcpy.management.Merge([dissolved_road_feature, buffer_poly_to_line], f"{working_gdb}\\merged_road_fc_{oid}")
            # Split the road feature
            splitted_road_fc = arcpy.management.SplitLine(merged_road_fc, f"{working_gdb}\\splitted_road_fc_{oid}")
            # Create convex hull polygon feature
            convex_hull_fc_road = arcpy.management.MinimumBoundingGeometry(dissolved_road_feature, f"{working_gdb}\\convex_hull_fc_road_{oid}", "CONVEX_HULL", "ALL", None, "NO_MBG_FIELDS")
            # Select splitted lines by convex hull
            selected_splitted_road_fc = arcpy.management.SelectLayerByLocation(splitted_road_fc, "INTERSECT", convex_hull_fc_road, None, "NEW_SELECTION")
            # Export the selected features
            exported_selected_split_features = arcpy.conversion.ExportFeatures(selected_splitted_road_fc, f"{working_gdb}\\exported_selected_split_features_{oid}")
            # Extend line to near line
            arcpy.edit.ExtendLine(exported_selected_split_features, "20 Meters", "EXTENSION")
            # Line to polygon
            split_line_fc_to_poly = arcpy.management.FeatureToPolygon(exported_selected_split_features, f"{working_gdb}\\split_line_fc_to_poly_{oid}", None, "ATTRIBUTES", None)
            # Snapping to remove gap
            arcpy.edit.Snap(exported_selected_split_features, [[split_line_fc_to_poly, "VERTEX", "60 Meters"]])
            # Convert line to polygon
            polygon_from_lines = arcpy.management.FeatureToPolygon(exported_selected_split_features, f"{working_gdb}\\polygon_from_lines_{oid}", None, "ATTRIBUTES", None)
            # Dissolved created polygon
            dissolved_polygon_from_lines = arcpy.analysis.PairwiseDissolve(polygon_from_lines, f"{working_gdb}\\dissolved_polygon_from_lines_{oid}")
            # Simplify polygon
            simplify_building = arcpy.cartography.SimplifyPolygon(dissolved_polygon_from_lines, f"{working_gdb}\\simplified_polygon_{oid}", "BEND_SIMPLIFY", "30 Meters")
            # Select buildings within simplified polygon
            selected_build_up_bldg = arcpy.management.SelectLayerByLocation(building_fc, "WITHIN", simplify_building, None, "NEW_SELECTION")
            # Delete selected buildings
            arcpy.management.DeleteFeatures(selected_build_up_bldg)
            # Append simplified polygon to built-up area
            arcpy.management.Append(simplify_building, built_up_fc, "NO_TEST")

def process_buildings_polygon_by_road_track_buffer(road_fc, road_class_field, track_fc, track_class_field, building_layers, duel_carriage_highway, single_carriage_highway, duel_carriage_road, 
                                           single_carriage_road, unsealed_road, road_under_construction, motorable_track, footpath, queried, query_expression, working_gdb):

    """
    This function processes building polygon layers by cutting and erasing overlapping portions with road and track buffers.
    """
    
    try:
        arcpy.env.overwriteOutput = True

        # ---------------------------------------------------
        # Temporary and output datasets
        # ---------------------------------------------------
        road_work = os.path.join(working_gdb, "Road_L_work")
        track_work = os.path.join(working_gdb, "Track_L_work")

        road_buffer = os.path.join(working_gdb, "Road_L_Category_Buffer")
        track_buffer = os.path.join(working_gdb, "Track_L_Category_Buffer")
        merged_buffer = os.path.join(working_gdb, "Road_Track_Merged_Buffer")

        for fc in [road_work, track_work, road_buffer, track_buffer, merged_buffer]:
            if arcpy.Exists(fc):
                arcpy.management.Delete(fc)

        # ---------------------------------------------------
        # Road buffer distance rules
        # ---------------------------------------------------
        road_buffer_distance = {
            1: ["Dual Carriage Highway", duel_carriage_highway],
            2: ["Single Carriage Highway", single_carriage_highway],
            3: ["Dual Carriage Road", duel_carriage_road],
            4: ["Single Carriage Road", single_carriage_road],
            5: ["Unsealed Road", unsealed_road],
            6: ["Road Under Construction", road_under_construction]
        }

        # ---------------------------------------------------
        # Track buffer distance rules
        # ---------------------------------------------------
        track_buffer_distance = {
            1: ["Motorable Track", motorable_track],
            2: ["Footpath", footpath]
        }

        # ---------------------------------------------------
        # Create road category-wise buffer
        # ---------------------------------------------------
        arcpy.AddMessage("Creating road category-wise buffer...")

        queried_value = str(queried).strip().lower()

        if queried_value not in ("yes", "no"):
            raise ValueError(
                "The queried parameter must be either 'Yes' or 'No'."
            )

        if queried_value == "yes":

            # Confirm that the Invisibility field exists
            road_field_names = {
                field.name.lower(): field.name
                for field in arcpy.ListFields(road_fc)
            }

            if "invisibility" not in road_field_names:
                raise ValueError(
                    "The road feature class does not contain an 'Invisibility' field."
                )

            arcpy.AddMessage(
                f"Queried = {queried_value}. Using road features where '{query_expression}'"
            )
            
            road_query_layer = "road_query_layer"
            arcpy.management.MakeFeatureLayer(
                in_features=road_fc,
                out_layer=road_query_layer,
                where_clause=query_expression
            )

            filtered_road_count = int(
                arcpy.management.GetCount(
                    road_query_layer
                )[0]
            )

            arcpy.AddMessage(
                f"Road features passing the query: {filtered_road_count}"
            )

            arcpy.management.CopyFeatures(
                road_query_layer,
                road_work
            )

            arcpy.management.Delete(road_query_layer)

        else:
            arcpy.AddMessage(
                "Queried = No. Using all road features."
            )

            arcpy.management.CopyFeatures(
                road_fc,
                road_work
            )

        arcpy.management.AddField(road_work, "BUFF_DIST", "TEXT", field_length=30)

        with arcpy.da.UpdateCursor(
            road_work,
            [road_class_field, "BUFF_DIST"]
        ) as cursor:

            for rcs, buff_dist in cursor:
                try:
                    rcs_code = int(rcs)

                    if rcs_code in road_buffer_distance:
                        road_class_name = road_buffer_distance[rcs_code][0]
                        road_distance = road_buffer_distance[rcs_code][1]

                        cursor.updateRow([
                            rcs,
                            f"{road_distance} Meters"
                        ])
                    else:
                        cursor.updateRow([rcs, None])

                except Exception:
                    cursor.updateRow([rcs, None])

        road_layer = "road_layer_with_buffer"

        if arcpy.Exists(road_layer):
            arcpy.management.Delete(road_layer)

        arcpy.management.MakeFeatureLayer(
            road_work,
            road_layer,
            "BUFF_DIST IS NOT NULL"
        )

        arcpy.analysis.PairwiseBuffer(
            in_features=road_layer,
            out_feature_class=road_buffer,
            buffer_distance_or_field="BUFF_DIST",
            dissolve_option="LIST",
            dissolve_field=[road_class_field, "BUFF_DIST"],
            method="PLANAR"
        )

        arcpy.AddMessage(f"Road buffer created: {road_buffer}")

        # ---------------------------------------------------
        # Create track category-wise buffer
        # ---------------------------------------------------
        arcpy.AddMessage("Creating track category-wise buffer...")

        queried_value = str(queried).strip().lower()

        if queried_value not in ("yes", "no"):
            raise ValueError(
                "The queried parameter must be either 'Yes' or 'No'."
            )

        if queried_value == "yes":

            # Confirm that the Invisibility field exists
            track_field_names = {
                field.name.lower(): field.name
                for field in arcpy.ListFields(track_fc)
            }

            if "invisibility" not in track_field_names:
                raise ValueError(
                    "The track feature class does not contain an 'Invisibility' field."
                )

            arcpy.AddMessage(
                f"Queried = {queried_value}. Using road features where '{query_expression}'"
            )

            track_query_layer = "track_query_layer"
            arcpy.management.MakeFeatureLayer(
                in_features=track_fc,
                out_layer="track_query_layer",
                where_clause=query_expression
            )

            filtered_track_count = int(
                arcpy.management.GetCount(
                    track_query_layer
                )[0]
            )

            arcpy.AddMessage(
                f"Track features passing the query: {filtered_track_count}"
            )

            arcpy.management.CopyFeatures(
                track_query_layer,
                track_work
            )

            arcpy.management.Delete(track_query_layer)

        else:
            arcpy.AddMessage(
                "Queried = No. Using all track features."
            )

            arcpy.management.CopyFeatures(
                track_fc,
                track_work
            )

        arcpy.management.AddField(track_work, "BUFF_DIST", "TEXT", field_length=30)

        with arcpy.da.UpdateCursor(
            track_work,
            [track_class_field, "BUFF_DIST"]
        ) as cursor:

            for tcs, buff_dist in cursor:
                try:
                    tcs_code = int(tcs)

                    if tcs_code in track_buffer_distance:
                        track_class_name = track_buffer_distance[tcs_code][0]
                        track_distance = track_buffer_distance[tcs_code][1]

                        cursor.updateRow([
                            tcs,
                            f"{track_distance} Meters"
                        ])
                    else:
                        cursor.updateRow([tcs, None])

                except Exception:
                    cursor.updateRow([tcs, None])

        track_layer = "track_layer_with_buffer"

        if arcpy.Exists(track_layer):
            arcpy.management.Delete(track_layer)

        arcpy.management.MakeFeatureLayer(
            track_work,
            track_layer,
            "BUFF_DIST IS NOT NULL"
        )

        arcpy.analysis.PairwiseBuffer(
            in_features=track_layer,
            out_feature_class=track_buffer,
            buffer_distance_or_field="BUFF_DIST",
            dissolve_option="LIST",
            dissolve_field=[track_class_field, "BUFF_DIST"],
            method="PLANAR"
        )

        arcpy.AddMessage(f"Track buffer created: {track_buffer}")

        # ---------------------------------------------------
        # Merge road buffer and track buffer
        # ---------------------------------------------------
        arcpy.AddMessage("Merging road and track buffers...")

        arcpy.management.Merge(
            inputs=[road_buffer, track_buffer],
            output=merged_buffer
        )

        arcpy.AddMessage(f"Merged road-track buffer created: {merged_buffer}")

        # ---------------------------------------------------
        # Process all building layers using merged buffer
        # ---------------------------------------------------
        for building_fc in building_layers:
            try:
                if has_features(building_fc):
                    arcpy.AddMessage(f"\nProcessing building layer: {building_fc}")

                    base_name = arcpy.ValidateTableName(
                        arcpy.Describe(building_fc).baseName,
                        working_gdb
                    )

                    building_layer = f"{base_name}_lyr"
                    final_layer = f"{base_name}_final_layer"

                    building_work = os.path.join(working_gdb, f"{base_name}_work")
                    building_erased = os.path.join(working_gdb, f"{base_name}_erased")
                    building_singlepart = os.path.join(working_gdb, f"{base_name}_singlepart")

                    final_output = os.path.join(
                        working_gdb,
                        f"{base_name}_Road_Track_Buffer_Removed"
                    )

                    for fc in [
                        building_layer,
                        final_layer,
                        building_work,
                        building_erased,
                        building_singlepart,
                        final_output
                    ]:
                        if arcpy.Exists(fc):
                            arcpy.management.Delete(fc)

                    # ---------------------------------------------------
                    # Select only buildings intersecting merged buffer
                    # ---------------------------------------------------
                    arcpy.management.MakeFeatureLayer(building_fc, building_layer)

                    arcpy.management.SelectLayerByLocation(
                        in_layer=building_layer,
                        overlap_type="INTERSECT",
                        select_features=merged_buffer,
                        selection_type="NEW_SELECTION"
                    )

                    selected_count = int(arcpy.management.GetCount(building_layer)[0])

                    if selected_count == 0:
                        arcpy.AddMessage(f"No intersecting buildings found in {building_fc}. Skipping.")
                        continue

                    arcpy.AddMessage(f"Selected buildings: {selected_count}")

                    # Copy selected buildings only
                    arcpy.management.CopyFeatures(building_layer, building_work)

                    # ---------------------------------------------------
                    # Add original building ID
                    # ---------------------------------------------------
                    if "ORIG_BLDG_ID" in [f.name for f in arcpy.ListFields(building_work)]:
                        arcpy.management.DeleteField(building_work, ["ORIG_BLDG_ID"])

                    arcpy.management.AddField(building_work, "ORIG_BLDG_ID", "LONG")

                    oid_field = arcpy.Describe(building_work).OIDFieldName

                    arcpy.management.CalculateField(
                        building_work,
                        "ORIG_BLDG_ID",
                        f"!{oid_field}!",
                        "PYTHON3"
                    )

                    # ---------------------------------------------------
                    # Erase building using merged road-track buffer
                    # ---------------------------------------------------
                    arcpy.analysis.PairwiseErase(
                        in_features=building_work,
                        erase_features=merged_buffer,
                        out_feature_class=building_erased
                    )

                    erased_count = int(arcpy.management.GetCount(building_erased)[0])

                    if erased_count == 0:
                        arcpy.AddMessage("All selected buildings were fully removed by buffer.")

                        arcpy.management.DeleteFeatures(building_layer)

                        arcpy.AddMessage(f"Completed Final: {building_fc}")
                        continue

                    # ---------------------------------------------------
                    # Convert multipart to singlepart
                    # ---------------------------------------------------
                    arcpy.management.MultipartToSinglepart(
                        building_erased,
                        building_singlepart
                    )

                    # ---------------------------------------------------
                    # Keep only largest part for each original building
                    # ---------------------------------------------------
                    if "KEEP_PART" in [f.name for f in arcpy.ListFields(building_singlepart)]:
                        arcpy.management.DeleteField(building_singlepart, ["KEEP_PART"])

                    arcpy.management.AddField(building_singlepart, "KEEP_PART", "SHORT")

                    oid_field = arcpy.Describe(building_singlepart).OIDFieldName

                    largest_part = {}

                    with arcpy.da.SearchCursor(
                        building_singlepart,
                        [oid_field, "ORIG_BLDG_ID", "SHAPE@AREA"]
                    ) as cursor:

                        for oid, orig_id, area in cursor:
                            if orig_id not in largest_part:
                                largest_part[orig_id] = [oid, area]
                            elif area > largest_part[orig_id][1]:
                                largest_part[orig_id] = [oid, area]

                    largest_oids = [value[0] for value in largest_part.values()]

                    with arcpy.da.UpdateCursor(
                        building_singlepart,
                        [oid_field, "KEEP_PART"]
                    ) as cursor:

                        for oid, keep in cursor:
                            if oid in largest_oids:
                                cursor.updateRow([oid, 1])
                            else:
                                cursor.updateRow([oid, 0])

                    arcpy.management.MakeFeatureLayer(
                        building_singlepart,
                        final_layer,
                        "KEEP_PART = 1"
                    )

                    arcpy.management.CopyFeatures(
                        final_layer,
                        final_output
                    )

                    # ---------------------------------------------------
                    # Delete helper fields only if they exist
                    # ---------------------------------------------------
                    fields_to_delete = ["KEEP_PART", "ORIG_BLDG_ID"]
                    existing_fields = [field.name for field in arcpy.ListFields(final_output)]

                    delete_fields = [
                        field_name for field_name in fields_to_delete
                        if field_name in existing_fields
                    ]

                    if delete_fields:
                        arcpy.management.DeleteField(final_output, delete_fields)

                    # ---------------------------------------------------
                    # Delete original overlapping buildings
                    # Then append corrected buildings back
                    # ---------------------------------------------------
                    arcpy.management.SelectLayerByLocation(
                        in_layer=building_layer,
                        overlap_type="INTERSECT",
                        select_features=merged_buffer,
                        selection_type="NEW_SELECTION"
                    )

                    arcpy.management.DeleteFeatures(building_layer)

                    arcpy.management.Append(
                        inputs=final_output,
                        target=building_fc,
                        schema_type="NO_TEST"
                    )

                    #Delete temporary layers and feature classes
                    for fc in [
                        building_layer,
                        final_layer,
                        building_work,
                        building_erased,
                        building_singlepart,
                        final_output
                    ]:
                        if arcpy.Exists(fc):
                            arcpy.management.Delete(fc)

                    arcpy.AddMessage(f"Completed Final: {building_fc}")

                else:
                    arcpy.AddMessage(f"No features found in building layer: {building_fc}. Skipping.")

            except Exception as building_error:
                arcpy.AddError(f"Error while processing building layer: {building_fc}")
                arcpy.AddError(building_error)
                arcpy.AddError(traceback.format_exc())
                continue

        arcpy.AddMessage("\nAll building layers processed successfully.")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Generalised building error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)



# # Builtup Generalization
def gen_buildup(fc_list, cut_build_a_inputs_poly, small_bldg_2_point_a, small_bldg_2_point_p, working_gdb, features_in_cemetery, enlarge_barrier_fcs, delete_small_bldgs,  enlarge_building_features,   
                  in_buildings_list, edge_features_list, in_feature_loc, delete_small_features, val_dict, logger, map_name):
    arcpy.AddMessage('Starting buildup features generalization.....')
    # Set the workspace
    arcpy.env.overwriteOutput = True
    dynamic_fc_names = resolve_lyr()
    try:
        # Cut building polygons by road and track buffers by category
        road_fc = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        track_fc = [fc for fc in fc_list if dynamic_fc_names.Track_L in fc][0]
        cut_build_a_inputs_poly = list(filter(str.strip, cut_build_a_inputs_poly))
        cut_build_a_inputs_poly = [fc for a_lyr in cut_build_a_inputs_poly for fc in fc_list if str(a_lyr) in fc]
        process_buildings_polygon_by_road_track_buffer(road_fc, val_dict['Built_road_class_field'], track_fc, val_dict['Built_track_class_field'], cut_build_a_inputs_poly, val_dict['Built_road_duel_carriage_highway'], val_dict['Built_road_single_carriage_highway'], val_dict['Built_road_duel_carriage_road'], 
                                           val_dict['Built_road_single_carriage_road'], val_dict['Built_road_unsealed_road'], val_dict['Built_road_road_under_construction'], val_dict['Built_track_motorable_track'], val_dict['Built_track_footpath'], val_dict['Built_road_used_by_invisibility_query'], val_dict['Built_invisibility_query'], working_gdb)
        
        # Delete small buildings
        delete_small_building(fc_list, delete_small_bldgs, val_dict['Built_del_min_area'])
        
        # Delete buildings in Cemetery
        features_in_cemetery = list(filter(str.strip, features_in_cemetery))
        features_in_cemetery = [fc for a_lyr in features_in_cemetery for fc in fc_list if str(a_lyr) in fc]
        cemetery = [fc for fc in fc_list if dynamic_fc_names.Cemetery_A in fc][0]
        delete_features_in_poly(features_in_cemetery, cemetery, val_dict['Built_min_size_bldg2'])
        
        # Convert small building to point
        convert_small_bldg_2_point(fc_list, small_bldg_2_point_a, small_bldg_2_point_p, val_dict['Built_min_size_bldg1'], val_dict['Built_delete_input'], 
                                   val_dict['Built_create_one_point'], val_dict['Built_unique_field'], working_gdb)
        
        # Enlarge builtup Features (Cemetery)
        enlarge_barrier_fcs = list(filter(str.strip, enlarge_barrier_fcs))
        enlarge_barrier_fcs = [fc for a_lyr in enlarge_barrier_fcs for fc in fc_list if str(a_lyr) in fc]
        enlarge_polygon_barrier(cemetery, None, None, val_dict['Built_enlarge_min_size'], val_dict['Built_enlarge_val'], enlarge_barrier_fcs, working_gdb)

        #Enlarge cemetery features to road and river
        road = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        river = [fc for fc in fc_list if dynamic_fc_names.River_Bank_L in fc][0]

        extend_cemetery_with_road_river(cemetery, road, working_gdb)
        extend_cemetery_with_road_river(cemetery, river, working_gdb)

        # Enlarge small buildings
        enlarge_building_features = list(filter(str.strip, enlarge_building_features))
        enlarge_building_features = [fc for a_lyr in enlarge_building_features for fc in fc_list if str(a_lyr) in fc]
        #extend_polygon_sides(enlarge_building_features, working_gdb, enlarge_bldg_min_width, enlarge_bldg_min_length, enlarge_bldg_additional_criteria, simplification_tolerance)
        # Simplify buildings
        for polygon_fc in enlarge_building_features:
            if has_features(polygon_fc):
                simplify_buildings(polygon_fc, val_dict['Built_simpl_bldg_distance'], working_gdb)

       

        current_working_dir = os.getcwd()
        dlpk_path = os.path.join(current_working_dir, "building_ft_identifier_v1.dlpk")
        road_fc = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        Residential_Building_A = [fc for fc in fc_list if dynamic_fc_names.Residential_Building_A in fc][0]
        Town_Built_up_A = [fc for fc in fc_list if dynamic_fc_names.Town_Built_up_A in fc][0]
        Residential_Building_P = [fc for fc in fc_list if dynamic_fc_names.Residential_Building_P in fc][0]

        arcpy.AddMessage(f"{val_dict['Built_townbuiltup_generator_mode']} Started")
        delineate_built_up_area(fc_list, in_buildings_list, edge_features_list, val_dict['Built_delineate_grp_dist'], 
                                val_dict['Built_delineate_min_detail_size'], val_dict['Built_delineate_min_bldg_count'], 
                                working_gdb, val_dict['Built_delineate_ref_scale'], val_dict['Built_townbuiltup_min_area'])
        area_based_delete(Town_Built_up_A, val_dict['Built_townbuiltup_min_area'])
      
        
        # Generalised Buildings
        generalised_buildings(fc_list, val_dict['Built_general_min_area'])
        
        # Delete small features (Swimming)
        recreation = [fc for fc in fc_list if dynamic_fc_names.Swimming_Pool_A in fc][0] ## edited
        delete_small_features = list(filter(str.strip, delete_small_features))
        delete_small_features = [fc for a_lyr in delete_small_features for fc in fc_list if str(a_lyr) in fc]
        remove_by_converting(recreation, delete_small_features, val_dict['Built_del_small_recreation_min_size'], None, working_gdb)
        # Erase vagetaton
        erase_polygons_by_replace(cemetery, delete_small_features, val_dict['Built_erase_sql'], working_gdb)
        pond = [fc for fc in fc_list if dynamic_fc_names.Pond_A in fc][0]
        fence = [fc for fc in fc_list if dynamic_fc_names.Fence_L in fc][0]
        lake = [fc for fc in fc_list if dynamic_fc_names.Lake_A in fc][0]
    
        # Fix Conflict Between Fence and Road / Track
        road_fc = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc][0]
        track_fc = [fc for fc in fc_list if dynamic_fc_names.Track_L in fc][0]
        # # Fence Feature Class
        fence_fc = [fc for fc in fc_list if dynamic_fc_names.Fence_L in fc][0]
        # # Wall Feature Class
        wall_fc = [fc for fc in fc_list if dynamic_fc_names.Wall_L in fc][0]

        fence_road_distance_rules={1: 77.8, 2: 67.8, 3: 77.8, 4: 67.8, 5: 62.8, 6: 65.3}
        fence_track_distance_rules={1: 36.3, 2: 31.3}
        fix_wall_fence_conflict_with_road(road_fc, track_fc, fence_fc, fence_road_distance_rules, fence_track_distance_rules, working_gdb, logger,  
                                          val_dict['Built_fix_wall_fence_road_class_field'], val_dict['Built_fix_wall_fence_track_class_field'])

        wall_road_distance_rules={1: 82.8, 2: 72.8, 3: 82.8, 4: 72.8, 5: 67.8, 6: 70.3}
        wall_track_distance_rules={1: 41.3, 2: 36.3}
        fix_wall_fence_conflict_with_road(road_fc, track_fc, wall_fc, wall_road_distance_rules, wall_track_distance_rules, working_gdb, logger, 
                                          val_dict['Built_fix_wall_fence_road_class_field'], val_dict['Built_fix_wall_fence_track_class_field'])
        # # Merge Buildings that are too close between Buildings and Street
        merge_buildings_too_closed_between_building_and_street(dynamic_fc_names.Road_L, dynamic_fc_names.Residential_Building_A, fc_list, working_gdb, logger)
        # # Align State Boundary in Reference to River Bank Line 
        align_feature_with_reference_fc(dynamic_fc_names.State_Coverage_L, dynamic_fc_names.River_Bank_L, fc_list, working_gdb, logger)
        # Move Buildings in accordance with Historical Sites
        move_feature_1_around_feature_2_to_specific_distance(fc_list, 
                                                             [dynamic_fc_names.Historical_Site_A], 
                                                             [dynamic_fc_names.Residential_Building_A, dynamic_fc_names.Residential_Building_P], 
                                                             working_gdb, logger, min_distance = val_dict['Built_move_feature_around_feature_minimum_distance'])
        residential_building_polygon = [fc for fc in fc_list if dynamic_fc_names.Residential_Building_A in fc][0]
        residential_building_side_tolerance = 35
        
        if has_features(residential_building_polygon):
            main_enlarge_building_polygon_side(residential_building_polygon, residential_building_side_tolerance, working_gdb)

        # Cut simplified building polygons again by road and track buffers by category
        arcpy.AddMessage("Cutting simplified building polygons again by road and track buffers...")
        process_buildings_polygon_by_road_track_buffer(road_fc, val_dict['Built_road_class_field'], track_fc, val_dict['Built_track_class_field'], cut_build_a_inputs_poly, val_dict['Built_road_duel_carriage_highway'], val_dict['Built_road_single_carriage_highway'], val_dict['Built_road_duel_carriage_road'], 
                                           val_dict['Built_road_single_carriage_road'], val_dict['Built_road_unsealed_road'], val_dict['Built_road_road_under_construction'], val_dict['Built_track_motorable_track'], val_dict['Built_track_footpath'], val_dict['Built_road_used_by_invisibility_query'], val_dict['Built_invisibility_query'], working_gdb)

        # Delete small buildings again after simplification and cutting by road and track buffers
        delete_small_building(fc_list, delete_small_bldgs, val_dict['Built_del_min_area'])

        # Convert small building to point again after simplification and cutting by road and track buffers
        convert_small_bldg_2_point(fc_list, small_bldg_2_point_a, small_bldg_2_point_p, val_dict['Built_min_size_bldg1'], val_dict['Built_delete_input'], 
                                   val_dict['Built_create_one_point'], val_dict['Built_unique_field'], working_gdb)
        
        #Apply defrinition query to building_P layers for hiding building points that doesn't have any name. 
        apply_layer_definition (small_bldg_2_point_p, val_dict['Builtup_Apply_Layer_Definition_expression'] , map_name)

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Built-Up Area Generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Built-Up Area Generalisation', f'{exc_value}\n')