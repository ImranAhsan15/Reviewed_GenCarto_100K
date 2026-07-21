import arcpy
import pandas as pd
import sys
import traceback

def layer_grouping(map_name, excel_file, sheet_name, logger):

    try:
        aoi = None
        # Setup
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        map_obj = aprx.listMaps(map_name)[0]
        arcpy.AddMessage(f"Working on map: {map_obj.name}")

        # Read Excel
        df = pd.read_excel(excel_file, sheet_name)
        df = df.applymap(lambda x: x.strip() if isinstance(x, str) else x)


        # Build hierarchies
        outer_hierarchy = {}      # outer -> {top -> [subs]}
        no_outer_hierarchy = {}   # top-only or sub-only top-level
        layer_group_map = {}      # full path -> layers

        for _, row in df.iterrows():
            outer = row.get('Group_of_Group_of_Groups')
            top = row.get('Group_of_Groups')
            sub = row.get('Group_Name')
            layer = row.get('Layer_Name')

            if pd.isna(outer) and pd.isna(top) and pd.isna(sub) and pd.isna(layer):
                continue  # skip empty rows

            # Sub only → top-level
            if pd.isna(outer) and pd.isna(top) and not pd.isna(sub):
                if sub not in no_outer_hierarchy:
                    no_outer_hierarchy[sub] = []

            # Top + Sub (no Outer)
            elif pd.isna(outer) and not pd.isna(top) and not pd.isna(sub):
                if top not in no_outer_hierarchy:
                    no_outer_hierarchy[top] = []
                if sub not in no_outer_hierarchy[top]:
                    no_outer_hierarchy[top].append(sub)

            # Outer + Sub (no Top)
            elif not pd.isna(outer) and pd.isna(top) and not pd.isna(sub):
                if outer not in outer_hierarchy:
                    outer_hierarchy[outer] = {}
                if "_direct" not in outer_hierarchy[outer]:
                    outer_hierarchy[outer]["_direct"] = []
                if sub not in outer_hierarchy[outer]["_direct"]:
                    outer_hierarchy[outer]["_direct"].append(sub)

            # Outer only
            elif not pd.isna(outer) and pd.isna(top) and pd.isna(sub):
                if outer not in outer_hierarchy:
                    outer_hierarchy[outer] = {}

            # Outer + Top + Sub
            elif not pd.isna(outer) and not pd.isna(top) and not pd.isna(sub):
                if outer not in outer_hierarchy:
                    outer_hierarchy[outer] = {}
                if top not in outer_hierarchy[outer]:
                    outer_hierarchy[outer][top] = []
                if sub not in outer_hierarchy[outer][top]:
                    outer_hierarchy[outer][top].append(sub)

            # Map layers to full path
            if not pd.isna(layer):
                if pd.isna(outer) and pd.isna(top) and not pd.isna(sub):
                    key = f"{sub}"  # Sub only → top-level
                elif pd.isna(outer) and not pd.isna(top) and not pd.isna(sub):
                    key = f"{top}\\{sub}"
                elif not pd.isna(outer) and pd.isna(top) and not pd.isna(sub):
                    key = f"{outer}\\{sub}"
                elif not pd.isna(outer) and pd.isna(top) and pd.isna(sub):
                    key = f"{outer}"   # Outer only → direct layer
                elif not pd.isna(outer) and not pd.isna(top) and not pd.isna(sub):
                    key = f"{outer}\\{top}\\{sub}"
                else:
                    key = None

                if key:
                    if key not in layer_group_map:
                        layer_group_map[key] = []
                    layer_group_map[key].append(layer)

        # Function to create or fetch group layers
        def get_or_create_group(group_path):
            parent = None
            full_path = []
            for name in group_path:
                full_path.append(name)
                path_key = "\\".join(full_path)
                existing = [lyr for lyr in map_obj.listLayers() if lyr.isGroupLayer and lyr.name == name and (not parent or lyr.longName.startswith(parent.longName))]
                if existing:
                    grp = existing[0]
                else:
                    grp = map_obj.createGroupLayer(name, parent)
                parent = grp
            return parent

        # Build group structure
        # Outer groups first
        for outer, tops in outer_hierarchy.items():
            outer_grp = get_or_create_group([outer])
            if "_direct" in tops:
                for sub in tops["_direct"]:
                    get_or_create_group([outer, sub])
            for top, subs in tops.items():
                if top == "_direct": continue
                top_grp = get_or_create_group([outer, top])
                for sub in subs:
                    get_or_create_group([outer, top, sub])

        # Top-level groups (Top + Sub or Sub-only)
        for top, subs in no_outer_hierarchy.items():
            top_grp = get_or_create_group([top])
            for sub in subs:
                get_or_create_group([top, sub])

        # Move layers into groups
        all_layers = map_obj.listLayers()
        for row in df.itertuples(index=False):
            outer, top, sub, layer_name = row[:4]

            # Build the deepest available group path
            if pd.notna(sub):
                if pd.notna(outer) and pd.notna(top):
                    group_path = [outer, top, sub]
                elif pd.notna(top):
                    group_path = [top, sub]
                elif pd.notna(outer):
                    group_path = [outer, sub]
                else:
                    group_path = [sub]  # only sub
            elif pd.notna(top):
                if pd.notna(outer):
                    group_path = [outer, top]
                else:
                    group_path = [top]
            elif pd.notna(outer):
                group_path = [outer]
            else:
                group_path = []  # no grouping at all

            if not group_path:
                continue

            # Ensure the group exists
            target_group = get_or_create_group(group_path)

            # Find the layer and move it
            lyr = next((l for l in all_layers if l.name == layer_name and not l.isGroupLayer), None)
            if lyr:
                map_obj.addLayerToGroup(target_group, lyr)
                group_path_str = "\\".join(group_path)  # build string separately

        # Delete top-level layers outside any group (except AOI)
        layers_to_delete = [
            lyr for lyr in all_layers
            if not lyr.isGroupLayer
            and len(lyr.longName.split("\\")) == 1
            and lyr.name.upper() != "AOI"
        ]
        for lyr in layers_to_delete:
            map_obj.removeLayer(lyr)
            
        for lyr in map_obj.listLayers():
            if lyr.name.upper() == "AOI":
                aoi = lyr
            elif lyr.name.upper() == "UTILITIES_A":
                utilities_a = lyr

        # Move Utilities_A before AOI if both exist
        if aoi and utilities_a:
            map_obj.moveLayer(utilities_a, aoi, "BEFORE")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'layer grouping error: {e}\nTraceback details:\n{tb}'
        arcpy.AddError(error_message)
        logger.error(error_message)

def reorder_group_layers(map_name, excel_file, sheet_name, logger):
    """
    Reorder group layers in a map based on order provided in an Excel sheet column named 'Layer_Orders'.
    After reordering, move the 'AOI' feature layer to the top.

    Parameters:
    map_name (str): Name of the map in the current ArcGIS Pro project.
    excel_file (str): Full path to the Excel file containing layer order.
    sheet_name (str): Name of the sheet in the Excel file with 'Layer_Orders' column.
    """

    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        m = aprx.listMaps(map_name)[0]

        # Read Excel
        df = pd.read_excel(excel_file, sheet_name=sheet_name)
        if "Layer_Orders" not in df.columns:
            return
        desired_order = df["Layer_Orders"].dropna().astype(str).tolist()

        # Collect group layers
        group_layers = [lyr for lyr in m.listLayers() if lyr.isGroupLayer]
        group_layer_dict = {lyr.name: lyr for lyr in group_layers}

        moved = []
        reference_layer = None  # start from top of TOC

        # Reorder group layers
        for group_name in desired_order:
            if group_name in group_layer_dict:
                layer_to_move = group_layer_dict[group_name]
                if reference_layer:
                    m.moveLayer(reference_layer, layer_to_move, "AFTER")
                else:
                    top_layer = m.listLayers()[0]
                    if top_layer != layer_to_move:
                        m.moveLayer(top_layer, layer_to_move, "BEFORE")
                reference_layer = layer_to_move
                moved.append(group_name)

        # Move AOI feature layer to the very top
        feature_layers = [lyr for lyr in m.listLayers() if not lyr.isGroupLayer]
        aoi_layer = [lyr for lyr in feature_layers if lyr.name.upper() == "AOI"]
        if aoi_layer:
            top_layer = m.listLayers()[0]
            if top_layer != aoi_layer[0]:
                m.moveLayer(top_layer, aoi_layer[0], "BEFORE")
            arcpy.AddMessage("'AOI' feature layer moved to the top")
        else:
            arcpy.AddMessage("'AOI' layer not found")

        aprx.save()
        arcpy.AddMessage("Group layers reordered and project saved.")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f'Reorder group layer error: {e}\nTraceback details:\n{tb}'
        arcpy.AddError(error_message)
        logger.error(error_message)

def wipe_map(m):
    # Remove standalone tables (if any)
    for tbl in list(m.listTables()):
        try:
            m.removeTable(tbl)
        except Exception as e:
            arcpy.AddWarning(f"  Could not remove table '{tbl.name}': {e}")
    # Remove all top-level layers (groups & non-groups). 
    # Removing a group layer automatically removes all its children (sub-groups, sublayers).
    top_layers = list(m.listLayers())
    arcpy.AddMessage("Removing layers")
    for lyr in top_layers:
        try:
            m.removeLayer(lyr)
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            tb = traceback.format_exc()
            error_message = f'reorder group layer error: {e}\nTraceback details:\n{tb}'
            arcpy.AddError(error_message)

def clear_map_contents(map_name):
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    # Run for a specific map or all maps
    if map_name:
        maps = [aprx.listMaps(map_name)[0]]
    else:
        maps = aprx.listMaps()
    for m in maps:
        wipe_map(m)
