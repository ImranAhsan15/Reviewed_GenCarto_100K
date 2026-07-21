import arcpy
import traceback
import sys
import time
from common_utils import *

def fix_veg_after_resolve_conflict(fc_list, input_primary, input_secondary, in_prim_sql, max_gap_area, fill_option, invisibility_field, working_gdb, logger):
    dynamic_fc_names = resolve_lyr()
    try:
        # Get feature classes
        input_primary = list(filter(str.strip, input_primary))
        input_primary = [fc for in_prim in input_primary for fc in fc_list if str(in_prim) in fc]
        input_secondary = list(filter(str.strip, input_secondary))
        input_secondary = [fc for in_second in input_secondary for fc in fc_list if str(in_second) in fc]
        river_coverage_A = [fc for fc in fc_list if dynamic_fc_names.River_Coverage_A in fc][0]
        logger.info(f"Fix vegetation: {len(input_primary)} primary and {len(input_secondary)} secondary feature classes")

        # Convert polygons
        for in_p in input_primary:
            logger.info(f"Fix vegetation: converting polygons for {os.path.basename(in_p)}")
            convert_polygon(in_p, input_secondary, max_gap_area, in_prim_sql, working_gdb)
        # Erase features and Fill gaps
        input_primary.append(river_coverage_A)
        logger.info("Fix vegetation: erasing secondary features under water polygons and filling gaps")
        erase_features(input_primary, input_secondary, working_gdb, max_gap_area, fill_option, invisibility_field)
        logger.info("Fix vegetation: erase and fill gaps completed")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Fix vegetation after resolving conflicts error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        raise

def resolve_building_point_road_track_conflict(building_fcs, logger, road_fc=None, track_fc=None):
    if road_fc is None or track_fc is None:
        dynamic_fc_names = resolve_lyr()
        if road_fc is None:
            road_fc = dynamic_fc_names.Road_L
        if track_fc is None:
            track_fc = dynamic_fc_names.Track_L
    arcpy.env.overwriteOutput = True
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    active_map = aprx.activeMap
    active_layers = active_map.listLayers()
    building_layers = [lyr for bfc in building_fcs for lyr in active_layers if lyr.name in bfc]
    road_layer = [lyr for lyr in active_layers if os.path.basename(road_fc) in lyr.name][0]
    track_layer = [lyr for lyr in active_layers if os.path.basename(track_fc) in lyr.name][0]
    
    resolve_rules = [
        (track_layer, "TCS = 1", "42.5 Meters"),
        (track_layer, "TCS = 2", "37.5 Meters"),
        (road_layer, "RCS = 1", "57.5 Meters"),
        (road_layer, "RCS = 2", "47.5 Meters"),
        (road_layer, "RCS = 3", "57.5 Meters"),
        (road_layer, "RCS = 4", "47.5 Meters"),
        (road_layer, "RCS = 5", "42.5 Meters"),
        (road_layer, "RCS = 6", "45 Meters")
        ]

    for src, query, buffer_dist in resolve_rules:
        # logger.info(f"Processing: {layer} | {query} | Buffer={buffer_dist}")
        # 1. Create filtered layer
        # arcpy.management.MakeFeatureLayer(src, layer, query)
        selected_features = arcpy.management.SelectLayerByAttribute(src, "NEW_SELECTION", query)
        logger.info(f"Resolving building conflict for {src} where {query}")
        ##  2. Resolve building conflicts
        arcpy.cartography.ResolveBuildingConflicts(
            in_buildings=building_layers,
            invisibility_field="INVISIBILITY",
            # in_barriers=f"{layer} TRUE '{buffer_dist}'",
            in_barriers=[[f"{selected_features}", "true", buffer_dist]],
            building_gap="12.5 Meters",
            minimum_size="10 Meters",
            hierarchy_field=""
        )
        arcpy.management.SelectLayerByAttribute(src, "CLEAR_SELECTION")

def resolve_building_polygon_road_track_conflict(building_fcs, logger, road_fc=None, track_fc=None):
    if road_fc is None or track_fc is None:
        dynamic_fc_names = resolve_lyr()
        if road_fc is None:
            road_fc = dynamic_fc_names.Road_L
        if track_fc is None:
            track_fc = dynamic_fc_names.Track_L
    arcpy.env.overwriteOutput = True
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    active_map = aprx.activeMap
    active_layers = active_map.listLayers()
    building_layers = [lyr for bfc in building_fcs for lyr in active_layers if lyr.name in bfc]
    road_layer = [lyr for lyr in active_layers if os.path.basename(road_fc) in lyr.name][0]
    track_layer = [lyr for lyr in active_layers if os.path.basename(track_fc) in lyr.name][0]
    
    resolve_rules = [
            (track_layer, "TCS = 1", "12.5 Meters"),
            (track_layer, "TCS = 2", "12.5 Meters"),
            (road_layer, "RCS = 1",  "12.5 Meters"),
            (road_layer, "RCS = 2",  "12.5 Meters"),
            (road_layer, "RCS = 3",  "12.5 Meters"),
            (road_layer, "RCS = 4",  "12.5 Meters"),
            (road_layer, "RCS = 5",  "12.5 Meters"),
            (road_layer, "RCS = 6",  "12.5 Meters")
        ]

    for src, query, buffer_dist in resolve_rules:
        # logger.info(f"Processing: {layer} | {query} | Buffer={buffer_dist}")
        # 1. Create filtered layer
        # arcpy.management.MakeFeatureLayer(src, layer, query)
        selected_features = arcpy.management.SelectLayerByAttribute(src, "NEW_SELECTION", query)
        logger.info(f"Resolving building conflict for {src} where {query}")
        ##  2. Resolve building conflicts
        arcpy.cartography.ResolveBuildingConflicts(
            in_buildings=building_layers,
            invisibility_field="INVISIBILITY",
            # in_barriers=f"{layer} TRUE '{buffer_dist}'",
            in_barriers=[[f"{selected_features}", "true", buffer_dist]],
            building_gap="12.5 Meters",
            minimum_size="10 Meters",
            hierarchy_field=""
        )
        arcpy.management.SelectLayerByAttribute(src, "CLEAR_SELECTION")

def move_building_near_adjacent_points(fc_list, map_name, logger, working_gdb, dist_mx, dist_mn, invisi_field, bld_gap, min_size, hier_field, building_features = None, stated_point_features_dict_with_distance = None):
    arcpy.AddMessage(f"Starting moving buildings in respect to stated points...")
    dynamic_fc_names = resolve_lyr()
    valid_barriers = []
    point_buffered_features = {}
    arcpy.env.workspace = working_gdb
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    map = aprx.listMaps(map_name)[0]
    active_layers = map.listLayers()
    active_layers = [lyr for lyr in active_layers if not lyr.isGroupLayer]
    # arcpy.AddMessage(f"fc_list:  {fc_list}")
    building_layers = []
    
    if(not building_features):
        building_features = [dynamic_fc_names.Residential_Building_A, dynamic_fc_names.Residential_Building_P]
        for bfc in building_features:
            for lyr in active_layers:
               # arcpy.AddMessage(f"active_layer : {lyr.name}")
               if bfc == lyr.name:
                   building_layers.append(lyr)
    if(not stated_point_features_dict_with_distance):
        stated_point_features_dict_with_distance = {
            dynamic_fc_names.Natural_Spring_A: f'{dist_mx} Meters',
            dynamic_fc_names.Natural_Spring_P: f'{dist_mx} Meters',
            dynamic_fc_names.Building_Of_Worship_A: f'{dist_mx} Meters',
            dynamic_fc_names.Building_Of_Worship_P: f'{dist_mx} Meters',
            dynamic_fc_names.Global_Navigation_Satellite_System_Station_P: f'{dist_mx} Meters',
            dynamic_fc_names.Trigonometry_Station_P: f'{dist_mx} Meters',
            dynamic_fc_names.Height_Point_P: f'{dist_mn} Meters',
            dynamic_fc_names.Base_Point_P: f'{dist_mn} Meters',
            dynamic_fc_names.International_Boundary_Marker_P: f'{dist_mx} Meters',
            dynamic_fc_names.State_Boundary_Marker_P: f'{dist_mx} Meters'
        }

    for feature, distance in stated_point_features_dict_with_distance.items():
        if feature in [lyr.name for lyr in active_layers ]:
            if feature in [ os.path.basename(fc) for fc in fc_list]:
                feature_class = [fc for fc in fc_list if feature == os.path.basename(fc)][0]
                feature_path = os.path.join(arcpy.env.workspace, feature)
                # arcpy.AddMessage(f"feature_class:  {feature_class}")
                if feature.endswith('_P'):
                    if has_features(feature_class):
                        arcpy.management.MakeFeatureLayer(feature_class, feature)
                        buffered_feature = os.path.join(arcpy.env.workspace, f"{feature}_Buffer")
                        arcpy.analysis.PairwiseBuffer(
                            in_features=feature_class,
                            out_feature_class=buffered_feature,
                            buffer_distance_or_field="0.5 Meters",
                            dissolve_option="NONE",
                            dissolve_field=None,
                            method="PLANAR"
                        )
                        point_buffered_features[feature] = buffered_feature
                        arcpy.management.MakeFeatureLayer(buffered_feature, f"{feature}_Buffer")
                        valid_barriers.append(f"{feature}_Buffer TRUE '{distance}'")
                else:
                    if has_features(feature_class):
                        for lyr in active_layers:
                            if feature == lyr.name:
                                # arcpy.management.MakeFeatureLayer(feature_class, feature)
                                valid_barriers.append(f"{lyr.name} TRUE '{distance}'")
                    else:
                        logger.warning(f"Polygon feature '{feature}' has no records and will be skipped.")

    if valid_barriers:
        in_barriers = ";".join(valid_barriers)
        arcpy.cartography.ResolveBuildingConflicts(
            in_buildings=building_layers, 
            invisibility_field=invisi_field,
            in_barriers=in_barriers,
            building_gap=f"{bld_gap} Meters",
            minimum_size=f"{min_size} Meters",
            hierarchy_field=hier_field
        )
        
    else:
        logger.warning("No valid barriers were found to resolve conflicts.")

    return None

# # Resolving Conflict for Polygons
def resolve_conflict_polygons(fc_list, build_up_area_fcs, input_building_layers, input_barrier_layers, symbology_file_path, g1_align_features,
                               g4_align_features, g5_input_points, g5_align_features, g6_align_features, g7_input_points, g7_align_features, input_primary, input_secondary, working_gdb, map_name, log_dir, val_dict, df_query_input_lyr_9b, logger, carto_partition=None):
    arcpy.AddMessage('Starting resolve conflicts for polygons.....')
    dynamic_fc_names = resolve_lyr()
    try:
        # Licenses are verified centrally in main.py (check_required_licenses)
        # before the theme starts.

        # Close open map views so Pro does not re-render the map after every edit
        close_active_map_views(logger)

        # Process the cartographic tools (ResolveBuildingConflicts,
        # AlignMarkerToStrokeOrFill) partition by partition. Without this the
        # tools load the symbolized graphics of the whole dataset into memory
        # at once, which is what makes long runs crash.
        if carto_partition and arcpy.Exists(carto_partition):
            arcpy.env.cartographicPartitions = carto_partition
            arcpy.AddMessage(f"Cartographic partitions enabled: {carto_partition}")
            logger.info(f"Cartographic partitions enabled: {carto_partition}")
        else:
            arcpy.AddWarning("CartoPartitionA not found; cartographic tools will run unpartitioned over the full dataset.")
            logger.warning("CartoPartitionA not found; cartographic tools will run unpartitioned over the full dataset.")

        step_start = time.time()
        def start_step(step_name):
            nonlocal step_start
            step_start = time.time()
            arcpy.AddMessage(f"[9b] Starting: {step_name}")
            logger.info(f"[9b] Starting: {step_name}")
        def log_step(step_name):
            nonlocal step_start
            elapsed = time.time() - step_start
            arcpy.AddMessage(f"[9b timing] {step_name}: {elapsed:.1f} s")
            logger.info(f"[9b timing] {step_name}: {elapsed:.1f} s")
            step_start = time.time()

        # # Hide buildings under built up area
        start_step("Hide buildings under built-up area")
        hide_blgs_under_built_up_area(fc_list, build_up_area_fcs, val_dict['Resolve_conflict_build_express_val_mx'], val_dict['Resolve_conflict_build_express_val_mn'], val_dict['Resolve_conflict_build_visible_field'], val_dict['Resolve_conflict_build_search_distance'], val_dict['Resolve_conflict_build_query'], map_name)
        log_step("Hide buildings under built-up area")

        # Resolve conflicts for point and polygon
        start_step("Resolve building conflicts")
        resolve_conflicts_points_polygon(fc_list, input_building_layers, input_barrier_layers, val_dict['Resolve_conflict_build_bb_lyr_ex'], val_dict['Resolve_conflict_build_bb_lyr_ex_his'], val_dict['Resolve_conflict_build_hierarchy_field'], val_dict['Resolve_conflict_build_visible_field'], symbology_file_path, val_dict['Resolve_conflict_build_ref_scale'],
                                        val_dict['Resolve_conflict_build_minimum_size'], val_dict['Resolve_conflict_build_building_gap'], working_gdb, map_name)
        log_step("Resolve building conflicts")
        # Align points-G1 -- Group 1 defined from Excel Feature Usage Notes column "Cartographic Generalisation Rule"
        g1_input_points = [fc for fc in fc_list if dynamic_fc_names.Bridge_P in fc]
        g1_align_features = list(filter(str.strip, g1_align_features))
        g1_align_features = [fc for align_fc in g1_align_features for fc in fc_list if str(align_fc) in fc]
        start_step(f"Align points G1 ({len(g1_input_points)} point FCs, {len(g1_align_features)} align FCs)")
        align_points(g1_input_points, g1_align_features, val_dict['Resolve_conflict_build_search_distance_mn'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G1")
        #Align points-G2
        g2_input_points = [fc for fc in fc_list if dynamic_fc_names.Rail_Terminal_Railway_Station_P in fc]
        g2_align_features = [fc for fc in fc_list if dynamic_fc_names.Rail_Line_L in fc]
        start_step(f"Align points G2 ({len(g2_input_points)} point FCs, {len(g2_align_features)} align FCs)")
        align_points(g2_input_points, g2_align_features, val_dict['Resolve_conflict_build_search_distance_mx'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G2")
        # Align points-G3
        g3_input_points = [fc for fc in fc_list if dynamic_fc_names.Toll_Plaza_P in fc]
        g3_align_features = [fc for fc in fc_list if dynamic_fc_names.Road_L in fc]
        start_step(f"Align points G3 ({len(g3_input_points)} point FCs, {len(g3_align_features)} align FCs)")
        align_points(g3_input_points, g3_align_features, val_dict['Resolve_conflict_build_search_distance_mx'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G3")
        # Align points-G4
        g4_input_points = [fc for fc in fc_list for build_p in [dynamic_fc_names.Residential_Building_P, dynamic_fc_names.Industrial_Building_P, dynamic_fc_names.Educational_Building_P] if build_p in fc]
        g4_align_features = list(filter(str.strip, g4_align_features))
        g4_align_features = [fc for align_fc in g4_align_features for fc in fc_list if str(align_fc) in fc]
        start_step(f"Align points G4 ({len(g4_input_points)} point FCs, {len(g4_align_features)} align FCs)")
        align_points(g4_input_points, g4_align_features, val_dict['Resolve_conflict_build_search_distance_mx'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G4")
        #Align points-G5
        g5_input_points = list(filter(str.strip, g5_input_points))
        g5_input_points = [fc for input_fc in g5_input_points for fc in fc_list if str(input_fc) in fc]
        g5_align_features = list(filter(str.strip, g5_align_features))
        g5_align_features = [fc for align_fc in g5_align_features for fc in fc_list if str(align_fc) in fc]
        start_step(f"Align points G5 ({len(g5_input_points)} point FCs, {len(g5_align_features)} align FCs)")
        align_points(g5_input_points, g5_align_features, val_dict['Resolve_conflict_build_search_distance_mn'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G5")
        # Align points-G6
        g6_input_points = [fc for fc in fc_list if dynamic_fc_names.Jetty_Pier_P in fc]
        g6_align_features = list(filter(str.strip, g6_align_features))
        g6_align_features = [fc for align_fc in g6_align_features for fc in fc_list if str(align_fc) in fc]
        start_step(f"Align points G6 ({len(g6_input_points)} point FCs, {len(g6_align_features)} align FCs)")
        align_points(g6_input_points, g6_align_features, val_dict['Resolve_conflict_build_search_distance_mn'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G6")
        # Align points-G7
        g7_input_points = list(filter(str.strip, g7_input_points))
        g7_input_points = [fc for input_fc in g7_input_points for fc in fc_list if str(input_fc) in fc]
        g7_align_features = list(filter(str.strip, g7_align_features))
        g7_align_features = [fc for align_fc in g7_align_features for fc in fc_list if str(align_fc) in fc]
        start_step(f"Align points G7 ({len(g7_input_points)} point FCs, {len(g7_align_features)} align FCs)")
        align_points(g7_input_points, g7_align_features, val_dict['Resolve_conflict_build_search_distance_mn'], val_dict['Resolve_conflict_build_orient_direction'], val_dict['Resolve_conflict_build_ref_scale'], val_dict['Resolve_conflict_build_hierarchy_field'], symbology_file_path, val_dict['RCB_Align_points_orientation_field_name'], working_gdb, map_name)
        log_step("Align points G7")

        # Fix Vegetation after Resolve Conflicts
        start_step("Fix vegetation after resolve conflicts")
        fix_veg_after_resolve_conflict(fc_list, input_primary, input_secondary, val_dict['Resolve_conflict_build_in_prim_sql'], val_dict['Resolve_conflict_build_mx_gap_area'], val_dict['Resolve_conflict_build_fill_option'], val_dict['Resolve_conflict_build_visible_field'], working_gdb, logger)
        log_step("Fix vegetation after resolve conflicts")

        # # Move Buildings According to Adjacent stated Points
        ## move_building_near_adjacent_points(fc_list, map_name, logger, working_gdb, val_dict['RCP_move_building_near_adjacent_points_dist_mx'], val_dict['RCP_move_building_near_adjacent_points_dist_mn'], val_dict['Resolve_conflict_build_visible_field'], val_dict['Resolve_conflict_build_building_gap'], val_dict['Resolve_conflict_build_minimum_size'], val_dict['Resolve_conflict_build_hierarchy_field'])

        # Apply Layer Definition on Building Feature Classes
        start_step("Apply layer definition")
        apply_layer_definition(df_query_input_lyr_9b, val_dict['RCL_Apply_Layer_Definition_expression'] , map_name)
        log_step("Apply layer definition")
        logger.info("[9b] All steps completed")

        # # # resolve_building_point_road_track_conflict(building_point_fcs, logger, road_fc, track_fc)
        # # # resolve_building_polygon_road_track_conflict(building_polygon_fcs, logger, road_fc, track_fc)

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Resolve conflicts for buildings error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Resolve conflicts for buildings', f'{exc_value}\n')
        # Propagate so main.py records the theme as failed instead of logging
        # a successful run and backing up a half-processed geodatabase.
        raise