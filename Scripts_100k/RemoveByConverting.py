# Import required python modules
import arcpy

def ConvertEnclosed(primary_fc_lyr, secondary_fc_lyrs):
    """
    This section handles enclosed features that overlap other features (not overlap holes) using 'COMPLETELY_WITHIN' filter.
    """
    arcpy.AddMessage("Searching for features that are fully contained.")
    # Remove representation
    # common.update_geo_with_override(primary_fc_lyr)

    secondary_fc_names = []
    for layer in secondary_fc_lyrs:
        desc = arcpy.da.Describe(layer)
        secondary_fc_names.append(layer)

    delete_ids = []
    for i in range(len(secondary_fc_lyrs)):    # keep all secondary lists indices same
        surrounding_fc_lyr = secondary_fc_lyrs[i]
        # Remove representation
        # common.update_geo_with_override(surrounding_fc_lyr)

        # Get new selection to refresh selection set
        with arcpy.da.SearchCursor(primary_fc_lyr, ("OID@", "SHAPE@")) as cursor:
            for row in cursor:
                desc = arcpy.da.Describe(surrounding_fc_lyr)
                surround_name = desc['name']
                primary_OID = row[0]
                primary_geo = row[1]

                # Is the primary feature completely within surrounding feature?
                spatial_selected_feature = arcpy.management.SelectLayerByLocation(surrounding_fc_lyr, "COMPLETELY_CONTAINS", primary_geo)
                if int(arcpy.management.GetCount(spatial_selected_feature).getOutput(0)) > 0:
                    # Get ID of surrounding feature
                    surrounding_FID_set = [int(oid) for oid in arcpy.da.Describe(surrounding_fc_lyr)['FIDSet']]

                    # Create temp fc to store and process geometry
                    temp_contained_fc =  surround_name + "_removed_contained_poly_temp"
                    # Copy surrounding feature to temp FC
                    test_geom = arcpy.management.CopyFeatures(surrounding_fc_lyr, arcpy.Geometry())
                    arcpy.management.CopyFeatures(test_geom, temp_contained_fc)     # Cannot use arcpy.Geometry() as it fails in append_management
                    # Now select the feature from primary FC and append its geometry to a temp FC
                    arcpy.management.Append([primary_fc_lyr], temp_contained_fc , "NO_TEST")

                    # Select appended features (any OID > 1 , in this case) and eliminate
                    temp_contained_fc_lyr = temp_contained_fc + "lyr"
                    arcpy.management.MakeFeatureLayer(temp_contained_fc, temp_contained_fc_lyr)

                    new_features = arcpy.management.SelectLayerByAttribute(temp_contained_fc_lyr, "NEW_SELECTION", "OBJECTID > 1")
                    if int(arcpy.management.GetCount(new_features).getOutput(0)) > 0:
                        elim_contained_fc= arcpy.management.Eliminate(temp_contained_fc_lyr, arcpy.Geometry(), "AREA")

                        # Update secondary feature with new geometry
                        # Including another field 'NAM' in query as placeholder - else update not working
                        with arcpy.da.UpdateCursor(surrounding_fc_lyr, ["NAM","SHAPE@"],"OBJECTID = " + str(surrounding_FID_set[0])) as update_cursor:
                            for updtRow in update_cursor:
                                # Update with geometry from eliminate tool
                                updtRow = (updtRow[0], elim_contained_fc[0])
                                update_cursor.updateRow(updtRow)
                                arcpy.AddMessage("Updated contained geometry for ID {0} in {1}".format(str(surrounding_FID_set[0]), surround_name))

                    # Delete the original selected features from the primary FC - just to avoid confusion for later queries
                    delete_ids.append(str(primary_OID))

                    # Delete the temp FC
                    arcpy.management.Delete([temp_contained_fc, temp_contained_fc_lyr])

    if len(delete_ids) >= 1:
        # delete the original features
        where = "OBJECTID = "
        where += " OR OBJECTID = ".join(delete_ids)
        arcpy.management.SelectLayerByAttribute(primary_fc_lyr, "NEW_SELECTION", where)
        arcpy.management.DeleteFeatures(primary_fc_lyr)

def ConvertOverlapping(primary_fc_lyr, secondary_fc_lyrs, working_gdb):
    arcpy.AddMessage("Searching for features that overlap.")
    """
    This section handles enclosed features that overlap empty geometry/holes.
    """
    # All input FC for FeatureToLine_management
    input_layers = secondary_fc_lyrs
    input_layers.append(primary_fc_lyr)

    # Remove representation
    # common.update_geo_with_override(primary_fc_lyr)

    num_features = int(arcpy.management.GetCount(primary_fc_lyr).getOutput(0))

    if num_features > 0:
        arcpy.AddMessage(str(num_features) + " selected from input")
        # Create FeatureToLine for primary selected features and ALL secondary FC
        feature_to_line_fc = "feature_to_line_fc"
        arcpy.AddMessage("Running Feature to Line")
        scratch = working_gdb
        arcpy.AddMessage("Scratch: " + scratch)
        arcpy.management.FeatureToLine(input_layers, feature_to_line_fc, "", "ATTRIBUTES")

        desc = arcpy.da.Describe(primary_fc_lyr)
        primary_fc_name = desc['name']

        first_query_fields = []
        secondary_FID_fields = []
        secondary_fc_names = []
        for layer in secondary_fc_lyrs:
            # Remove representation
            # common.update_geo_with_override(layer)

            desc = arcpy.da.Describe(layer)
            fc_name = desc['name']
            secondary_fc_names.append(layer)
            secondary_FID_fields.append("FID_" + str(fc_name))
            first_query_fields.append("FID_" + str(fc_name))

        # Now get the FID field name
        primary_FID_field = "FID_" + str(primary_fc_name)

        if primary_FID_field in secondary_FID_fields:
            secondary_FID_fields.remove(primary_FID_field)
        # Create a Describe object from the GDB Feature Class
        desc = arcpy.da.Describe(feature_to_line_fc)
        shape_length = desc['lengthFieldName']

        # Get all unique primary IDs
        whereClause = primary_FID_field + " > -1 "
        first_primary_fc_IDS = [row[0] for row in arcpy.da.SearchCursor(feature_to_line_fc, [primary_FID_field], whereClause)]
        unique_primary_IDs = set(first_primary_fc_IDS)

        first_query_fields.append(shape_length)

        # Dictionary to store primary feat ID and secondary FC it should be appended to
        primary_feature_append = {}

        arcpy.AddMessage("Determining features to convert.")
        for uniq_primary_ID in unique_primary_IDs:
            postfix  = "ORDER BY " + shape_length + " DESC"
            whereClause = primary_FID_field + " = " + str(uniq_primary_ID)
            cnt = 0
            with arcpy.da.SearchCursor(feature_to_line_fc, first_query_fields, whereClause, sql_clause = (None, postfix) ) as cursor:
                # Get first row only
                for row in cursor:
                    index = []
                    for i in range(len(secondary_FID_fields)):    # Keep both lists indices same
                        # check which FID_.... field is > -1 and get its FID value , and the index postion of this FC in the secondaryFIDFields list
                        # say if FID_V_Forest_A has a value greater than -1 , then the primary feature should be appended with V_Forest_A
                        if row[i] > 0:
                            # Used index to determine if more than one secondary shares this line segment
                            index.append(i)

                    if len(index) == 1:
                        primary_feature_append[uniq_primary_ID] = [index[0], row[index[0]]]
                        break
                    if len(index) > 1:
                        arcpy.AddMessage("Shares boundary with multiple secondary")
                        break
                    cnt += 1

        for i in range(len(secondary_fc_lyrs)):
            secondary_lyr = secondary_fc_lyrs[i]

            # Dictionary to store primary feat ID and secondary FC ID it should be appended to
            OID_pairs = {}
            for uniq_primary_ID, values in primary_feature_append.items():
                if  values[0] == i:   # Index in both lists are same
                    OID_pairs[uniq_primary_ID] = values[1]    # Dictionary key=primaryFeatOID , value = secondary feat OID

            for uniq_primary_ID, secondary_ID in OID_pairs.items():
                # Create temp fc to store and process geometry
                temp_fc = f"{working_gdb}\\{primary_fc_name}_removePolygonsTmp"

                # Now select the feature from secondary FC and copy its geometry to a temp FC
                arcpy.management.SelectLayerByAttribute(secondary_lyr, "NEW_SELECTION", "OBJECTID = " + str(secondary_ID))
                arcpy.management.CopyFeatures(secondary_lyr, temp_fc)     # Cannot use arcpy.Geometry() as it fails in append_management

                # Now select the feature from primary FC and append its geometry to a temp FC
                arcpy.management.SelectLayerByAttribute(primary_fc_lyr, "NEW_SELECTION", "OBJECTID = " + str(uniq_primary_ID))
                arcpy.management.Append([primary_fc_lyr], temp_fc , "NO_TEST")

                # Delete the original selected features from the primary FC  - just to avoid confusion for later queries
                arcpy.management.DeleteFeatures(primary_fc_lyr)

                # Select appended features and eliminate
                temp_fc_lyr = temp_fc + "lyr"
                arcpy.management.MakeFeatureLayer(temp_fc, temp_fc_lyr)
                newFeatures = arcpy.management.SelectLayerByAttribute(temp_fc_lyr, "NEW_SELECTION", "OBJECTID > 1 ")

                if int(arcpy.management.GetCount(newFeatures).getOutput(0)) > 0:
                    elimFeat= arcpy.management.Eliminate(temp_fc_lyr, arcpy.Geometry(), "LENGTH")

                    # Update secondary feature with new geometry
                    # Including another field 'NAM' in query as placeholder - else update not working
                    with arcpy.da.UpdateCursor(secondary_lyr, ("SHAPE@", "OID@"),"OBJECTID=" + str(secondary_ID) ) as updateCursor:
                        for updtRow in updateCursor:
                            # Update with geometry from eliminate tool
                            updtRow[0] = elimFeat[0]
                            updateCursor.updateRow(updtRow)
                            arcpy.AddMessage("Updated geometry for ID {0} in {1}".format(str(secondary_ID), secondary_fc_names[i]))

                # Delete the temp FC
                arcpy.management.Delete([temp_fc_lyr, temp_fc, "in_memory\\featureToLineFC"])

                # Delete from dictionary as each feature is processed to speed up later iterations
                del primary_feature_append[uniq_primary_ID]