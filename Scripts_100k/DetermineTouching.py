# Import required python module
import arcpy

def determine(input_lines, input_polygon, out_table, line_field, poly_field):
    poly_lyr = arcpy.management.MakeFeatureLayer(input_polygon, "poly_lyr")
    line_lyr = arcpy.management.MakeFeatureLayer(input_lines, "line_lyr")

    # Copy features to a temporary location
    temp_poly_lyr = arcpy.management.CopyFeatures(poly_lyr, 'in_memory\\temp_poly_lyr')
    # Integrate line features
    arcpy.management.Integrate([temp_poly_lyr, line_lyr])

    node_field = "Node_end"

    # Add fields for storing the OIDs of each input feature class
    fields = arcpy.ListFields(out_table)
    field_names = []
    for field in fields:
        field_names.append(field.name)

    if not line_field in field_names:
        arcpy.management.AddField(out_table, line_field, "LONG")
    if not poly_field in field_names:
        arcpy.management.AddField(out_table, poly_field, "LONG")
    if not node_field in field_names:
        arcpy.management.AddField(out_table, node_field, "Text")

    # Select just those lines that intersect polygons
    arcpy.management.SelectLayerByLocation(line_lyr, "INTERSECT", poly_lyr)
    arcpy.management.SelectLayerByLocation(line_lyr, "WITHIN", poly_lyr, "", "REMOVE_FROM_SELECTION")

    # If at least on line touches one polygon loop through all the line features
    count_line_lyr = int(arcpy.management.GetCount(line_lyr)[0])
    if count_line_lyr >= 1:
        near_tab = arcpy.analysis.GenerateNearTable(line_lyr, poly_lyr, "in_memory\\line_near_poly", "0 Meters", closest="ALL")
        line_to_poly = {}
        line_ids = []
        poly_ids = []
        with arcpy.da.SearchCursor(near_tab, ['IN_FID', 'NEAR_FID']) as n_cur:
            for n_row in n_cur:
                if n_row[0] in line_ids:
                    cur_ids = line_to_poly[n_row[0]]
                    cur_ids.append(n_row[1])
                    line_to_poly[n_row[0]] = cur_ids
                    poly_ids.append(n_row[1])
                else:
                    line_ids.append(n_row[0])
                    line_to_poly[n_row[0]] = [n_row[1]]
                    poly_ids.append(n_row[1])

        poly_ids = set(poly_ids)
        poly_geos = {}

        with arcpy.da.SearchCursor(poly_lyr, ['OID@', 'SHAPE@']) as s_cur:
            for s_row in s_cur:
                if s_row[0] in poly_ids:
                    poly_geos[s_row[0]] = s_row[1]

        # Open an insert cursor
        i_cursor = arcpy.da.InsertCursor(out_table, [line_field, poly_field, node_field])

        with arcpy.da.SearchCursor(line_lyr, ['OID@', 'SHAPE@']) as cursor:
            for row in cursor:
                geo = row[1]
                line_id = row[0]
                if line_id in line_ids:
                    poly_touches = line_to_poly[line_id]
                    start_pt = geo.firstPoint
                    end_pt = geo.lastPoint
                    for touch_id in poly_touches:
                        poly_geo = poly_geos[touch_id]
                        if not poly_geo.disjoint(start_pt):
                            # Add a record to the table
                            arcpy.AddMessage(f"Line feature {row[0]}) touches")
                            new_row = (row[0], touch_id, "start")
                            i_cursor.insertRow(new_row)
                        elif not poly_geo.disjoint(end_pt):
                            # Add a record to the table
                            arcpy.AddMessage(f"Line feature {row[0]}) touches")
                            new_row = (row[0], touch_id, "end")
                            i_cursor.insertRow(new_row)
        del i_cursor

