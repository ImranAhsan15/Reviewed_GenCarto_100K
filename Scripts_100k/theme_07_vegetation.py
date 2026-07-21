import arcpy
import traceback
import sys
from common_utils import *

def transfer_features_with_dict(fc_list, veg_transfer_veg_features):
    def resolve_fc(fc_name):
         matches = [fc for fc in fc_list if fc_name in fc]
         return matches[0] if matches else None
    for source_name, target_name in veg_transfer_veg_features.items():
        # Skip self-maps (optional, but usually sensible)
        if source_name == target_name:
            arcpy.AddMessage(f"Skipping '{source_name}' → '{target_name}' (same target).")
            continue

        source_fc = resolve_fc(source_name)
        target_fc = resolve_fc(target_name)

        if not source_fc:
            arcpy.AddWarning(f"Source feature class not found in fc_list: {source_name}")
            continue
        if not target_fc:
            arcpy.AddWarning(f"Target feature class not found in fc_list: {target_name}")
            continue

        try:
            if has_features(source_fc) > 0:
                arcpy.AddMessage(f"Transferring features from {source_name} to {target_name}")
                arcpy.management.Append(source_fc, target_fc, "NO_TEST")
                arcpy.management.DeleteFeatures(source_fc)
                arcpy.AddMessage(f"Deleted features from {source_name}")
            else:
                arcpy.AddMessage(f"No features found to transfer from {source_name} to {target_name}")
        except Exception as ex:
            arcpy.AddError(f"Transfer failed for {source_name} → {target_name}: {ex}")
            # decide whether to continue or raise; continuing is often nicer in batch runs
            continue
    return None


# Vegetation Generalization
def gen_vegetation(fc_list, val_dict, veg_lyrs_list, veg_transfer_veg_features, veg_field_values, working_gdb, logger):
    arcpy.AddMessage('Starting vegetation features generalization.....')
    arcpy.env.overwriteOutput = True
    try:
        input_fcs=[fc for a_lyr in veg_lyrs_list for fc in fc_list if str(a_lyr) in fc]
        transfer_features_with_dict(fc_list, veg_transfer_veg_features)
        
        field_cal_expr = [
            ([fc for fc in input_fcs if 'VA1030_Coconut_A' in fc][0], "VA1030"),
            ([fc for fc in input_fcs if 'VA1060_Oil_Palm_A' in fc][0], "VA1060"),
            ([fc for fc in input_fcs if 'VA9010_Sundry_Tree_A' in fc][0], "VA9010"),
            ([fc for fc in input_fcs if 'VA9020_Sundry_Non_Tree_A' in fc][0], "VA9020"),
            ([fc for fc in input_fcs if 'VA2060_Paddy_A' in fc][0], "VA2060"),
            ([fc for fc in input_fcs if 'VB3020_Rubber_Trees_A' in fc][0], "VB3020"),
            ([fc for fc in input_fcs if 'VB0000_Forest_A' in fc][0], "VB0000"),
            ([fc for fc in input_fcs if 'VC1110_Grass_A' in fc][0], "VC1110"),
            ([fc for fc in input_fcs if 'VC1100_Riung_A' in fc][0], "VC1100"),
            ([fc for fc in input_fcs if 'VC1090_Scrub_Shrub_A' in fc][0], "VC1090")]

        # Loop through the list and apply selection + calculation
        for fc, field_val in field_cal_expr:
            field_names = [field.name for field in arcpy.ListFields(fc)]
            if 'trace_fld' not in field_names:
                arcpy.management.AddFields(fc, [['trace_fld', 'TEXT', 'trace_fld', 255]])
            arcpy.management.CalculateField(in_table=fc, field='trace_fld', expression=f"'{field_val}'",
                                            expression_type="PYTHON3")
     
        spatial_ref = arcpy.Describe(input_fcs[0]).spatialReference
        created_fcs = arcpy.management.CreateFeatureclass(working_gdb, 'merged_fcs_new', 'POLYGON','','','',spatial_ref)
        arcpy.AddMessage('created feature')
       
        fields_to_add = []
        existing_field_names = set()

        reserved_fields = {val_dict['Reserve_Field1'], val_dict['Reserve_Field2'], val_dict['Reserve_Field3'], val_dict['Reserve_Field4']}

        # Loop through all input feature classes
        for fc in input_fcs:
            for field in arcpy.ListFields(fc):
                if field.type not in reserved_fields and field.name not in reserved_fields and field.name not in existing_field_names:
                    fields_to_add.append({
                        'name': field.name,
                        'type': field.type,
                        'precision': field.precision,
                        'scale': field.scale,
                        'length': field.length,
                        'alias': field.aliasName
                    })
                    existing_field_names.add(field.name)

        # Add each field individually to the created feature class
        for field_def in fields_to_add:
            arcpy.management.AddField(
                in_table=created_fcs,
                field_name=field_def['name'],
                field_type=field_def['type'],
                field_precision=field_def.get('precision'),
                field_scale=field_def.get('scale'),
                field_length=field_def.get('length'),
                field_alias=field_def.get('alias', ''),
                field_is_nullable='NULLABLE',
                field_is_required='NON_REQUIRED',
                field_domain=''
            )
  
        arcpy.AddMessage("All unique fields have been added to the feature class.")
        merged_fcs = arcpy.management.Append(input_fcs, created_fcs, 'NO_TEST')
        merge_feature_layer = arcpy.management.MakeFeatureLayer(merged_fcs, "merged_layer")
        layer_selection_clause = f"SHAPE_Area < {val_dict['Veg_minimum_area']}"
        selected_layer = arcpy.management.SelectLayerByAttribute(merge_feature_layer, "NEW_SELECTION", layer_selection_clause)
        eliminate_layer = arcpy.management.Eliminate(selected_layer, "V_Merge_Eliminate", "LENGTH")
        eliminate_part_feature = arcpy.management.EliminatePolygonPart(eliminate_layer, "V_Merge_Eliminate_Part", "AREA", f"{val_dict['Veg_eliminate_area']}" , None, "CONTAINED_ONLY")
        # dissolving the eliminate_part_feature 
        dissolve_feature = arcpy.analysis.PairwiseDissolve(eliminate_part_feature, "VegDissolve", ["trace_fld"])
        arcpy.AddMessage(f"Completed dissolve after elimination.")

        if arcpy.Exists(dissolve_feature):
            for fc in input_fcs:
                arcpy.management.DeleteFeatures(fc)
                arcpy.AddMessage(f"Deleted features from {fc}")
        
        # Loop through the list and apply selection + appending
        arcpy.AddMessage(f"Start Applying Selection and Appending")
        for fc, field_val in field_cal_expr:
            selected_dissolved_lyr = arcpy.management.SelectLayerByAttribute(in_layer_or_view=dissolve_feature,
                                                                             selection_type="NEW_SELECTION",
                                                                             where_clause=f"trace_fld = '{field_val}'")
            if has_features(selected_dissolved_lyr):
                veg_indiv_dissolve_layer = arcpy.management.MakeFeatureLayer(selected_dissolved_lyr, 'veg_indiv_dissolve_layer')
                arcpy.management.Append(veg_indiv_dissolve_layer, fc, "NO_TEST")

        arcpy.AddMessage(f"Start Assigning Feature Code")
        for fc, field_val in field_cal_expr:
            field_names = [field.name for field in arcpy.ListFields(fc)]
            if 'Feature_Code' in field_names:
                arcpy.management.CalculateField(in_table=fc, field='Feature_Code', expression=f"'{field_val}'",expression_type="PYTHON3")

        # Erase vegetation overlap
        enlarge_fcs_1 = [fc for a_lyr in [resolve_lyr().Grass_A, resolve_lyr().Riung_A, resolve_lyr().Scrub_Shrub_A] for fc in fc_list if str(a_lyr) in fc]
        erase_fcs_1 = [fc for a_lyr in [resolve_lyr().Coconut_A, resolve_lyr().Oil_Palm_A, resolve_lyr().Sundry_Tree_A,
                                        resolve_lyr().Sundry_Non_Tree_A, resolve_lyr().Paddy_A, resolve_lyr().Rubber_Trees_A, 
                                        resolve_lyr().Forest_A] for fc in fc_list if str(a_lyr) in fc]
        enlarge_fcs_2 = [fc for a_lyr in [resolve_lyr().Coconut_A, resolve_lyr().Oil_Palm_A, resolve_lyr().Sundry_Tree_A,
                                          resolve_lyr().Sundry_Non_Tree_A, resolve_lyr().Paddy_A] for fc in fc_list if str(a_lyr) in fc]
        erase_fcs_2 = [fc for a_lyr in [resolve_lyr().Rubber_Trees_A, resolve_lyr().Forest_A] for fc in fc_list if str(a_lyr) in fc]

        for fc in enlarge_fcs_1:
            erase_polygons_by_replace(fc, erase_fcs_1, None, working_gdb)
        for fc in enlarge_fcs_2:
            erase_polygons_by_replace(fc, erase_fcs_2, None, working_gdb)

            
        # Delete temp files
        temp_file = [eliminate_layer, eliminate_part_feature, dissolve_feature]
        arcpy.management.Delete(temp_file)
       
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Vegetation generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Vegetation generalisation', f'{exc_value}\n')