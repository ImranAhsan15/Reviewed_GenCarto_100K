import arcpy
import traceback
import sys
from common_utils import *

# # Loading Data into Enterprise Geodatabase
# # Obsolete on 01st March 2026
def load_data_into_edb(in_workspace, aoi_sheet, out_workspace, version, working_gdb, logger):
    arcpy.AddMessage('Starting Loading data into EDB.....')
    # Set environments
    arcpy.env.workspace = working_gdb
    arcpy.env.overwriteOutput = True
    try:
        desc = arcpy.da.Describe(aoi_sheet)
        aoi_name = desc['name']
        ident_field = "FID_" + aoi_name

        # Grab the AOI directly from the workspace
        aoi_lyr = arcpy.management.MakeFeatureLayer(aoi_sheet, "AOI_layer")
        
        desc = arcpy.da.Describe(out_workspace)
        wksp_type = desc['workspaceType']
        # Get required features and data
        fc_name_list, in_fcs_dict = get_fcs_load_data(in_workspace, wksp_type)
        out_fc_name_list, out_fcs_dict = get_fcs_load_data(out_workspace, wksp_type)
        split_list = split_fcs_load_data(working_gdb)
        
        # Determine which feature classes from the input are also in the output
        for fc_name in fc_name_list:
            if fc_name not in out_fc_name_list:
                # Remove the feature class if not in output
                fc_name_list.remove(fc_name)
                arcpy.AddMessage(fc_name + " feature class in ouput but not in input")
        # Sorted feature class list
        fc_name_list = sorted(fc_name_list)

        # Process each feature class from the input
        for name in fc_name_list:
            outfc = out_fcs_dict[name]
            arcpy.AddMessage("Processing " + name)
            arcpy.AddMessage(outfc)
            outlyr = name + "_lyr"
            if arcpy.Exists(outlyr):
                arcpy.management.Delete(outlyr)

            #----------------------------------------------
            # First split delete features from output database version
            # ----------------------------------------

            arcpy.management.MakeFeatureLayer(outfc, outlyr)
            if version:
                arcpy.management.ChangeVersion(outlyr, "TRANSACTIONAL", version)

            arcpy.management.SelectLayerByLocation(outlyr, "INTERSECT", aoi_lyr)
            count_out_lyr = int(arcpy.management.GetCount(outlyr)[0])
            # Get geo type
            desc = arcpy.da.Describe(outfc)
            geo_type = desc['shapeType']

            if count_out_lyr >= 1:
                # If the feature class is a line split the features and delete the parts inside
                if geo_type == "Polyline" and name in split_list:
                    split_fc = name + "_split"
                    arcpy.AddMessage("     Determining if features need to be split.")
                    # Split the features
                    arcpy.analysis.Identity(outlyr, aoi_lyr, split_fc, "ONLY_FID")
                    # Delete features
                    arcpy.management.DeleteFeatures(outlyr)
                    # Find only those features outside AOI and add back into output fc
                    query = f"{ident_field} = -1"
                    arcpy.management.MakeFeatureLayer(split_fc, "split_lyr", query)
                    count_split_fc = int(arcpy.management.GetCount("split_lyr")[0])

                    if count_split_fc >= 1:
                        arcpy.AddMessage("     Adding " + str(count_split_fc) + " split features")
                        arcpy.management.Append("split_lyr", outlyr, "NO_TEST")            
                else:
                    # If the feature class isn't a line, just delete the features.
                    arcpy.AddMessage("     Deleting " + str(count_out_lyr) + " features from output database.")
                    arcpy.management.DeleteFeatures(outlyr)

            #---------------------------------------
            # Then append features from the input to the output
            #---------------------------------------------------

            # Select the features from the input that intersect the AOI
            arcpy.AddMessage("     Checking for features to add")
            input_fc = in_fcs_dict[name]
            in_lyr = "inLyr"
            if arcpy.Exists(in_lyr):
                arcpy.management.Delete(in_lyr)
            # Make feature layer    
            in_lyr = arcpy.management.MakeFeatureLayer(input_fc, in_lyr)

            if geo_type == "Polyline" and name in split_list:
                    arcpy.management.SelectLayerByLocation(in_lyr, "CROSSED_BY_THE_OUTLINE_OF", aoi_lyr)
                    feat_count = int(arcpy.management.GetCount(in_lyr)[0])
                    if feat_count >= 1:
                        insplitfc = name + "_in_split"
                        arcpy.AddMessage("     Splitting features in input.")
                        # Split the features
                        arcpy.management.SelectLayerByLocation(in_lyr, "INTERSECT", aoi_lyr)
                        arcpy.analysis.Identity(in_lyr, aoi_lyr, insplitfc, "ONLY_FID")

                        # Find only those features outside AOI and add back into output fc
                        query = f"{ident_field} <> -1"
                        load_lyr = arcpy.management.MakeFeatureLayer(insplitfc, "in_split_lyr", query)
                    else:
                        arcpy.management.SelectLayerByLocation(in_lyr, "WITHIN", aoi_lyr)
                        load_lyr = in_lyr
            else:
                arcpy.management.SelectLayerByLocation(in_lyr, "INTERSECT", aoi_lyr)
                load_lyr = in_lyr

            count_load_lyr = int(arcpy.management.GetCount(load_lyr)[0])

            if count_load_lyr >= 1:
                arcpy.AddMessage("     Appending " + str(count_load_lyr) + " input features to output")
                arcpy.management.Append(load_lyr, outlyr, "NO_TEST")
                arcpy.AddMessage(arcpy.GetMessages())

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Load Data into Ent DB error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Load Data into Ent DB', f'{exc_value}\n')