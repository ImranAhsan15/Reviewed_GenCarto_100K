# Import required modules from the respective theme, helper, and common utility files
import arcpy
import time
import os
import sys
import traceback
import logging
import importlib
import DetermineTouching as touch
import RemoveByConverting as convert
import SplitByBox
import get_param_vals as ParamFile
from get_param_vals import ParamValues
from datetime import datetime
import LayerGrouping as LG

import common_utils as common_utils

# Import theme files
import theme_01_data_prep as theme_01_data_prep
import theme_02_transportation as theme_02_transportation
import theme_03_hydrography as theme_03_hydrography
import theme_04_buildup as theme_04_buildup
import theme_05_utility as theme_05_utility
import theme_06_hypsography as theme_06_hypsography
import theme_07_vegetation as theme_07_vegetation
import theme_08_apply_carto_symbology as theme_08_apply_carto_symbology
import theme_09a_resolve_conflict_lines as theme_09a_resolve_conflict_lines
import theme_09b_resolve_conflict_polygons as theme_09b_resolve_conflict_polygons
import theme_10_detect_conflict as theme_10_detect_conflict
import theme_11_load_data as theme_11_load_data

# Set development mode
DEV_MODE = True
if(DEV_MODE):
    importlib.reload(ParamFile)
    importlib.reload(common_utils)
    importlib.reload(touch)
    importlib.reload(convert)
    importlib.reload(SplitByBox)
    importlib.reload(theme_01_data_prep)
    importlib.reload(theme_02_transportation)
    importlib.reload(theme_03_hydrography)
    importlib.reload(theme_04_buildup)
    importlib.reload(theme_05_utility)
    importlib.reload(theme_06_hypsography)
    importlib.reload(theme_07_vegetation)
    importlib.reload(theme_08_apply_carto_symbology)
    importlib.reload(theme_09a_resolve_conflict_lines)
    importlib.reload(theme_09b_resolve_conflict_polygons)
    importlib.reload(theme_10_detect_conflict)
    importlib.reload(theme_11_load_data)
    importlib.reload(LG)



def backup_theme_data(log_dir, stage_folder, in_feature_loc, logger):
    """Create the Auto/Edit backup folders for a pipeline stage and back up the
    working geodatabase into the Auto folder. Returns the backup GDB location."""
    backup_path = os.path.join(log_dir, "Backup", stage_folder, "Auto")
    os.makedirs(backup_path, exist_ok=True)
    os.makedirs(os.path.join(log_dir, "Backup", stage_folder, "Edit"), exist_ok=True)

    backup_gdb_loc = os.path.join(backup_path, os.path.basename(in_feature_loc))
    if not os.path.exists(backup_gdb_loc) and os.path.isdir(in_feature_loc):
        os.makedirs(backup_gdb_loc)
    common_utils.backup_data(in_feature_loc, backup_gdb_loc, logger)
    return backup_gdb_loc


def main():
    logger = None
    try:
        # Calling logger
        log_dir = os.path.dirname(arcpy.env.scratchGDB)
        logger = common_utils.error_msgs(log_dir)

        # Create scratch GDB if not exists
        scratch_gdb = os.path.join(log_dir, "scratch.gdb")
        if not arcpy.Exists(scratch_gdb):
            arcpy.management.CreateFileGDB(log_dir, "scratch.gdb", "CURRENT")

        # Current time
        current_time = time.time()
        arcpy.AddMessage('Starting cartographic generalisation processing.....')
        logger.info('Starting cartographic generalisation processing.....')

        start_time = current_time
        logger.info(f"Starting time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start_time))}")
        

        # User Input
        theme_type = arcpy.GetParameterAsText(0)
        in_feature_loc = arcpy.GetParameterAsText(1)
        hierarchy_file = arcpy.GetParameterAsText(2)
        out_workspace = arcpy.GetParameterAsText(3)
        rev_workspace = arcpy.GetParameterAsText(4)
        excel_file = arcpy.GetParameterAsText(5)
        symbology_file_path = arcpy.GetParameterAsText(6)
        vst_workspace = arcpy.GetParameterAsText(7)
        working_gdb = scratch_gdb

        # Validate all user-supplied inputs up front and stop if anything
        # required for this theme is missing or invalid.
        input_errors = common_utils.validate_user_inputs(theme_type, {
            'in_feature_loc': in_feature_loc,
            'excel_file': excel_file,
            'hierarchy_file': hierarchy_file,
            'symbology_file_path': symbology_file_path,
            'vst_workspace': vst_workspace,
            'out_workspace': out_workspace,
        }, logger)
        if input_errors:
            return

        # Extension licenses required by each theme's tools (arcpy.CheckExtension
        # codes). Every theme also needs the Advanced product level for the
        # cartography tools. Verified before any processing starts, so a missing
        # license stops the run immediately instead of hours in.
        theme_required_extensions = {
            '3-Hydrography Generalization': ['Foundation'],       # arcpy.topographic: FillGaps, EliminatePolygon, IdentifyNarrowPolygons, MergeLinesByPseudoNode
            '9b-Resolve Conflict for Polygons': ['Foundation'],   # arcpy.topographic: FillGaps, EliminatePolygon
        }
        common_utils.check_required_licenses(logger, extensions=theme_required_extensions.get(theme_type, []), product='ArcInfo')

        # Use all available cores for geoprocessing tools that support parallel
        # processing (Pairwise* and other multi-threaded tools). The cartography
        # tools ignore this setting, so it is safe for all themes.
        arcpy.env.parallelProcessingFactor = "100%"
        logger.info('Parallel processing factor set to 100%')

        # Get params values from excel file
        if os.path.exists(excel_file):

            # Initialize object
            val_obj = ParamValues(excel_file)
            fc_dict = val_obj.get_param_list()
            val_dict = val_obj.get_param_vals()
            common_utils.init_layer_name_resolver(excel_file)
            
            arcpy.AddMessage('Fetching parameter values from configuration file.....')

            # Get param values
            hypso_compare_features = fc_dict['hypso_compare_features']
            veg_lyrs_list = fc_dict['veg_lyrs_list']
            veg_field_values = fc_dict['veg_field_values']
            prep_line_resolve_fcs_list = fc_dict['prep_line_resolve_fcs_list']
            footprint_fcs = fc_dict['footprint_fcs']
            resolve_line_compare = fc_dict['resolve_line_compare']
            utility_area_features = fc_dict['utility_area_features']
            utility_point_features = fc_dict['utility_point_features']
            utility_compare_features = fc_dict['utility_compare_features']
            not_include_fields = fc_dict['not_include_fields']
            bau_field_fc = fc_dict['bau_field_fc']
            buffer_points_25K = fc_dict['buffer_points_25K']
            feature_to_split = fc_dict['feature_to_split']
            railway_sql = fc_dict['railway_sql']
            fcs_trim_extent_trans = fc_dict['fcs_trim_extent_trans']
            fcs_trim_extent_hyd = fc_dict['fcs_trim_extent_hyd']
            fcs_trim_extend = fcs_trim_extent_hyd + fcs_trim_extent_trans
            Structure2Structure = fc_dict['Structure2Structure']
            Structure2Lines = fc_dict['Structure2Lines']
            Lines2Lines = fc_dict['Lines2Lines']
            G1_Poly2Poly = fc_dict['G1_Poly2Poly']
            G2_Poly2Poly = fc_dict['G2_Poly2Poly']
            G3_Poly2Poly = fc_dict['G3_Poly2Poly']
            df_query_input_lyr = fc_dict['df_query_input_lyr']
            build_up_area_fcs = fc_dict['build_up_area_fcs']
            input_building_layers = fc_dict['input_building_layers'] 
            input_barrier_layers = fc_dict['input_barrier_layers']
            g5_input_points = fc_dict['g5_input_points']
            g7_input_points = fc_dict['g7_input_points']
            g1_align_features = fc_dict['g1_align_features']
            g4_align_features = fc_dict['g4_align_features']
            g5_align_features = fc_dict['g5_align_features']
            g6_align_features = fc_dict['g6_align_features']
            g7_align_features = fc_dict['g7_align_features']
            df_query_input_lyr_9b = fc_dict['df_query_input_lyr_9b']
            input_primary = fc_dict['input_primary']
            input_secondary = fc_dict['input_secondary']
            input_line_layers = fc_dict['input_line_layers']
            edge_features = fc_dict['edge_features']
            embank_list = fc_dict['embank_list']
            compare_fcs_embank = fc_dict['compare_fcs_embank']
            road_query = fc_dict['road_query']
            bridge_query = fc_dict['bridge_query']
            attribution_fc_list = fc_dict['attribution_fc_list']
            apply_symbology_layers_list = fc_dict['apply_symbology_layers_list']
            express_list = fc_dict['express_list']
            query_list = fc_dict['query_list']
            field_list = fc_dict['field_list']
            intersecting_fc_list = fc_dict['intersecting_fc_list']
            collapse_sql = fc_dict['collapse_sql']
            cut_build_a_inputs_poly = fc_dict['cut_build_a_inputs_poly']
            small_bldg_2_point_a = fc_dict['small_bldg_2_point_a']
            small_bldg_2_point_p = fc_dict['small_bldg_2_point_p']
            features_in_cemetery = fc_dict['features_in_cemetery']
            enlarge_barrier_features = fc_dict['enlarge_barrier_features']
            delineate_building_layers = fc_dict['delineate_building_layers']
            delineate_edge_features = fc_dict['delineate_edge_features']
            delete_small_features = fc_dict['delete_small_features']
            enlarge_building_features = fc_dict['enlarge_building_features']
            hydro_prep_fc_list = fc_dict['hydro_prep_fc_list']
            hydro_input_polygon_fc = fc_dict['hydro_input_polygon_fc']
            hydro_center_line_fc = fc_dict['hydro_center_line_fc']
            hydro_replace_fc = fc_dict['hydro_replace_fc']
            hydro_remove_near_poly_list = fc_dict['hydro_remove_near_poly_list']
            hydro_enlarge_poly_list = fc_dict['hydro_enlarge_poly_list']
            hydro_remove_small_poly_list = fc_dict['hydro_remove_small_poly_list']
            hydro_erase_poly_list = fc_dict['hydro_erase_poly_list']
            hydro_small_line_fc_list = fc_dict['hydro_small_line_fc_list']
            hydro_small_point_fc_list = fc_dict['hydro_small_point_fc_list']
            hydro_delete_small_pools = fc_dict['hydro_delete_small_pools']
            trans_build_up_buildings = fc_dict['trans_build_up_buildings']
            trans_topology_features = fc_dict['topology_features']
            veg_transfer_veg_features = fc_dict['veg_transfer_veg_features']
            delete_small_bldgs = fc_dict['delete_small_bldgs']
            utility_merge_clusters = fc_dict['utility_merge_clusters']
            align_bridge_point_input = fc_dict['align_bridge_point_input']
            align_bridge_point_waterbody = fc_dict['align_bridge_point_waterbody']
            align_bridge_point_surface = fc_dict['align_bridge_point_surface']

            
            # Mapx Config values
            map_name_data_preparation = val_dict['mapx_map_name_data_prep']
            map_name_transportation = val_dict['mapx_map_name_transportation']
            map_name_hydrography = val_dict['mapx_map_name_hydrography']
            map_name_builtup_generalization = val_dict['mapx_map_name_builtup']
            map_name_utility = val_dict['mapx_map_name_utility']
            map_name_hypsography = val_dict['mapx_map_name_hypsography']
            map_name_vegetation = val_dict['mapx_map_name_vegetation']
            map_name_apply_carto_symbology = val_dict['mapx_map_name_apply_carto_symbology']
            map_name_resolve_lines = val_dict['mapx_map_name_resolve_lines']
            map_name_resolve_polygons = val_dict['mapx_map_name_resolve_polygons']
            map_name_detect_conflict = val_dict['mapx_map_name_detect_conflict']
            
            # Hydrography config value
            remove_short_line_line_length = val_dict['Hydrography_remove_short_line_line_length']
            hydro_np_polygon_width = val_dict['Hydrography_np_polygon_width']
            hydro_np_polygon_percentage = val_dict['Hydrography_np_polygon_percentage']
            hydro_simple_tolerance = val_dict['Hydrography_Hydro_Gen_simple_tolerance']
            hydro_smooth_tolerance = val_dict['Hydrography_Hydro_Gen_smooth_tolerance']
            hydro_remove_small_poly_exp = val_dict['Hydrography_hydro_remove_small_poly_exp']
            hydro_remove_small_poly_mim_area = val_dict['Hydrography_hydro_remove_small_poly_mim_area']
            hydro_enlarge_poly_mim_size = val_dict['Hydrography_hydro_enlarge_poly_min_size']
            hydro_enlarge_poly_buffer_dist = val_dict['Hydrography_hydro_enlarge_poly_buffer_dist']
            hydro_remove_near_poly_delete_size = val_dict['Hydrography_hydro_remove_near_poly_delete_size']
            hydro_remove_near_poly_min_size = val_dict['Hydrography_hydro_remove_near_poly_min_size']
            hydro_remove_near_poly_dist = val_dict['Hydrography_hydro_remove_near_poly_dist']
            hydro_remove_near_poly_sql = val_dict['Hydrography_hydro_remove_near_poly_sql']
            hydro_enlarge_poly_sql = val_dict['Hydrography_hydro_enlarge_poly_sql']
            hydro_enlarge_untouch_poly_buffer_dist = val_dict['Hydrography_hydro_enlarge_untouch_poly_buffer_dist']
            hydro_trim_between_polygon_min_area = val_dict['Hydrography_hydro_trim_between_polygon_min_area']
            hydro_trim_between_polygon_distance = val_dict['Hydrography_hydro_trim_between_polygon_distance']
            hydro_remove_small_sql = val_dict['Hydrography_hydro_remove_small_sql']
            hydro_remove_small_min_size = val_dict['Hydrography_hydro_remove_small_min_size']
            hydro_erase_poly_max_gap_area = val_dict['Hydrography_hydro_erase_poly_max_gap_area']
            hydro_convert_ungr_river_min_length = val_dict['Hydrography_hydro_convert_ungr_river_min_length']
            hydro_generalized_operation = val_dict['Hydrography_hydro_generalized_operation']
            hydro_delete_input = val_dict['Hydrography_hydro_delete_input']
            hydro_create_one_point = val_dict['Hydrography_hydro_create_one_point']
            hydro_trim_update_val = val_dict['Hydrography_hydro_trim_update_val']
            hydro_unique_field = val_dict['Hydrography_hydro_unique_field']
            remove_close_parallel_per_min = val_dict['Hydrography_remove_close_parallel_per_min']
            remove_close_parallel_per_max = val_dict['Hydrography_remove_close_parallel_per_max']
            remove_close_dist = val_dict['Hydrography_remove_close_dist']
            remove_close_tolerance = val_dict['Hydrography_remove_close_tolerance']
            hydro_line_dangle_min_length = val_dict['Hydrography_hydro_line_dangle_min_length']
            hydro_small_fc_min_length = val_dict['Hydrography_hydro_small_fc_min_length']
            increase_hydro_line_min_length = val_dict['Hydrography_increase_hydro_line_min_length']
            hydro_delete_small_pool_min_area = val_dict["Hydrography_delete_small_pool_min_area"]
            hydro_replace_poly_with_line_smooth_tolerance = val_dict["Hydrography_replace_poly_with_line_smooth_tolerance"]

            # Data prep config value
            dataset_name = val_dict['Data_prep_dataset_name']
            buffer_distance_point = val_dict['Data_prep_buffer_distance_point']
            extend_val = val_dict['Data_prep_extend_val']
            trim_val = val_dict['Data_prep_trim_dangle_value']
            buffer_distance = val_dict['Data_prep_buffer_distance']
            feature_count = val_dict['Data_prep_feature_count']
            vertex_limit = val_dict['Data_prep_vertex_limit_feature_dice']

            # Transportation config value
            collapse_size = val_dict['Transport_collapse_size']
            seg_length = val_dict['Transport_min_seg_length']
            group_sql_rd1 = val_dict['Transport_group_sql_rd1']
            group_sql_rd2 = val_dict['Transport_group_sql_rd2']
            group_sql_track = val_dict['Transport_group_sql_track']
            trans_common_express = val_dict['Transport_common_express']
            trans_generalized_operation = val_dict['Transport_generalized_operation']
            trans_delete_input = val_dict['Transport_delete_input']
            trans_create_one_point = val_dict['Transport_create_one_point']
            trans_update_val = val_dict['Transport_update_val']
            trans_changed_road_type = val_dict['Transport_changed_road_type']
            trans_unique_field = val_dict['Transport_unique_field']
            minimum_length_min = val_dict['Transport_minimum_length_min']
            minimum_length_max = val_dict['Transport_minimum_length_max']
            simple_tolerance = val_dict['Transport_simplify_tolerance']
            smooth_tolerance = val_dict['Transport_smooth_tolerance']
            merge_field = val_dict['Transport_merge_field']
            min_size = val_dict['Transport_min_size']
            additional_criteria_trans = val_dict['Transport_additional_criteria']
            minimum_length = val_dict['Transport_minimum_length']
            minimum_width = val_dict['Transport_minimum_width']
            merge_distance = val_dict['Transport_merge_distance']
            remove_backlane_length = val_dict['Transport_remove_backlane_length']
            remove_backlane_tolerance = val_dict['Transport_remove_backlane_distance']

            # Built up config value
            min_size_bldg1 = val_dict['Built_min_size_bldg1']
            sql_bldg = val_dict['Built_sql_bldg']
            min_size_bldg2 = val_dict['Built_min_size_bldg2']
            enlarge_min_size = val_dict['Built_enlarge_min_size']
            enlarge_val = val_dict['Built_enlarge_val']
            del_min_area = val_dict['Built_del_min_area']
            enlarge_bldg_min_width = val_dict['Built_enlarge_bldg_min_width']
            enlarge_bldg_min_length = val_dict['Built_enlarge_bldg_min_length']
            enlarge_bldg_additional_criteria = val_dict['Built_additional_criteria']
            simpl_bldg_distance = val_dict['Built_simpl_bldg_distance']
            simplification_tolerance = val_dict['Built_simplification_tolerance']
            delineate_ref_scale = val_dict['Built_delineate_ref_scale']
            delineate_min_bldg_count = val_dict['Built_delineate_min_bldg_count']
            delineate_min_detail_size = val_dict['Built_delineate_min_detail_size']
            delineate_grp_dist = val_dict['Built_delineate_grp_dist']
            del_small_recreation_fc_min_size = val_dict['Built_del_small_recreation_min_size']
            erase_sql = val_dict['Built_erase_sql']
            build_delete_input = val_dict['Built_delete_input']
            build_create_one_point = val_dict['Built_create_one_point']
            build_unique_field = val_dict['Built_unique_field']

            # Utility config value
            utility_min_size_sewerage = val_dict['Utility_min_size_sewerage'] 
            utility_min_size_building = val_dict['Utility_min_size_building']
            utility_min_size = val_dict['Utility_min_size']
            utility_beffer_dist = val_dict['Utility_beffer_distance']
            utility_dist = val_dict['Utility_merge_paraller_distance']
            utility_dist_shorter = val_dict['Utility_merge_paraller_distance_shorter']
            utility_addi_criteria_sewerage = val_dict['Utility_addi_criteria_sewerage']
            utility_addi_criteria = val_dict['Utility_addi_criteria']
            utility_merge_field = val_dict['Utility_merge_field']
            utility_delete_input = val_dict['Utility_delete_input']
            utility_create_one_point = val_dict['Utility_create_one_point_each_unique_value']
            utility_update_val = val_dict['Utility_update_val']
            utility_unique_field = val_dict['Utility_unique_field']
            utility_powerline_val = val_dict['Utility_powerline_val']
            utility_aggregate_val = val_dict['Utility_aggregate_val']
            
            # Hypsography config value
            hypso_dissolved_field = val_dict['Hypso_dissolved_field']
            hypso_dist = val_dict['Hypso_distance']
            hypso_parallel_per = val_dict['Hypso_parallel_percent']
            hypso_min_length = val_dict['Hypso_minimum_length']
            hypso_smoothing_tolerance = val_dict['Hypso_smoothing_tolerance']
            hypso_increase_factor = val_dict['Hypso_increase_factor']
            hypso_size_max = val_dict['Hypso_miximum_size']
            hypso_size_min = val_dict['Hypso_minimum_size']

            # Vegetation config value
            vegetation_min_area = val_dict['Veg_minimum_area']
            vegetation_eliminate_area = val_dict['Veg_eliminate_area']

            # Apply Carto Symbology config value
            query_acs = val_dict['Applycarto_query_acs']
            distance_acs = val_dict['Applycarto_distance_between_features']
            mx_no_close_fcs_l = val_dict['Applycarto_maximum_number_of_close_features_l']
            mx_no_close_fcs_m = val_dict['Applycarto_maximum_number_of_close_features_m']
            mx_no_close_fcs_u = val_dict['Applycarto_maximum_number_of_close_features_u']
            feature_count_acs = val_dict['Applycarto_feature_count']
            specification = val_dict['Applycarto_specification']
            apply_carto_map_scale = val_dict['ApplyCarto_Map_Scale']
            apply_carto_map_unit = val_dict['ApplyCarto_Map_Unit_of_Dataset']
            X_anchor_offset_VA1060_Oil_Palm_A = val_dict['ApplyCarto_VA1060_Oil_Palm_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_VA1060_Oil_Palm_A = val_dict['ApplyCarto_VA1060_Oil_Palm_A_Y_Anchor_Offset_Percent']
            marker_width_VA1060_Oil_Palm_A = val_dict['ApplyCarto_VA1060_Oil_Palm_A_Marker_Width']
            marker_height_VA1060_Oil_Palm_A = val_dict['ApplyCarto_VA1060_Oil_Palm_A_Marker_Height']
            X_anchor_offset_VA1030_Coconut_A = val_dict['ApplyCarto_VA1030_Coconut_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_VA1030_Coconut_A = val_dict['ApplyCarto_VA1030_Coconut_A_Y_Anchor_Offset_Percent']
            marker_width_VA1030_Coconut_A = val_dict['ApplyCarto_VA1030_Coconut_A_Marker_Width']
            marker_height_VA1030_Coconut_A  = val_dict['ApplyCarto_VA1030_Coconut_A_Marker_Height']
            X_anchor_offset_HF0070_Rocks_A = val_dict['ApplyCarto_HF0070_Rocks_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_HF0070_Rocks_A = val_dict['ApplyCarto_HF0070_Rocks_A_Y_Anchor_Offset_Percent']
            marker_width_HF0070_Rocks_A = val_dict['ApplyCarto_HF0070_Rocks_A_Marker_Width']
            marker_height_HF0070_Rocks_A = val_dict['ApplyCarto_HF0070_Rocks_A_Marker_Height']
            X_anchor_offset_GF4100_Rock_Outcrop_A = val_dict['ApplyCarto_GF4100_Rock_Outcrop_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_GF4100_Rock_Outcrop_A = val_dict['ApplyCarto_GF4100_Rock_Outcrop_A_Y_Anchor_Offset_Percent']
            marker_width_GF4100_Rock_Outcrop_A = val_dict['ApplyCarto_GF4100_Rock_Outcrop_A_Marker_Width']
            marker_height_GF4100_Rock_Outcrop_A = val_dict['ApplyCarto_GF4100_Rock_Outcrop_A_Marker_Height']
            X_anchor_offset_GF4200_Rock_Boulders_A = val_dict['ApplyCarto_GF4200_Rock_Boulders_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_GF4200_Rock_Boulders_A = val_dict['ApplyCarto_GF4200_Rock_Boulders_A_Y_Anchor_Offset_Percent']
            marker_width_GF4200_Rock_Boulders_A = val_dict['ApplyCarto_GF4200_Rock_Boulders_A_Marker_Width']
            marker_height_GF4200_Rock_Boulders_A = val_dict['ApplyCarto_GF4200_Rock_Boulders_A_Marker_Height']
            X_anchor_offset_GD3100_Quarry_Pit_A = val_dict['ApplyCarto_GD3100_Quarry_Pit_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_GD3100_Quarry_Pit_A = val_dict['ApplyCarto_GD3100_Quarry_Pit_A_Y_Anchor_Offset_Percent']
            marker_width_GD3100_Quarry_Pit_A = val_dict['ApplyCarto_GD3100_Quarry_Pit_A_Marker_Width']
            marker_height_GD3100_Quarry_Pit_A = val_dict['ApplyCarto_GD3100_Quarry_Pit_A_Marker_Height']
            X_anchor_offset_VB3020_Rubber_Trees_A = val_dict['ApplyCarto_VB3020_Rubber_Trees_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_VB3020_Rubber_Trees_A = val_dict['ApplyCarto_VB3020_Rubber_Trees_A_Y_Anchor_Offset_Percent']
            marker_width_VB3020_Rubber_Trees_A = val_dict['ApplyCarto_VB3020_Rubber_Trees_A_Marker_Width']
            marker_height_VB3020_Rubber_Trees_A = val_dict['ApplyCarto_VB3020_Rubber_Trees_A_Marker_Height']
            X_anchor_offset_HA0100_Reef_A = val_dict['ApplyCarto_HA0100_Reef_A_X_Anchor_Offset_Percent']
            Y_anchor_offset_HA0100_Reef_A = val_dict['ApplyCarto_HA0100_Reef_A_Y_Anchor_Offset_Percent']
            marker_width_HA0100_Reef_A = val_dict['ApplyCarto_HA0100_Reef_A_Marker_Width']
            marker_height_HA0100_Reef_A = val_dict['ApplyCarto_HA0100_Reef_A_Marker_Height']

            # Detect Conflict config value
            dc_express = val_dict['Detect_expression']
            dc_ref_scale = val_dict['Detect_reference_scale']
            dc_severity = val_dict['Detect_severity']
            dc_reviewer_session = val_dict['Detect_reviewer_session']
            dc_distance = val_dict['Detect_conflict_distance']

            
            # Resolve Lines config value
            ln_lyr_ex = val_dict['Resolve_conflict_line_lyr_expression']
            res_con_line_delete = val_dict['Resolve_conflict_line_delete']
            river_ex = val_dict['Resolve_conflict_line_river_lyr_expression']
            road_query_rlc = val_dict['Resolve_conflict_lines_road_query']
            name_fld = val_dict['Resolve_conflict_line_name_field']
            distance_b = val_dict['Resolve_conflict_line_distance_b']
            distance_s = val_dict['Resolve_conflict_line_distance_s']
            min_area = val_dict['Resolve_conflict_line_minimum_area']
            additional_criteria = val_dict['Resolve_conflict_line_additional_criteria']
            distance_rcl = val_dict['Resolve_conflict_line_distance_l']
            min_length = val_dict['Resolve_conflict_line_distance_minimum_length']
            orient_fld = val_dict['Resolve_conflict_line_orient_fld']
            offset_dist_l = val_dict['Resolve_conflict_line_offset_distance_s']
            offset_dist_u = val_dict['Resolve_conflict_line_offset_distance_l']
            offset_dist_benc_l = val_dict['Resolve_conflict_line_offset_distance_benc_l']
            offset_dist_benc_u = val_dict['Resolve_conflict_line_offset_distance_benc_u']
            perpendicular_k = val_dict['Resolve_conflict_line_perpendicular_k']
            perpendicular_b = val_dict['Resolve_conflict_line_perpendicular_b']
            bench_query = val_dict['Resolve_conflict_line_bench_query']
            res_con_line_erase_input_fcs = val_dict['Resolve_conflict_line_erase_input_features']

            # Resolve Buildings config value
            express_val_mx = val_dict['Resolve_conflict_build_express_val_mx']
            express_val_mn = val_dict['Resolve_conflict_build_express_val_mn']
            visible_field = val_dict['Resolve_conflict_build_visible_field']
            hierarchy_field = val_dict['Resolve_conflict_build_hierarchy_field']
            search_distance = val_dict['Resolve_conflict_build_search_distance']
            query = val_dict['Resolve_conflict_build_query']
            bb_lyr_ex = val_dict['Resolve_conflict_build_bb_lyr_ex']
            bb_lyr_ex_his = val_dict['Resolve_conflict_build_bb_lyr_ex_his']
            ref_scale = val_dict['Resolve_conflict_build_ref_scale']
            minimum_size = val_dict['Resolve_conflict_build_minimum_size']
            bld_gap = val_dict['Resolve_conflict_build_building_gap']
            ap_src_dis_mx = val_dict['Resolve_conflict_build_search_distance_mx']
            ap_src_dis_mn = val_dict['Resolve_conflict_build_search_distance_mn']
            orient_dir = val_dict['Resolve_conflict_build_orient_direction']
            in_prim_sql = val_dict['Resolve_conflict_build_in_prim_sql']
            max_gap_area = val_dict['Resolve_conflict_build_mx_gap_area']
            fill_option = val_dict['Resolve_conflict_build_fill_option']
            orient_field = val_dict['RCB_Align_points_orientation_field_name']


        else:
            arcpy.AddError(f'Configuration file not found: {excel_file}. Please check the path and run again.')
            logger.error(f'Configuration file not found: {excel_file}')
            return

        logger.info(f'Configuration loaded. Dataset name from config: {dataset_name}')

        # Checking configuration file data inputs
        arcpy.env.workspace = in_feature_loc
        datasets = arcpy.ListDatasets("*", "Feature")
        dataset_list = [dataset for dataset in datasets]
        if len(dataset_list) == 0 and dataset_name != '':
            arcpy.AddError('Since geodatabase has no dataset. Please remove dataset name from configuration file and Again run.....')
            return
        elif len(dataset_list) > 0 and dataset_name == '':
            arcpy.AddError('Since geodatabase has dataset. Please add dataset name in configuration file and Again run.....')
            return

        
        # Checking AOI feature class in input geodatabase
        aoi_fc = os.path.join(in_feature_loc, 'AOI')
        aoi_l_fc = os.path.join(in_feature_loc, 'AOI_L')
        # logger.info(f'Found {aoi_fc} and {aoi_l_fc}')
        if not arcpy.Exists(aoi_fc) and not arcpy.Exists(aoi_l_fc):
            arcpy.AddError('AOI and AOI_L feature class not found in input geodatabase. Please check and Again run.....')
            return

        backup_path_dataprep_edit = os.path.join(log_dir, "Backup", "01-AFTDP", "Edit")
        backup_path_transport_edit = os.path.join(log_dir, "Backup", "02-AFTTrans", "Edit")
        backup_path_hydrography_edit = os.path.join(log_dir, "Backup", "03-AFTHydro", "Edit")
        backup_path_builtup_edit = os.path.join(log_dir, "Backup", "04-AFTBuiltUp", "Edit")
        backup_path_utility_edit = os.path.join(log_dir, "Backup", "05-AFTUtil", "Edit")
        backup_path_hypsography_edit = os.path.join(log_dir, "Backup", "06-AFTHypso", "Edit")
        backup_path_vegetation_edit = os.path.join(log_dir, "Backup", "07-AFTVeg", "Edit")
        backup_path_applycarto_edit = os.path.join(log_dir, "Backup", "08-AFTAS", "Edit")
        backup_path_rcl_edit = os.path.join(log_dir, "Backup", "09a-AFTRCL", "Edit")
        backup_path_rcp_edit = os.path.join(log_dir, "Backup", "09b-AFTRCP", "Edit")


        # Starting process based on theme type
        if theme_type == '1-Data Preparation':
            logger.info('Starting Data Preparation Theme.....')
            # Warn about Excel config feature class names that do not match the geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Backup features data
            logger.info('Backing up data before data preparation.....')
            backup_path_ext = os.path.join(log_dir, "Backup", "00-AFTExt", "Auto")
            os.makedirs(backup_path_ext, exist_ok=True)
            backup_path_edit_ext = os.path.join(log_dir, "Backup", "00-AFTExt", "Edit")
            os.makedirs(backup_path_edit_ext, exist_ok=True)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))

            # Get map sheet from input workspace
            aoi = f'{in_feature_loc}\\AOI'

            ## Import the Map file
            imported_map = common_utils.import_mapx(map_name_data_preparation, logger, map_name_data_preparation)
            
            # # Data Cleaning - AFter 04th March
            theme_01_data_prep.data_cleaning_all_funcs(aoi, fc_list, in_feature_loc, working_gdb, val_dict, not_include_fields, 
                            fcs_trim_extend, buffer_points_25K, feature_to_split, bau_field_fc, trans_build_up_buildings, seg_length, logger)
            logger.info(f'Data Prep Theme ran successfully. Starting Backup.....')
            
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "01-AFTDP", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '2-Transportation Generalization':
            logger.info('Starting Transport Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_dataprep_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_transportation, logger, map_name_transportation)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # Get CartoPartition from input workspace
            carto_partition = f'{in_feature_loc}\\CartoPartitionA'
            # Get Generalize operation
            generalize_operations = trans_generalized_operation.split(" ")
            # Get changed road type
            change_road_type = trans_changed_road_type.split(",")
            # Transportation Feature to point
            theme_02_transportation.gen_transportation(fc_list, working_gdb, hierarchy_file, in_feature_loc, collapse_sql, carto_partition, generalize_operations, railway_sql, 
                                                       change_road_type, trans_build_up_buildings, trans_topology_features, val_dict, imported_map.name, logger)
            logger.info(f'Transportation Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "02-AFTTrans", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )

        elif theme_type == '3-Hydrography Generalization':
            logger.info('Starting Hydrography Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_transport_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_hydrography, logger, map_name_hydrography)
            ## Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # Get Generalize operation
            generalize_operations = hydro_generalized_operation.split(" ")
            # Hydrography Feature Generalisation
            theme_03_hydrography.gen_hydrography(fc_list, hydro_prep_fc_list, working_gdb, hydro_input_polygon_fc, hydro_center_line_fc, 
                            hydro_replace_fc, generalize_operations, in_feature_loc, hydro_remove_near_poly_list, 
                            hydro_enlarge_poly_list, hydro_remove_small_poly_list,  hydro_erase_poly_list,  hydro_small_line_fc_list, hydro_small_point_fc_list, 
                            hydro_delete_small_pools, val_dict, logger)
            logger.info(f'Hydrography Theme ran successfully. Starting Backup.....')
            # # # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "03-AFTHydro", in_feature_loc, logger)
            # # Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )

        elif theme_type == '4-Built-up Generalization':
            logger.info('Starting Built-up Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_hydrography_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_builtup_generalization, logger, map_name_builtup_generalization)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # Built-Up Feature Generalisation
            theme_04_buildup.gen_buildup(fc_list, cut_build_a_inputs_poly, small_bldg_2_point_a, small_bldg_2_point_p, working_gdb,  features_in_cemetery, enlarge_barrier_features, delete_small_bldgs,  
                        enlarge_building_features, delineate_building_layers, delineate_edge_features, in_feature_loc, delete_small_features, val_dict, logger, imported_map.name)
            logger.info(f'Built-up Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "04-AFTBuiltUp", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '5-Utilities Generalization':
            logger.info('Starting Utilities Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_builtup_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_utility, logger, map_name_utility)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # # Utility Feature Generalisation
            theme_05_utility.gen_utility(fc_list, utility_area_features, utility_point_features, utility_compare_features, 
                                         val_dict, utility_merge_clusters, working_gdb, logger)
            logger.info(f'Utilities Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "05-AFTUtil", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '6-Hypsography Generalization':
            logger.info('Starting Hypsography Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_utility_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_hypsography, logger, map_name_hypsography)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # Hypsography Feature Generalisation 
            theme_06_hypsography.gen_hypsography(fc_list, hypso_compare_features, val_dict, working_gdb, logger)
            logger.info(f'Hypsography Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "06-AFTHypso", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '7-Vegetation Generalization':
            logger.info('Starting Vegetation Generalization Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_hypsography_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_vegetation, logger, map_name_vegetation)
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # # Vegetation Feature Generalisation
            theme_07_vegetation.gen_vegetation(fc_list, val_dict, veg_lyrs_list, veg_transfer_veg_features, veg_field_values, working_gdb, logger)
            
            logger.info(f'Vegetation Theme ran successfully. Starting Backup.....')
            # # Closing All Map Views
            common_utils.close_active_map_views(logger)
            # Compacting geodatabase
            logger.info(f'Starting compacting geodatabase {in_feature_loc} ...')
            arcpy.management.Compact(in_feature_loc)
            logger.info(f'Compacting geodatabase {in_feature_loc} was successful. Proceeding to backup...')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "07-AFTVeg", in_feature_loc, logger)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '8-Apply Carto Symbology':
            logger.info('Starting Theme Apply Carto Symbology.....')
            common_utils.replace_gdb_from_backup(backup_path_vegetation_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            # ## Import Map File
            imported_map = common_utils.import_mapx(map_name_apply_carto_symbology, logger, map_name_apply_carto_symbology)
            
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, in_feature_loc, logger, False )
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            carto_partition = f'{in_feature_loc}\\CartoPartitionA'
            arcpy.AddMessage(f"in_feature_loc: {in_feature_loc}")
            create_vegetation_symbol_detail = [
                {
                    "name": common_utils.resolve_lyr().Oil_Palm_A,
                    "x_anchor": X_anchor_offset_VA1060_Oil_Palm_A,
                    "y_anchor": Y_anchor_offset_VA1060_Oil_Palm_A,
                    "marker_width": marker_width_VA1060_Oil_Palm_A,
                    "marker_height": marker_height_VA1060_Oil_Palm_A
                }, 
                {
                    "name": common_utils.resolve_lyr().Coconut_A,
                    "x_anchor": X_anchor_offset_VA1030_Coconut_A,
                    "y_anchor": Y_anchor_offset_VA1030_Coconut_A,
                    "marker_width": marker_width_VA1030_Coconut_A,
                    "marker_height": marker_height_VA1030_Coconut_A
                },
                {
                    "name": common_utils.resolve_lyr().Rocks_A,
                    "x_anchor": X_anchor_offset_HF0070_Rocks_A,
                    "y_anchor": Y_anchor_offset_HF0070_Rocks_A,
                    "marker_width": marker_width_HF0070_Rocks_A,
                    "marker_height": marker_height_HF0070_Rocks_A
                },
                {
                    "name": common_utils.resolve_lyr().Rock_Outcrop_A,
                    "x_anchor": X_anchor_offset_GF4100_Rock_Outcrop_A,
                    "y_anchor": Y_anchor_offset_GF4100_Rock_Outcrop_A,
                    "marker_width": marker_width_GF4100_Rock_Outcrop_A,
                    "marker_height": marker_height_GF4100_Rock_Outcrop_A
                },
                {
                    "name": common_utils.resolve_lyr().Rock_Boulders_A,
                    "x_anchor": X_anchor_offset_GF4200_Rock_Boulders_A,
                    "y_anchor": Y_anchor_offset_GF4200_Rock_Boulders_A,
                    "marker_width": marker_width_GF4200_Rock_Boulders_A,
                    "marker_height": marker_height_GF4200_Rock_Boulders_A
                },
                # # {
                # #     "name": common_utils.resolve_lyr().Quarry_Pit_A,
                # #     "x_anchor": X_anchor_offset_GD3100_Quarry_Pit_A,
                # #     "y_anchor": Y_anchor_offset_GD3100_Quarry_Pit_A,
                # #     "marker_width": marker_width_GD3100_Quarry_Pit_A,
                # #     "marker_height": marker_height_GD3100_Quarry_Pit_A
                # # },
                {
                    "name": common_utils.resolve_lyr().Rubber_Trees_A,
                    "x_anchor": X_anchor_offset_VB3020_Rubber_Trees_A,
                    "y_anchor": Y_anchor_offset_VB3020_Rubber_Trees_A,
                    "marker_width": marker_width_VB3020_Rubber_Trees_A,
                    "marker_height": marker_height_VB3020_Rubber_Trees_A
                },
                {
                    "name": common_utils.resolve_lyr().Reef_A,
                    "x_anchor": X_anchor_offset_HA0100_Reef_A,
                    "y_anchor": Y_anchor_offset_HA0100_Reef_A,
                    "marker_width": marker_width_HA0100_Reef_A,
                    "marker_height": marker_height_HA0100_Reef_A

                }
            ]
            align_bridge_config = {
                "align_bridge_point_input": align_bridge_point_input,
                "align_bridge_point_waterbody": align_bridge_point_waterbody,
                "align_bridge_point_surface": align_bridge_point_surface
            }
            # Apply Carto Symbology
            theme_08_apply_carto_symbology.apply_carto_symbology(fc_list, attribution_fc_list, express_list, query_list, field_list, intersecting_fc_list, working_gdb, 
                    in_feature_loc, vst_workspace, hierarchy_file, prep_line_resolve_fcs_list, carto_partition, symbology_file_path, imported_map.name, apply_symbology_layers_list, create_vegetation_symbol_detail, align_bridge_config, val_dict,logger)
            logger.info(f'Apply Carto Symbology Theme ran successfully. Starting Backup.....')
            # Closing All Map Views
            common_utils.close_active_map_views(logger)
            # Compacting geodatabase
            logger.info(f'Starting compacting geodatabase {in_feature_loc} ...')
            arcpy.management.Compact(in_feature_loc)
            logger.info(f'Compacting geodatabase {in_feature_loc} was successful. Proceeding to backup...')
            # Back up Geodatabase again
            # # Backup Geodatabase
            backup_gdb_loc = backup_theme_data(log_dir, "08-AFTAS", in_feature_loc, logger)
            # Update Data Source with Backup GDB
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )
            

        
        elif theme_type == '9a-Resolve Conflict for Lines':
            logger.info('Starting Resolve Conflicts for Lines Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_applycarto_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_resolve_lines, logger, map_name_resolve_lines)
            
            # # Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, in_feature_loc, logger, False )
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            carto_partition = f'{in_feature_loc}\\CartoPartitionA'

            # # Resolving Conflicts for Lines
            theme_09a_resolve_conflict_lines.resolve_conflict_lines(fc_list, in_feature_loc, input_line_layers, symbology_file_path, working_gdb, carto_partition,
                        edge_features, embank_list, compare_fcs_embank, bridge_query, footprint_fcs, resolve_line_compare, road_query, log_dir, imported_map.name, val_dict, logger)
            logger.info(f'Resolve Conflict for Lines Theme ran successfully. Starting Backup.....')
            # Closing All Map Views
            common_utils.close_active_map_views(logger)
            # Compacting geodatabase
            logger.info(f'Starting compacting geodatabase {in_feature_loc} ...')
            arcpy.management.Compact(in_feature_loc)
            logger.info(f'Compacting geodatabase {in_feature_loc} was successful. Proceeding to backup...')
            # Backup features data
            ## Backup Geodatabase
            backup_gdb_loc = backup_theme_data(log_dir, "09a-AFTRCL", in_feature_loc, logger)
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )


        elif theme_type == '9b-Resolve Conflict for Polygons':
            logger.info('Starting Resolve Conflict for Polygons Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_rcl_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_resolve_polygons, logger, map_name_resolve_polygons)
            
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, in_feature_loc, logger, False )
            # # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            # Get CartoPartition from input workspace
            carto_partition = f'{in_feature_loc}\\CartoPartitionA'

            # # Resolving Conflicts for Polygons
            theme_09b_resolve_conflict_polygons.resolve_conflict_polygons(fc_list, build_up_area_fcs, input_building_layers, input_barrier_layers, symbology_file_path, g1_align_features,
                            g4_align_features, g5_input_points, g5_align_features, g6_align_features, g7_input_points, g7_align_features, input_primary, input_secondary, working_gdb, imported_map.name, log_dir, val_dict, df_query_input_lyr_9b, logger, carto_partition=carto_partition)
            logger.info(f'Resolve Conflict for Polygons Theme ran successfully. Starting Backup.....')
            # # Closing All Map Views
            common_utils.close_active_map_views(logger)
            # # Compacting geodatabase
            logger.info(f'Starting compacting geodatabase {in_feature_loc} ...')
            arcpy.management.Compact(in_feature_loc)
            logger.info(f'Compacting geodatabase {in_feature_loc} was successful. Proceeding to backup...')
            # # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "09b-AFTRCP", in_feature_loc, logger)
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )
            
        
        elif theme_type == '10-Detect Conflict':
            logger.info('Starting Detect Conflict Theme.....')
            common_utils.replace_gdb_from_backup(backup_path_rcp_edit, in_feature_loc, logger)
            # Warn about Excel config feature class names that do not match the restored geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            ## Import Map File
            imported_map = common_utils.import_mapx(map_name_detect_conflict, logger, map_name_detect_conflict)
            ## Update Map file Data Source with Backed Up Geodatabase
            common_utils.update_mapx_datasource(imported_map, in_feature_loc, logger, False )
            # Get feature classes
            fc_list = sorted(common_utils.get_fcs(in_feature_loc, dataset_name, logger))
            carto_partition = f'{in_feature_loc}\\CartoPartitionA'

            logger.info(f'Starting Detecting Conflict.....')
            # Apply Layer Definition on Building Feature Classes
            common_utils.apply_layer_definition(df_query_input_lyr, val_dict['detect_conf_layer_definition'], map_name_detect_conflict)

            # # Process Validation (config keys were renamed later; fall back to
            # defaults so an older config file does not crash the theme)
            theme_10_detect_conflict.run_gdb_audit_and_validation(
                in_feature_loc,
                val_dict.get("Detect_Output_GDB", "DetectConflict_Errors.gdb"),
                val_dict.get("Detect_Conflict_ExcelFileName", "DetectConflict_Errors.xlsx"),
                logger, log_dir)
            
            logger.info(f'Detect Conflict Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "10-AFTDC", in_feature_loc, logger)
            common_utils.update_mapx_datasource(imported_map, backup_gdb_loc, logger )



        elif theme_type == '11-Load Data into CARTO100K':
            logger.info('Starting Load Data into CARTO100K Theme.....')
            # Warn about Excel config feature class names that do not match the geodatabase
            common_utils.validate_config_fc_names(fc_dict, in_feature_loc, logger)
            # Get map sheet from input workspace
            aoi = f'{in_feature_loc}\\AOI'
            # Load data into gdb or ent db
            theme_11_load_data.load_data_into_edb(in_feature_loc, aoi, out_workspace, versions, working_gdb, logger)
            logger.info(f'Load Data into CARTO100K Theme ran successfully. Starting Backup.....')
            # Backup features data
            backup_gdb_loc = backup_theme_data(log_dir, "11-Final", in_feature_loc, logger)


        end_time = time.time()
        logger.info(f"Ending time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(end_time))}")
        total_time = end_time - start_time
        logger.info(f"The cartographic generalisation process is successfully completed with {total_time} s")
        arcpy.AddMessage(f"The cartographic generalisation process is successfully completed.....")

    except arcpy.ExecuteError:
        error_message = f'Gen carto 100k geoprocessing error:\n{arcpy.GetMessages(2)}\nTraceback details:\n{traceback.format_exc()}'
        arcpy.AddError(error_message)
        if logger:
            logger.error(error_message)
        common_utils.simplified_msgs('Gen carto 100k', f'{arcpy.GetMessages(2)}\n')
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'Gen carto 100k error: {e}\nTraceback details:\n{tb}'
        arcpy.AddError(error_message)
        if logger:
            logger.error(error_message)
        common_utils.simplified_msgs('Gen carto 100k', f'{exc_value}\n')

if __name__ == '__main__':
    main()
