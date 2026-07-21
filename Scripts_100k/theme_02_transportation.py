import arcpy
import traceback
import sys
from common_utils import *
import re
import os
import time


# -*- coding: utf-8 -*-
"""
Fast global priority-aware geometry-only road thinning.

FINAL BEHAVIOUR
---------------
1. The user supplies only the original road layer and its existing hierarchy
   field; no separate hierarchy-value parameter is required.
2. Smaller hierarchy numbers are treated as more important. Larger hierarchy
   numbers are considered for hiding first.
3. Hierarchy affects decisions only when hierarchy values differ:
      - between hierarchy 4 and 5, hierarchy 5 is considered first;
      - between hierarchy 1 and 5, hierarchy 5 is considered first;
      - between equal values (4/4, 5/5, 1/1), hierarchy gives no preference.
4. Equal-hierarchy routes are ranked by same-class hiding continuity, low edge
   betweenness centrality, and route length.
5. A route may be hidden only when a valid alternative path remains using
   routes of equal or higher importance. A lower-priority road can never be
   used to justify hiding a higher-priority road.
6. Connectivity, dangle, maximum-length, detour, closed-loop, and small-
   component protections are retained.
7. The target-removal percentage is global across all hierarchy classes.
8. No road feature is deleted, split, copied back, or recreated. All original
   geometry and attributes are retained.
9. Only the numeric Invisibility field is updated:
      1 = hidden
      0 = visible
10. No final output feature class is created.

The only materialized intermediate is one planar feature class in ArcGIS Pro's
memory workspace. It is required to split roads at real intersections. No
prepared planar dataset and no source-ID field are required from the user.

Recommended definition query:
    Invisibility <> 1

Dependencies:
- ArcGIS Pro / ArcPy
- NetworkX
"""

import heapq
import math
import os
import traceback
import uuid
from collections import defaultdict
import arcpy
import networkx as nx


arcpy.env.overwriteOutput = True


# -----------------------------------------------------------------------------
# Messages and input validation
# -----------------------------------------------------------------------------

def add_message(message):
    arcpy.AddMessage(str(message))
    print(message)


def add_warning(message):
    arcpy.AddWarning(str(message))
    print(f"WARNING: {message}")


def resolve_source_feature_class(input_roads):
    """Resolve a feature layer to its underlying editable feature class."""

    description = arcpy.Describe(input_roads)
    catalog_path = getattr(description, "catalogPath", None)

    if catalog_path and arcpy.Exists(catalog_path):
        return catalog_path

    return input_roads


def validate_metric_polyline(feature_class):
    """Require projected polyline data with metres as the linear unit."""

    if not arcpy.Exists(feature_class):
        raise ValueError(f"Input roads do not exist: {feature_class}")

    description = arcpy.Describe(feature_class)

    if description.shapeType.lower() != "polyline":
        raise ValueError("The input dataset must be a polyline feature class or layer.")

    spatial_reference = description.spatialReference

    if not spatial_reference or spatial_reference.type != "Projected":
        raise ValueError("The input roads must use a projected coordinate system.")

    unit_name = (spatial_reference.linearUnitName or "").lower()

    if "meter" not in unit_name and "metre" not in unit_name:
        raise ValueError("The input roads must use metres as the linear unit.")

    return description


def find_field(feature_class, requested_name):
    """Return a field object by case-insensitive field-name matching."""

    requested_upper = requested_name.upper()

    for field in arcpy.ListFields(feature_class):
        if field.name.upper() == requested_upper:
            return field

    return None


def validate_hierarchy_field(feature_class, requested_field_name):
    """Validate and return the existing numeric hierarchy field."""

    requested_field_name = (requested_field_name or "").strip()

    if not requested_field_name:
        raise ValueError("A hierarchy field must be provided.")

    field = find_field(feature_class, requested_field_name)

    if field is None:
        raise ValueError(
            f"Hierarchy field '{requested_field_name}' was not found in the input roads."
        )

    numeric_types = {
        "SmallInteger",
        "Integer",
        "BigInteger",
        "Single",
        "Double",
    }

    if field.type not in numeric_types:
        raise TypeError(
            f"Hierarchy field '{field.name}' must be numeric. This script uses "
            "the rule: smaller hierarchy number = higher road importance."
        )

    return field


def ensure_numeric_invisibility_field(feature_class, requested_field_name):
    """Create the invisibility field when missing, or validate the existing one."""

    requested_field_name = (requested_field_name or "Invisibility").strip()
    existing_field = find_field(feature_class, requested_field_name)

    if existing_field is None:
        workspace = os.path.dirname(arcpy.Describe(feature_class).catalogPath)
        validated_name = arcpy.ValidateFieldName(requested_field_name, workspace)

        if validated_name.upper() != requested_field_name.upper():
            add_warning(
                f"Field name '{requested_field_name}' is invalid for this source. "
                f"Using '{validated_name}' instead."
            )

        add_message(f"Adding numeric field: {validated_name}")
        arcpy.management.AddField(feature_class, validated_name, "SHORT")

        created_field = find_field(feature_class, validated_name)
        if created_field is None:
            raise RuntimeError(
                f"The invisibility field could not be created: {validated_name}"
            )

        return created_field.name

    numeric_types = {
        "SmallInteger",
        "Integer",
        "BigInteger",
        "Single",
        "Double",
    }

    if existing_field.type not in numeric_types:
        raise TypeError(
            f"Field '{existing_field.name}' exists but is {existing_field.type}. "
            "Use a numeric field for 0 and 1 values."
        )

    if not existing_field.editable:
        raise ValueError(
            f"Field '{existing_field.name}' exists but is not editable."
        )

    return existing_field.name


def normalize_hierarchy_value(value, field_type):
    """Normalize a hierarchy category to a finite Python numeric value."""

    if value is None:
        return None

    integer_types = {"SmallInteger", "Integer", "BigInteger"}

    try:
        if field_type in integer_types:
            numeric_value = float(value)
            if not numeric_value.is_integer():
                return None
            return int(numeric_value)

        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            return None

        # Hierarchy values are categories. Rounding avoids insignificant binary
        # floating-point differences creating duplicate hierarchy categories.
        return round(numeric_value, 12)

    except (TypeError, ValueError, OverflowError):
        return None


def collect_source_hierarchies(source_feature_class, hierarchy_field):
    """Map original ObjectIDs to hierarchy values and list all unique classes."""

    oid_field = arcpy.Describe(source_feature_class).OIDFieldName
    source_hierarchy = {}
    hierarchy_counts = defaultdict(int)
    invalid_count = 0
    total_count = 0

    with arcpy.da.SearchCursor(
        source_feature_class,
        [oid_field, hierarchy_field.name],
    ) as cursor:
        for object_id, raw_hierarchy in cursor:
            total_count += 1
            source_id = str(object_id)
            hierarchy_value = normalize_hierarchy_value(
                raw_hierarchy,
                hierarchy_field.type,
            )

            source_hierarchy[source_id] = hierarchy_value

            if hierarchy_value is None:
                invalid_count += 1
            else:
                hierarchy_counts[hierarchy_value] += 1

    hierarchy_values = sorted(hierarchy_counts)

    if not hierarchy_values:
        raise ValueError(
            f"Hierarchy field '{hierarchy_field.name}' contains no valid numeric values."
        )

    add_message(
        "Hierarchy priority rule: smaller value = higher road importance."
    )
    add_message(
        "Hierarchy classes found automatically: "
        + ", ".join(str(value) for value in hierarchy_values)
    )

    for value in hierarchy_values:
        add_message(
            f"  {hierarchy_field.name} = {value}: "
            f"{hierarchy_counts[value]:,} original roads"
        )

    if invalid_count:
        add_warning(
            f"{invalid_count:,} of {total_count:,} roads have null or invalid "
            "hierarchy values. They will remain visible and will not participate "
            "in thinning decisions."
        )

    return source_hierarchy, hierarchy_values


# -----------------------------------------------------------------------------
# Minimum planar preparation: one layer view + one memory feature class
# -----------------------------------------------------------------------------

def create_lightweight_analysis_layer(source_feature_class):
    """Create a nonmaterialized layer view with unnecessary fields hidden."""

    description = arcpy.Describe(source_feature_class)
    oid_name = description.OIDFieldName.upper()
    shape_name = description.shapeFieldName.upper()
    field_info = arcpy.FieldInfo()

    for field in arcpy.ListFields(source_feature_class):
        visibility = (
            "VISIBLE"
            if field.name.upper() in {oid_name, shape_name}
            else "HIDDEN"
        )
        field_info.addField(field.name, field.name, visibility, "NONE")

    layer_name = f"roadthin_view_{uuid.uuid4().hex[:10]}"

    arcpy.management.MakeFeatureLayer(
        in_features=source_feature_class,
        out_layer=layer_name,
        field_info=field_info,
    )

    return layer_name


def find_generated_source_fid_field(planar_feature_class, source_feature_class):
    """Find the FID_<input> field generated by Feature To Line."""

    source_field_names = {
        field.name.upper()
        for field in arcpy.ListFields(source_feature_class)
    }
    planar_oid_name = arcpy.Describe(planar_feature_class).OIDFieldName.upper()
    integer_types = {"SmallInteger", "Integer", "BigInteger"}
    preferred_candidates = []
    fallback_candidates = []

    for field in arcpy.ListFields(planar_feature_class):
        name_upper = field.name.upper()

        if name_upper == planar_oid_name or field.type not in integer_types:
            continue

        if name_upper.startswith("FID_"):
            fallback_candidates.append(field.name)

            if name_upper not in source_field_names:
                preferred_candidates.append(field.name)

    if len(preferred_candidates) == 1:
        return preferred_candidates[0]

    if not preferred_candidates and len(fallback_candidates) == 1:
        return fallback_candidates[0]

    candidates = preferred_candidates or fallback_candidates

    for candidate in candidates:
        delimited = arcpy.AddFieldDelimiters(planar_feature_class, candidate)

        with arcpy.da.SearchCursor(
            planar_feature_class,
            [candidate],
            where_clause=f"{delimited} >= 0",
        ) as cursor:
            if next(cursor, None) is not None:
                return candidate

    raise RuntimeError(
        "Feature To Line did not produce an identifiable FID_<input> field. "
        "The original-road relationship cannot be established safely."
    )


def create_planar_network(source_feature_class):
    """Create the one planar intermediate required for graph construction."""

    analysis_layer = create_lightweight_analysis_layer(source_feature_class)
    planar_fc = rf"memory\roadthin_planar_{uuid.uuid4().hex[:10]}"

    add_message(
        "Splitting roads at intersections in memory; no prepared dataset is required..."
    )

    arcpy.management.FeatureToLine(
        in_features=[analysis_layer],
        out_feature_class=planar_fc,
        cluster_tolerance=None,
        attributes="ATTRIBUTES",
    )

    source_fid_field = find_generated_source_fid_field(
        planar_feature_class=planar_fc,
        source_feature_class=source_feature_class,
    )

    return planar_fc, source_fid_field


# -----------------------------------------------------------------------------
# Graph construction
# -----------------------------------------------------------------------------

def node_key(point, tolerance):
    """Convert an endpoint coordinate into a stable graph-node key."""

    return (
        int(round(point.X / tolerance)),
        int(round(point.Y / tolerance)),
    )


def graph_degree(node, node_edges, edge_nodes):
    """Return undirected degree, counting a self-loop twice."""

    incident_edges = node_edges.get(node, ())
    degree_value = len(incident_edges)

    for edge_id in incident_edges:
        start_node, end_node = edge_nodes[edge_id]
        if start_node == node and end_node == node:
            degree_value += 1

    return degree_value


def other_node(edge_id, current_node, edge_nodes):
    """Return the opposite endpoint of an edge."""

    start_node, end_node = edge_nodes[edge_id]

    if start_node == current_node:
        return end_node

    if end_node == current_node:
        return start_node

    raise RuntimeError("The supplied node is not incident to the supplied edge.")


def normalize_source_id(value):
    """Normalize source ObjectIDs for set and dictionary matching."""

    if value in (None, "", -1, "-1"):
        return None

    try:
        return str(int(value))
    except (TypeError, ValueError, OverflowError):
        text = str(value).strip()
        return text if text and text != "-1" else None


def read_planar_segments(
    planar_feature_class,
    source_fid_field,
    source_hierarchy,
    node_tolerance,
):
    """Read geometry, source IDs, and hierarchy values in one cursor pass."""

    edge_nodes = {}
    edge_lengths = {}
    edge_source_ids = {}
    edge_hierarchies = {}
    node_edges = defaultdict(set)
    invalid_geometry_count = 0
    unmapped_count = 0

    fields = [
        "OID@",
        "SHAPE@",
        "SHAPE@LENGTH",
        source_fid_field,
    ]

    with arcpy.da.SearchCursor(planar_feature_class, fields) as cursor:
        for segment_oid, geometry, length, raw_source_id in cursor:
            if (
                geometry is None
                or geometry.pointCount <= 0
                or geometry.partCount <= 0
                or length is None
                or length <= 0
            ):
                invalid_geometry_count += 1
                continue

            first_point = geometry.firstPoint
            last_point = geometry.lastPoint

            if first_point is None or last_point is None:
                invalid_geometry_count += 1
                continue

            source_id = normalize_source_id(raw_source_id)
            start_node = node_key(first_point, node_tolerance)
            end_node = node_key(last_point, node_tolerance)

            edge_nodes[segment_oid] = (start_node, end_node)
            edge_lengths[segment_oid] = float(length)
            edge_source_ids[segment_oid] = source_id
            edge_hierarchies[segment_oid] = (
                source_hierarchy.get(source_id)
                if source_id is not None
                else None
            )

            node_edges[start_node].add(segment_oid)
            node_edges[end_node].add(segment_oid)

            if source_id is None:
                unmapped_count += 1

    if not edge_nodes:
        raise ValueError("No valid line segments were created by Feature To Line.")

    if invalid_geometry_count:
        add_warning(
            f"Skipped {invalid_geometry_count:,} invalid or zero-length segments."
        )

    if unmapped_count:
        add_warning(
            f"{unmapped_count:,} planar segments could not be linked to an original "
            "road. They will remain protected from hiding."
        )

    return (
        edge_nodes,
        edge_lengths,
        edge_source_ids,
        edge_hierarchies,
        node_edges,
    )


def route_boundary_nodes(edge_nodes, edge_hierarchies, node_edges):
    """
    Identify route boundaries.

    In addition to ordinary junctions, a node is a boundary when the hierarchy
    changes. This guarantees that one route never combines different hierarchy
    classes.
    """

    boundaries = set()

    for node, incident_edges in node_edges.items():
        if graph_degree(node, node_edges, edge_nodes) != 2:
            boundaries.add(node)
            continue

        incident_hierarchies = {
            edge_hierarchies.get(edge_id)
            for edge_id in incident_edges
        }

        if len(incident_hierarchies) != 1 or None in incident_hierarchies:
            boundaries.add(node)

    return boundaries


def build_hierarchy_routes(
    edge_nodes,
    edge_lengths,
    edge_hierarchies,
    node_edges,
):
    """Group atomic segments into same-hierarchy routes between boundaries."""

    boundary_nodes = route_boundary_nodes(
        edge_nodes=edge_nodes,
        edge_hierarchies=edge_hierarchies,
        node_edges=node_edges,
    )

    visited_edges = set()
    routes = {}
    edge_to_route = {}
    next_route_id = 1
    maximum_steps = len(edge_nodes) + 1

    def store_route(route_edges, start_node, end_node):
        nonlocal next_route_id

        if not route_edges:
            return

        hierarchy_set = {
            edge_hierarchies.get(edge_id)
            for edge_id in route_edges
        }

        route_hierarchy = (
            next(iter(hierarchy_set))
            if len(hierarchy_set) == 1 and None not in hierarchy_set
            else None
        )

        route_length = sum(edge_lengths[edge_id] for edge_id in route_edges)

        routes[next_route_id] = {
            "route_id": next_route_id,
            "edges": tuple(route_edges),
            "start": start_node,
            "end": end_node,
            "length": float(route_length),
            "closed": start_node == end_node,
            "hierarchy": route_hierarchy,
        }

        for edge_id in route_edges:
            edge_to_route[edge_id] = next_route_id

        next_route_id += 1

    # Routes beginning at a junction, dead end, or hierarchy-change node.
    for start_node in sorted(boundary_nodes):
        for starting_edge in sorted(node_edges[start_node]):
            if starting_edge in visited_edges:
                continue

            route_edges = []
            current_node = start_node
            current_edge = starting_edge
            end_node = start_node
            safety_counter = 0

            while True:
                safety_counter += 1

                if safety_counter > maximum_steps:
                    raise RuntimeError("Unexpected cycle while constructing routes.")

                if current_edge in visited_edges:
                    end_node = current_node
                    break

                visited_edges.add(current_edge)
                route_edges.append(current_edge)

                next_node = other_node(current_edge, current_node, edge_nodes)
                end_node = next_node

                if next_node in boundary_nodes:
                    break

                possible_edges = [
                    edge_id
                    for edge_id in node_edges[next_node]
                    if edge_id != current_edge
                ]

                unvisited_edges = [
                    edge_id
                    for edge_id in possible_edges
                    if edge_id not in visited_edges
                ]

                if not unvisited_edges:
                    break

                current_node = next_node
                current_edge = min(unvisited_edges)

            store_route(route_edges, start_node, end_node)

    # Remaining components are closed same-hierarchy cycles.
    for starting_edge in sorted(edge_nodes):
        if starting_edge in visited_edges:
            continue

        start_node = edge_nodes[starting_edge][0]
        current_node = start_node
        current_edge = starting_edge
        route_edges = []
        end_node = start_node
        safety_counter = 0

        while True:
            safety_counter += 1

            if safety_counter > maximum_steps:
                raise RuntimeError("Unexpected closed-cycle traversal failure.")

            if current_edge in visited_edges:
                end_node = current_node
                break

            visited_edges.add(current_edge)
            route_edges.append(current_edge)

            next_node = other_node(current_edge, current_node, edge_nodes)
            end_node = next_node

            possible_edges = [
                edge_id
                for edge_id in node_edges[next_node]
                if edge_id != current_edge
            ]
            unvisited_edges = [
                edge_id
                for edge_id in possible_edges
                if edge_id not in visited_edges
            ]

            if next_node == start_node and not unvisited_edges:
                break

            if not unvisited_edges:
                break

            current_node = next_node
            current_edge = min(unvisited_edges)

        store_route(route_edges, start_node, end_node)

    return routes, edge_to_route


def make_route_graph(routes, route_ids):
    """Create a NetworkX MultiGraph for the supplied active route IDs."""

    graph = nx.MultiGraph()

    for route_id in route_ids:
        route = routes[route_id]
        graph.add_edge(
            route["start"],
            route["end"],
            key=route_id,
            route_id=route_id,
            length=max(route["length"], 0.000001),
        )

    return graph


def lookup_centrality(centrality, start_node, end_node, route_id):
    """Read undirected edge centrality regardless of edge orientation."""

    forward_key = (start_node, end_node, route_id)
    reverse_key = (end_node, start_node, route_id)

    if forward_key in centrality:
        return float(centrality[forward_key])

    if reverse_key in centrality:
        return float(centrality[reverse_key])

    return 0.0


# -----------------------------------------------------------------------------
# Global hierarchy-priority route selection
# -----------------------------------------------------------------------------

def calculate_component_route_counts(graph):
    """Map every node to its original connected component's route count."""

    node_component_size = {}

    for component_nodes in nx.connected_components(graph):
        route_count = graph.subgraph(component_nodes).number_of_edges()

        for node in component_nodes:
            node_component_size[node] = route_count

    return node_component_size


def hierarchy_is_allowed_as_alternative(route_hierarchy, candidate_hierarchy):
    """
    Return True when a route may support removal of the candidate route.

    Null/invalid hierarchy routes are protected and may remain in an
    alternative path. For valid classes, only equal or more-important routes
    are allowed, where a smaller number means greater importance.
    """

    return (
        route_hierarchy is None
        or route_hierarchy <= candidate_hierarchy
    )


def priority_degree(route_graph, routes, node, candidate_hierarchy):
    """Degree at a node using only equal/higher-priority visible routes."""

    if node not in route_graph:
        return 0

    degree_value = 0

    for neighbour, keyed_edges in route_graph.adj[node].items():
        for route_id in keyed_edges:
            route_hierarchy = routes[route_id]["hierarchy"]

            if not hierarchy_is_allowed_as_alternative(
                route_hierarchy,
                candidate_hierarchy,
            ):
                continue

            degree_value += 2 if neighbour == node else 1

    return degree_value


def priority_shortest_path_length(
    route_graph,
    routes,
    source_node,
    target_node,
    candidate_hierarchy,
):
    """
    Dijkstra shortest-path length using only equal/higher-priority routes.

    A custom filtered search avoids repeatedly constructing NetworkX subgraph
    views for every candidate and is faster for large road networks.
    """

    if source_node == target_node:
        return 0.0

    if source_node not in route_graph or target_node not in route_graph:
        raise nx.NodeNotFound

    distances = {source_node: 0.0}
    queue = [(0.0, source_node)]

    while queue:
        current_distance, current_node = heapq.heappop(queue)

        if current_distance != distances.get(current_node):
            continue

        if current_node == target_node:
            return current_distance

        for neighbour, keyed_edges in route_graph.adj[current_node].items():
            best_parallel_weight = None

            for route_id, attributes in keyed_edges.items():
                route_hierarchy = routes[route_id]["hierarchy"]

                if not hierarchy_is_allowed_as_alternative(
                    route_hierarchy,
                    candidate_hierarchy,
                ):
                    continue

                edge_weight = float(attributes.get("length", 1.0))

                if (
                    best_parallel_weight is None
                    or edge_weight < best_parallel_weight
                ):
                    best_parallel_weight = edge_weight

            if best_parallel_weight is None:
                continue

            new_distance = current_distance + best_parallel_weight

            if new_distance < distances.get(neighbour, float("inf")):
                distances[neighbour] = new_distance
                heapq.heappush(queue, (new_distance, neighbour))

    raise nx.NetworkXNoPath


def build_route_node_index(routes):
    """Map route endpoints to the routes incident at those endpoints."""

    node_routes = defaultdict(set)

    for route_id, route in routes.items():
        node_routes[route["start"]].add(route_id)
        node_routes[route["end"]].add(route_id)

    return node_routes


def select_routes_global_priority(
    routes,
    target_removal_percent,
    max_removable_route_length,
    max_detour_ratio,
    centrality_sample_nodes,
    min_component_routes,
):
    """
    Select the maximum safe road length up to the global target.

    Candidate ordering is:
      1. larger hierarchy value first (lower importance),
      2. more adjacent already-hidden routes of the same hierarchy,
      3. lower edge betweenness centrality,
      4. shorter route,
      5. stable route ID.

    When hierarchy values are equal, hierarchy itself supplies no preference.
    The continuity, centrality, geometry, and safety rules decide which route is
    hidden. A candidate can be removed only if its endpoints remain connected
    through routes with hierarchy <= candidate hierarchy (or protected null
    hierarchy routes).
    """

    candidate_route_ids = {
        route_id
        for route_id, route in routes.items()
        if route["hierarchy"] is not None
    }

    if not candidate_route_ids or target_removal_percent <= 0:
        return set()

    all_route_ids = set(routes)
    route_graph = make_route_graph(routes, all_route_ids)
    node_count = route_graph.number_of_nodes()

    if node_count == 0:
        return set()

    sample_count = None

    if centrality_sample_nodes and node_count > centrality_sample_nodes:
        sample_count = int(centrality_sample_nodes)

    add_message(
        "Calculating global geometry-based importance for "
        f"{route_graph.number_of_edges():,} routes..."
    )

    centrality = nx.edge_betweenness_centrality(
        route_graph,
        k=sample_count,
        normalized=True,
        weight="length",
        seed=42,
    )

    route_centrality = {
        route_id: lookup_centrality(
            centrality,
            routes[route_id]["start"],
            routes[route_id]["end"],
            route_id,
        )
        for route_id in candidate_route_ids
    }
    del centrality

    component_route_counts = calculate_component_route_counts(route_graph)
    node_routes = build_route_node_index(routes)

    eligible_total_length = sum(
        routes[route_id]["length"]
        for route_id in candidate_route_ids
    )
    target_length = eligible_total_length * target_removal_percent / 100.0

    # A route receives a continuity bonus only from already-hidden neighbours
    # with exactly the same hierarchy value. This does not alter hierarchy
    # priority; it only cleans gaps within equal-hierarchy road groups.
    hidden_same_class_neighbours = defaultdict(set)
    hidden_routes = set()
    permanently_blocked_routes = set()
    removed_length = 0.0

    hierarchy_hidden_lengths = defaultdict(float)
    hierarchy_hidden_counts = defaultdict(int)

    candidate_heap = []

    def push_candidate(route_id):
        route = routes[route_id]
        continuity_score = len(hidden_same_class_neighbours[route_id])

        heapq.heappush(
            candidate_heap,
            (
                -float(route["hierarchy"]),
                -continuity_score,
                route_centrality.get(route_id, 0.0),
                route["length"],
                route_id,
                continuity_score,
            ),
        )

    for route_id in candidate_route_ids:
        push_candidate(route_id)

    while candidate_heap and removed_length < target_length:
        (
            _,
            _,
            _,
            _,
            route_id,
            queued_continuity_score,
        ) = heapq.heappop(candidate_heap)

        if route_id in hidden_routes or route_id in permanently_blocked_routes:
            continue

        current_continuity_score = len(
            hidden_same_class_neighbours[route_id]
        )

        # Ignore stale heap entries produced before a continuity-score update.
        if queued_continuity_score != current_continuity_score:
            continue

        route = routes[route_id]
        route_hierarchy = route["hierarchy"]
        start_node = route["start"]
        end_node = route["end"]
        route_length = route["length"]

        if route["closed"]:
            permanently_blocked_routes.add(route_id)
            continue

        if route_length > max_removable_route_length:
            permanently_blocked_routes.add(route_id)
            continue

        if component_route_counts.get(start_node, 0) < min_component_routes:
            permanently_blocked_routes.add(route_id)
            continue

        if not route_graph.has_edge(start_node, end_node, key=route_id):
            permanently_blocked_routes.add(route_id)
            continue

        edge_attributes = dict(
            route_graph.get_edge_data(start_node, end_node, key=route_id)
        )

        # Test the network after temporary candidate removal.
        route_graph.remove_edge(start_node, end_node, key=route_id)
        keep_removed = False

        try:
            # Do not create dangles in the equal/higher-priority network.
            if priority_degree(
                route_graph,
                routes,
                start_node,
                route_hierarchy,
            ) < 2:
                raise nx.NetworkXNoPath

            if priority_degree(
                route_graph,
                routes,
                end_node,
                route_hierarchy,
            ) < 2:
                raise nx.NetworkXNoPath

            alternative_length = priority_shortest_path_length(
                route_graph=route_graph,
                routes=routes,
                source_node=start_node,
                target_node=end_node,
                candidate_hierarchy=route_hierarchy,
            )

            detour_ratio = alternative_length / max(route_length, 0.000001)
            keep_removed = detour_ratio <= max_detour_ratio

        except (nx.NetworkXNoPath, nx.NodeNotFound):
            keep_removed = False

        if not keep_removed:
            route_graph.add_edge(
                start_node,
                end_node,
                key=route_id,
                **edge_attributes,
            )
            permanently_blocked_routes.add(route_id)
            continue

        hidden_routes.add(route_id)
        removed_length += route_length
        hierarchy_hidden_lengths[route_hierarchy] += route_length
        hierarchy_hidden_counts[route_hierarchy] += 1

        # Promote adjacent candidates of the same hierarchy. A road between two
        # hidden same-class roads obtains a stronger continuity bonus than a road
        # touching only one hidden same-class road.
        neighbouring_route_ids = (
            node_routes.get(start_node, set())
            | node_routes.get(end_node, set())
        )

        for neighbour_route_id in neighbouring_route_ids:
            if neighbour_route_id == route_id:
                continue

            if neighbour_route_id not in candidate_route_ids:
                continue

            if (
                neighbour_route_id in hidden_routes
                or neighbour_route_id in permanently_blocked_routes
            ):
                continue

            if (
                routes[neighbour_route_id]["hierarchy"]
                != route_hierarchy
            ):
                continue

            previous_score = len(
                hidden_same_class_neighbours[neighbour_route_id]
            )
            hidden_same_class_neighbours[neighbour_route_id].add(route_id)
            new_score = len(
                hidden_same_class_neighbours[neighbour_route_id]
            )

            if new_score != previous_score:
                push_candidate(neighbour_route_id)

    actual_percent = (
        100.0 * removed_length / eligible_total_length
        if eligible_total_length > 0
        else 0.0
    )

    add_message(
        f"Requested global removal: {target_removal_percent:.1f}% of eligible "
        f"road length; safely selected {actual_percent:.2f}% "
        f"({len(hidden_routes):,} complete routes)."
    )

    for hierarchy_value in sorted(hierarchy_hidden_counts, reverse=True):
        add_message(
            f"  Hierarchy {hierarchy_value}: "
            f"{hierarchy_hidden_counts[hierarchy_value]:,} hidden routes, "
            f"{hierarchy_hidden_lengths[hierarchy_value]:,.2f} m"
        )

    if actual_percent + 0.01 < target_removal_percent:
        add_warning(
            "The requested global reduction could not be fully reached without "
            "violating hierarchy priority, connectivity, dangle, length, detour, "
            "closed-loop, or small-component safeguards."
        )

    return hidden_routes


# -----------------------------------------------------------------------------
# Transfer graph decisions back to original roads
# -----------------------------------------------------------------------------

def select_complete_original_features_to_hide(
    edge_source_ids,
    edge_to_route,
    hidden_routes,
):
    """Hide an original feature only when all its planar segments are hidden."""

    source_total_segment_count = defaultdict(int)
    source_hidden_segment_count = defaultdict(int)

    for edge_id, source_id in edge_source_ids.items():
        if source_id is None:
            continue

        source_total_segment_count[source_id] += 1

        if edge_to_route.get(edge_id) in hidden_routes:
            source_hidden_segment_count[source_id] += 1

    hidden_source_ids = {
        source_id
        for source_id, total_count in source_total_segment_count.items()
        if total_count > 0
        and source_hidden_segment_count.get(source_id, 0) == total_count
    }

    partially_selected_count = sum(
        1
        for source_id, total_count in source_total_segment_count.items()
        if 0 < source_hidden_segment_count.get(source_id, 0) < total_count
    )

    if partially_selected_count:
        add_message(
            f"Protected {partially_selected_count:,} original features because "
            "only part of each original geometry was selected by the planar graph."
        )

    return hidden_source_ids


def update_original_invisibility_field(
    source_feature_class,
    invisibility_field,
    hidden_source_ids,
):
    """Write the final automatic all-hierarchy result to the original input."""

    source_oid_field = arcpy.Describe(source_feature_class).OIDFieldName
    hidden_count = 0
    visible_count = 0
    changed_count = 0

    with arcpy.da.UpdateCursor(
        source_feature_class,
        [source_oid_field, invisibility_field],
    ) as cursor:
        for row in cursor:
            source_id = str(row[0])
            new_value = 1 if source_id in hidden_source_ids else 0

            if new_value == 1:
                hidden_count += 1
            else:
                visible_count += 1

            if row[1] != new_value:
                row[1] = new_value
                cursor.updateRow(row)
                changed_count += 1

    add_message(
        f"Final original roads: {hidden_count:,} hidden and "
        f"{visible_count:,} visible."
    )
    add_message(f"Rows actually written: {changed_count:,}.")

    return hidden_count, visible_count, changed_count


# -----------------------------------------------------------------------------
# Main workflow
# -----------------------------------------------------------------------------

def geometry_only_road_thinning_fast(
    input_roads,
    hierarchy_field,
    invisibility_field="Invisibility",
    node_tolerance=0.20,
    target_removal_percent=20.0,
    max_removable_route_length=800.0,
    max_detour_ratio=4.0,
    centrality_sample_nodes=200,
    min_component_routes=8,
):
    """
    Thin the complete road network using global hierarchy priority.

    The target-removal percentage is calculated once across all valid
    hierarchy routes. Larger hierarchy numbers are considered first, while
    equal hierarchy values are decided by continuity and graph importance.
    """

    if node_tolerance <= 0:
        raise ValueError("Node tolerance must be greater than zero.")

    if not 0 <= target_removal_percent <= 100:
        raise ValueError("Target removal percentage must be between 0 and 100.")

    if max_removable_route_length <= 0:
        raise ValueError("Maximum removable route length must be greater than zero.")

    if max_detour_ratio < 1:
        raise ValueError("Maximum detour ratio must be at least 1.")

    if centrality_sample_nodes < 0:
        raise ValueError("Centrality sample nodes cannot be negative.")

    if min_component_routes < 1:
        raise ValueError("Minimum component routes must be at least 1.")

    source_feature_class = resolve_source_feature_class(input_roads)
    validate_metric_polyline(source_feature_class)

    actual_hierarchy_field = validate_hierarchy_field(
        source_feature_class,
        hierarchy_field,
    )

    source_hierarchy, hierarchy_values = collect_source_hierarchies(
        source_feature_class=source_feature_class,
        hierarchy_field=actual_hierarchy_field,
    )

    planar_fc, source_fid_field = create_planar_network(source_feature_class)

    (
        edge_nodes,
        edge_lengths,
        edge_source_ids,
        edge_hierarchies,
        node_edges,
    ) = read_planar_segments(
        planar_feature_class=planar_fc,
        source_fid_field=source_fid_field,
        source_hierarchy=source_hierarchy,
        node_tolerance=node_tolerance,
    )

    add_message(
        f"Planar graph contains {len(node_edges):,} nodes and "
        f"{len(edge_nodes):,} atomic segments."
    )

    routes, edge_to_route = build_hierarchy_routes(
        edge_nodes=edge_nodes,
        edge_lengths=edge_lengths,
        edge_hierarchies=edge_hierarchies,
        node_edges=node_edges,
    )

    del edge_nodes
    del edge_lengths
    del edge_hierarchies
    del node_edges
    del source_hierarchy

    hierarchy_route_counts = defaultdict(int)
    protected_route_count = 0

    for route in routes.values():
        if route["hierarchy"] is None:
            protected_route_count += 1
        else:
            hierarchy_route_counts[route["hierarchy"]] += 1

    add_message(
        f"Atomic segments were grouped into {len(routes):,} complete routes."
    )

    for hierarchy_value in hierarchy_values:
        add_message(
            f"  Hierarchy {hierarchy_value}: "
            f"{hierarchy_route_counts.get(hierarchy_value, 0):,} same-class routes"
        )

    if protected_route_count:
        add_warning(
            f"{protected_route_count:,} routes have no valid hierarchy mapping and "
            "will remain protected."
        )

    hidden_routes = select_routes_global_priority(
        routes=routes,
        target_removal_percent=target_removal_percent,
        max_removable_route_length=max_removable_route_length,
        max_detour_ratio=max_detour_ratio,
        centrality_sample_nodes=centrality_sample_nodes,
        min_component_routes=min_component_routes,
    )

    hidden_source_ids = select_complete_original_features_to_hide(
        edge_source_ids=edge_source_ids,
        edge_to_route=edge_to_route,
        hidden_routes=hidden_routes,
    )

    actual_invisibility_field = ensure_numeric_invisibility_field(
        source_feature_class,
        invisibility_field,
    )

    hidden_count, _, _ = update_original_invisibility_field(
        source_feature_class=source_feature_class,
        invisibility_field=actual_invisibility_field,
        hidden_source_ids=hidden_source_ids,
    )

    add_message(
        "No original feature, geometry, or non-invisibility attribute was changed."
    )
    add_message(
        f"Definition query for visible roads: {actual_invisibility_field} <> 1"
    )

    if hidden_routes and hidden_count == 0:
        add_warning(
            "Routes were selected by the graph, but no complete original feature "
            "was fully selected. Original features spanning both hidden and retained "
            "routes were conservatively kept visible."
        )

    return input_roads


# -----------------------------------------------------------------------------
# ArcGIS Pro script-tool entry point
# -----------------------------------------------------------------------------

def _read_float_parameter(index, default_value):
    value = arcpy.GetParameterAsText(index)
    return float(value) if value not in (None, "") else float(default_value)


def _read_int_parameter(index, default_value):
    value = arcpy.GetParameterAsText(index)
    return int(value) if value not in (None, "") else int(default_value)


def run_as_script_tool():
    """
    ArcGIS Pro script-tool parameters:

    0  Input roads                    Feature Layer
    1  Hierarchy field                Field
    2  Invisibility field             Field/String     default Invisibility
    3  Node tolerance (m)             Double           default 0.20
    4  Target removal (%)             Double           default 20
    5  Maximum removable length (m)   Double           default 800
    6  Maximum detour ratio           Double           default 4
    7  Centrality sample nodes        Long             default 200
    8  Minimum component routes       Long             default 8
    9  Modified input                 Derived Feature Layer (optional)

    For parameter 1, set Parameter Dependency to parameter 0 in the script-tool
    properties so ArcGIS Pro displays the input road fields automatically.
    """

    input_roads = arcpy.GetParameterAsText(0)
    hierarchy_field = arcpy.GetParameterAsText(1)
    invisibility_field = arcpy.GetParameterAsText(2) or "INVISIBILITY"

    # Replace these only when running the .py file directly instead of as a tool.
    if not input_roads:
        input_roads = "TA0060_Road_L_ExportFeatures11"
    if not hierarchy_field:
        hierarchy_field = "HIERARCHY"

   

    

 

def delete_if_exists(dataset):
    """Delete a dataset or layer if it exists."""
    if arcpy.Exists(dataset):
        arcpy.management.Delete(dataset)


def run_thinning_stage(road_source,current_road_layer,temporary_gdb,stage_number,area_threshold,hierarchy_value,thinning_distance,hierarchy_field,invisibility_field,ref_scale):
    """
    Create polygons from visible roads, assign hierarchy values,
    and thin the original road feature class.

    Temporary feature classes are stored in the user-provided
    geodatabase.
    """
    arcpy.env.referenceScale = ref_scale
    polygon_fc = os.path.join(temporary_gdb,f"Stage_{stage_number}_Polygons")
    dissolved_fc = os.path.join(temporary_gdb,f"Stage_{stage_number}_Dissolved")
    polygon_layer = f"stage_{stage_number}_polygon_layer"
    next_road_layer = f"roads_stage_{stage_number}"
    temporary_items = [polygon_layer,polygon_fc,dissolved_fc]
    for item in temporary_items + [next_road_layer]:
        delete_if_exists(item)
    arcpy.AddMessage(f"Starting thinning stage {stage_number}...")
    try:
        arcpy.management.FeatureToPolygon(in_features=current_road_layer,out_feature_class=polygon_fc)
        polygon_count = int(arcpy.management.GetCount(polygon_fc)[0])
        if polygon_count == 0:
            arcpy.AddWarning(f"Stage {stage_number}: no closed polygons were created from the visible road network.")
        polygon_query = (f"Shape_Area < {float(area_threshold)}")
        arcpy.management.MakeFeatureLayer(in_features=polygon_fc,out_layer=polygon_layer,where_clause=polygon_query)
        selected_polygon_count = int(arcpy.management.GetCount(polygon_layer)[0])
        arcpy.AddMessage(
            f"Stage {stage_number}: {selected_polygon_count} polygons selected.")
        if selected_polygon_count > 0:
            arcpy.management.Dissolve(in_features=polygon_layer,out_feature_class=dissolved_fc)
            arcpy.management.SelectLayerByLocation(in_layer=current_road_layer,overlap_type="SHARE_A_LINE_SEGMENT_WITH",select_features=dissolved_fc,selection_type="NEW_SELECTION")
            selected_road_count = int(arcpy.management.GetCount(current_road_layer)[0])
            if selected_road_count > 0:
                arcpy.management.CalculateField(in_table=current_road_layer,field=hierarchy_field,expression=str(hierarchy_value),expression_type="PYTHON3")
            arcpy.management.SelectLayerByAttribute(in_layer_or_view=current_road_layer,selection_type="CLEAR_SELECTION")
        else:
            arcpy.AddMessage(
                f"Stage {stage_number}: no polygons were smaller than {area_threshold} square metres."
            )
        with arcpy.EnvManager(referenceScale=ref_scale):
            arcpy.cartography.ThinRoadNetwork(in_features=current_road_layer,minimum_length=thinning_distance,invisibility_field=invisibility_field,hierarchy_field=hierarchy_field)
        arcpy.management.MakeFeatureLayer(in_features=road_source,out_layer=next_road_layer,where_clause=f"{invisibility_field} = 0")
        return next_road_layer
    finally:
        # Remove temporary feature classes and layers
        for item in temporary_items:
            delete_if_exists(item)


def collapse_replace(input_line_list, collapse_sql, collapse_size, carto_partition, working_gdb, ref_scale):
    # Set the workspace
    arcpy.env.overwriteOutput = True
    # Set the cartographic partitions
    # arcpy.env.cartographicPartitions = carto_partition
    try:
        for input_line, colps_sql in zip(input_line_list, collapse_sql):
            fc_singlepart = arcpy.management.MultipartToSinglepart(input_line, f"{working_gdb}\\fc_singlepart")
            # if colps_sql != '' and colps_sql != None:
            #     in_lyr = arcpy.management.MakeFeatureLayer(fc_singlepart, "in_lyr", colps_sql)
            # else:
            in_lyr = arcpy.management.MakeFeatureLayer(fc_singlepart, "in_lyr")
            # Run Collapse Road Detail
            collapse_out = "Collapse"
            arcpy.AddMessage("Collapsing Road Detail")
            with arcpy.EnvManager(transferGDBAttributeProperties="NOT_TRANSFER_GDB_ATTRIBUTE_PROPERTIES", referenceScale=ref_scale):
                arcpy.cartography.CollapseRoadDetail(
                    in_features=in_lyr,
                    collapse_distance=f'{collapse_size} Meters',
                    output_feature_class=collapse_out,
                    locking_field=None
                )

            # # Select features and delete features
            # arcpy.management.SelectLayerByLocation(input_line, "WITHIN", in_lyr, None, "NEW_SELECTION", "NOT_INVERT")
            # Delete all features in original feature class
            arcpy.AddMessage("Replacing geometry on original features")
            arcpy.management.DeleteFeatures(input_line)
            # Append collups out features with input features
            arcpy.management.Append(collapse_out, input_line, "NO_TEST")
            # Delete temp files
            arcpy.management.Delete([fc_singlepart, "Collapse", "in_lyr"])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Collapse replace error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)

def trans_delete_dangles_old(trans_lines, sql, compare_fcs, seg_length, working_gdb, recursive):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb

    try:
        # Denote dangles using points using the
        # Feature Vertices to Points GP tool at dangles
        arcpy.AddMessage("Creating points at dangles...")
        dangles = arcpy.management.FeatureVerticesToPoints(trans_lines, "dangles", "DANGLE").getOutput(0)
        # Use Describe function to get SHAPE Length field
        shp_len_fld = arcpy.da.Describe(trans_lines)['lengthFieldName']
        # Create feature layer of hydro lines where
        # length of segment < seg_length and Name field
        # is an empty string or NULL
    
        where = f"{shp_len_fld} < {seg_length}"
        if sql:
            where += " AND "  + "(" + sql + ")"
        arcpy.management.MakeFeatureLayer(trans_lines, "transport", where)
        feature_count = count_features("transport")
        if feature_count >= 1:
            if recursive == "true":
                delete_dangles("transport", dangles, seg_length, compare_fcs, working_gdb)
                arcpy.management.SelectLayerByAttribute("transport", "NEW_SELECTION", where)
        else:
            delete_dangles("transport", dangles, seg_length, compare_fcs, working_gdb)

        # Delete temp files
        arcpy.management.Delete([dangles, "transport"])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete dangles error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def trans_delete_dangles_(trans_lines, sql, compare_fcs, seg_length, recursive, force_delete, working_gdb):
    # Define environment variables
    arcpy.env.overwriteOutput = 1
    arcpy.env.workspace = working_gdb

    try:
        # Denote dangles using points using the
        # Feature Vertices to Points GP tool at dangles
        arcpy.AddMessage("Creating points at dangles...")
        dangles = arcpy.management.FeatureVerticesToPoints(trans_lines, "dangles", "DANGLE").getOutput(0)
        # Use Describe function to get SHAPE Length field
        shp_len_fld = arcpy.da.Describe(trans_lines)['lengthFieldName']
        # Create feature layer of hydro lines where
        # length of segment < seg_length and Name field
        # is an empty string or NULL
    
        where = f"{shp_len_fld} < {seg_length}"
        if sql and force_delete.lower() == "true" :
            where = where
            arcpy.AddMessage(f"Using sql: {where}")
        else:
            where = f"({sql} AND ({where})"
            arcpy.AddMessage(f"Using sql: {where}")
        arcpy.management.MakeFeatureLayer(trans_lines, "transport", where)
        feature_count = int(arcpy.GetCount_management("transport")[0])
        if feature_count >= 1:
            if recursive.lower() == "true":
                count = 1
                while feature_count >= 1:
                    arcpy.AddMessage("Deleting dangles loop " + str(count))
                    feature_count  = delete_dangles("transport")
                    arcpy.management.SelectLayerByAttribute("transport", "NEW_SELECTION", where)
                    count += 1
            else:
                delete_dangles("transport", dangles, seg_length, compare_fcs, working_gdb)

        # Delete temp files
        arcpy.management.Delete([dangles, "transport"])

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete dangles error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def trans_delete_dangles(trans_lines, sql, compare_fcs, seg_length, recursive, force_delete, working_gdb):
    # Define environment variables
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb

    try:

        shp_len_fld = arcpy.da.Describe(trans_lines)["lengthFieldName"]
        length_sql = f"{shp_len_fld} < {seg_length}"
        if sql and str(force_delete).lower() != "true":
            where = f"({sql}) AND ({length_sql})"
        else:
            where = length_sql
        arcpy.AddMessage(f"Using sql: {where}")
        if str(recursive).lower() == "true":
            while True:
                dangles = arcpy.management.FeatureVerticesToPoints(trans_lines,"dangles","DANGLE")
                dangles_road = arcpy.management.SelectLayerByLocation(in_layer=trans_lines,overlap_type="BOUNDARY_TOUCHES",select_features=dangles,
                                                                      selection_type="NEW_SELECTION",invert_spatial_relationship="NOT_INVERT")
                transport_final = arcpy.management.SelectLayerByAttribute(dangles_road,"SUBSET_SELECTION",where)
                arcpy.AddMessage(f"Compare fcs: {compare_fcs}")
                if compare_fcs:
                    for fc in compare_fcs:
                        transport_final = arcpy.management.SelectLayerByLocation(in_layer=transport_final,overlap_type="INTERSECT",select_features=fc,
                                                                                 selection_type="REMOVE_FROM_SELECTION")
                        
                feature_count = int(arcpy.management.GetCount(transport_final)[0])
                if feature_count >= 1:
                    arcpy.AddMessage(f"Deleting {feature_count} dangling short line(s).")
                    arcpy.management.DeleteFeatures(transport_final)
                    # delete temporary dangle points before next iteration
                    if arcpy.Exists("dangles"):
                        arcpy.management.Delete("dangles")
                else:
                    arcpy.AddMessage("No more matching dangling short line found.")
                    if arcpy.Exists("dangles"):
                        arcpy.management.Delete("dangles")
                    break
        else:
            dangles = arcpy.management.FeatureVerticesToPoints(trans_lines,"dangles","DANGLE")
            dangles_road = arcpy.management.SelectLayerByLocation(in_layer=trans_lines,overlap_type="BOUNDARY_TOUCHES",select_features=dangles,
                                                                selection_type="NEW_SELECTION",invert_spatial_relationship="NOT_INVERT")
            transport_final = arcpy.management.SelectLayerByAttribute(dangles_road,"SUBSET_SELECTION",where)
            arcpy.AddMessage(f"Compare fcs: {compare_fcs}")
            if compare_fcs:
                for fc in compare_fcs:
                    transport_final = arcpy.management.SelectLayerByLocation(in_layer=transport_final,overlap_type="INTERSECT",select_features=fc,
                                                                            selection_type="REMOVE_FROM_SELECTION")
            feature_count = int(arcpy.management.GetCount(transport_final)[0])
            if feature_count >= 1:
                arcpy.AddMessage(f"Deleting {feature_count} dangling short line(s).")
                arcpy.management.DeleteFeatures(transport_final)
                # delete temporary dangle points before next iteration
                if arcpy.Exists("dangles"):
                    arcpy.management.Delete("dangles")
                    
    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Delete dangles error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def thin_road_network(in_features, minimum_length_min, minimum_length_max, invisibility_field, hierarchy_field, ref_scale, carto_high_sql, carto_low_sql, carto_partition, working_gdb):
    arcpy.AddMessage("Starting thin road networking")
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        # Set the reference scale
        arcpy.env.referenceScale = ref_scale

        with arcpy.EnvManager(referenceScale=ref_scale):
            arcpy.cartography.ThinRoadNetwork(
                f"{in_features[0]};{in_features[1]}",
                f"{minimum_length_max} Meters",
                invisibility_field,
                hierarchy_field
            )
        arcpy.AddMessage("Applied Thin Road Network function for maximum distance")

        # Delete temp files
        # arcpy.management.Delete([f'{working_gdb}\\road_carto_rank_high_fc', f'{working_gdb}\\road_carto_rank_low_fc', f"{working_gdb}\\fc_singlepart_road", f"{working_gdb}\\fc_singlepart_track"])
        arcpy.AddMessage("Thin Road Network function completed")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Thin road network error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)


def thin_road_network_default(in_features, minimum_length_min, minimum_length_max, invisibility_field, hierarchy_field, ref_scale, carto_high_sql, carto_low_sql, carto_partition, working_gdb):
    arcpy.AddMessage("Starting thin road networking")
    try:
        # Set the workspace
        arcpy.env.overwriteOutput = True
        # Set the reference scale
        arcpy.env.referenceScale = ref_scale
        # Set the cartographic partitions
        # arcpy.env.cartographicPartitions = carto_partition

        carto_high_where = carto_high_sql
        carto_low_where  = carto_low_sql

        
        # arcpy.AddMessage(f"Cartographic partition: {carto_partition} \n in_features: {in_features}")
    
        arcpy.SetProgressorLabel(f"Processing thin road networking for {in_features[0]} and {in_features[1]}")
        fc_singlepart_road = arcpy.management.MultipartToSinglepart(in_features[0], f"{working_gdb}\\fc_singlepart_road")
        fc_singlepart_track = arcpy.management.MultipartToSinglepart(in_features[1], f"{working_gdb}\\fc_singlepart_track")
        arcpy.management.MakeFeatureLayer(fc_singlepart_road, "fc_singlepart_road_lyr")
        arcpy.management.MakeFeatureLayer(fc_singlepart_track, "fc_singlepart_track_lyr")

        selected_features_high = arcpy.analysis.Select(carto_partition, f"{working_gdb}\\road_carto_rank_high_fc", carto_high_where)
        selected_fc_singlepart_road_lyr=arcpy.management.SelectLayerByLocation(
            in_layer="fc_singlepart_road_lyr",
            overlap_type="WITHIN",
            select_features=selected_features_high,
            selection_type="NEW_SELECTION"
        )
        selected_fc_singlepart_track_lyr=arcpy.management.SelectLayerByLocation(
            in_layer="fc_singlepart_track_lyr",
            overlap_type="WITHIN",
            select_features=selected_features_high,
            selection_type="NEW_SELECTION"
        )

        if count_features("fc_singlepart_road_lyr") > 0 and count_features("fc_singlepart_track_lyr") > 0 :
            arcpy.cartography.ThinRoadNetwork(
                f"{selected_fc_singlepart_road_lyr};{selected_fc_singlepart_track_lyr}",
                f"{minimum_length_max} Meters",
                invisibility_field,
                hierarchy_field
            )
            arcpy.AddMessage("Applied Thin Road Network function for maximum distance")


        selected_features_low = arcpy.analysis.Select(carto_partition, f"{working_gdb}\\road_carto_rank_low_fc", carto_low_where)
        selected_low_fc_singlepart_road_lyr = arcpy.management.SelectLayerByLocation(
            in_layer="fc_singlepart_road_lyr",
            overlap_type="WITHIN",
            select_features=selected_features_low,
            selection_type="NEW_SELECTION"
        )
        selected_low_fc_singlepart_track_lyr = arcpy.management.SelectLayerByLocation(
            in_layer="fc_singlepart_track_lyr",
            overlap_type="WITHIN",
            select_features=selected_features_low,
            selection_type="NEW_SELECTION"
        )
        if count_features("fc_singlepart_road_lyr") > 0 and count_features("fc_singlepart_track_lyr") > 0:
            arcpy.cartography.ThinRoadNetwork(
                f"{selected_low_fc_singlepart_road_lyr};{selected_low_fc_singlepart_track_lyr}",
                f"{minimum_length_min} Meters",
                invisibility_field,
                hierarchy_field
            )
            arcpy.AddMessage("Applied Thin Road Network function for minimum distance")

        arcpy.management.SelectLayerByAttribute("fc_singlepart_road_lyr", "CLEAR_SELECTION")
        arcpy.management.SelectLayerByAttribute("fc_singlepart_track_lyr", "CLEAR_SELECTION")
        arcpy.SetProgressorLabel(f"Repairing Geometry")
        arcpy.management.RepairGeometry("fc_singlepart_road_lyr")
        arcpy.management.RepairGeometry("fc_singlepart_track_lyr")


        arcpy.SetProgressorLabel(f"Deleting Features in {in_features[0]} and {in_features[1]}")
        arcpy.management.DeleteFeatures(in_features[0])
        arcpy.management.DeleteFeatures(in_features[1])

        arcpy.SetProgressorLabel(f"Adding Generated Features in {in_features[0]} and {in_features[1]}")
        arcpy.management.Append("fc_singlepart_road_lyr", in_features[0], "NO_TEST")
        arcpy.management.Append("fc_singlepart_track_lyr", in_features[1], "NO_TEST")
        # Delete temp files
        # arcpy.management.Delete([f'{working_gdb}\\road_carto_rank_high_fc', f'{working_gdb}\\road_carto_rank_low_fc', f"{working_gdb}\\fc_singlepart_road", f"{working_gdb}\\fc_singlepart_track"])
        arcpy.AddMessage("Thin Road Network function completed")



    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Thin road network error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)






def grouping(input_line_list, group_sql_rd1, group_sql_rd2, group_sql_track):
    arcpy.AddMessage(f"Processing grouping for {len(input_line_list)} feature classes")
    try:
        for fc in input_line_list:
            if dynamic_fc_names.Road_L in fc:
                # Add Field
                if len(arcpy.ListFields(fc, "Road_Group")) == 0:
                    arcpy.management.AddField(in_table=fc, field_name="Road_Group", field_type="TEXT")
                arcpy.management.SelectLayerByAttribute(fc, "NEW_SELECTION", group_sql_rd1)
                arcpy.management.CalculateField(in_table=fc, field="Road_Group", expression='"Highway"', expression_type="PYTHON3")
                arcpy.management.SelectLayerByAttribute(fc, "NEW_SELECTION", group_sql_rd2)
                arcpy.management.CalculateField(in_table=fc, field="Road_Group", expression='"Road"', expression_type="PYTHON3")

            elif dynamic_fc_names.Track_L in fc:
                # Add Field
                if len(arcpy.ListFields(fc, "Track_Group")) == 0:
                    arcpy.management.AddField(in_table=fc, field_name="Track_Group", field_type="TEXT")
                arcpy.management.SelectLayerByAttribute(fc, "NEW_SELECTION", group_sql_track)
                arcpy.management.CalculateField(in_table=fc, field="Track_Group", expression='"Track"', expression_type="PYTHON3")

    except Exception as e:
        tb = traceback.format_exc()
        error_message = f"Transportation grouping error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)   

def remove_short_road_in_terrace_house(road_fc, bldg_fc, search_tolerance, working_gdb, length_tolerance, road_class_field, road_class_type, name_field, invisibility_field, invisibility_field_2, hierarchy_field, logger):
    # Set environment variables
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    try:
        arcpy.AddMessage("Applying remove back lane function") 
        if length_tolerance != 0:
            # Get length field
            length_field = arcpy.da.Describe(road_fc)['lengthFieldName']
            name_query = f"({name_field} = '' or {name_field} = ' ' or {name_field} IS NULL)"
            # Create make feature layer for road and building
            copy_road_lyr = arcpy.management.CopyFeatures(road_fc, f"{working_gdb}\\copy_road_lyr")
            arcpy.management.MakeFeatureLayer(road_fc, "road_layer")
            # Select building features with RET <= 3 (Terrace houses)
            arcpy.management.SelectLayerByAttribute(in_layer_or_view=bldg_fc, selection_type="NEW_SELECTION", where_clause="RET <= 3")
            # Dissolve selected building features
            dissolved_terrace_bldg_lyr = arcpy.analysis.PairwiseDissolve(in_features=bldg_fc, out_feature_class=f"{working_gdb}\\dissolved_terrace_bldg_lyr")
            arcpy.management.MakeFeatureLayer(dissolved_terrace_bldg_lyr, "dissolved_terrace_bldg_lyr")
            # Select features within a distance
            arcpy.management.SelectLayerByLocation(in_layer="road_layer", overlap_type="WITHIN_A_DISTANCE", select_features="dissolved_terrace_bldg_lyr", search_distance=f"{search_tolerance} Meters",
                selection_type="NEW_SELECTION")
            # Select feature by attribute
            if count_features("road_layer")>0:
                arcpy.cartography.ThinRoadNetwork(in_features=road_fc, minimum_length=length_tolerance, invisibility_field=invisibility_field_2, hierarchy_field=hierarchy_field)
                warnings = arcpy.GetMessages(1)
                # Extract all txt paths from the warning text
                txt_files = re.findall(r'[A-Za-z]:\\[^\n]*\.txt', warnings)
                sharedgeom_file = None
                for file in txt_files:
                    if "SharedGeom" in file:
                        sharedgeom_file = file
                        break
                arcpy.AddMessage(f"SharedGeom file: {sharedgeom_file}")
                if sharedgeom_file != None:
                    # Wait until ArcGIS finishes writing the file (bounded so a
                    # missing file cannot hang the tool forever)
                    wait_seconds = 0
                    while not os.path.exists(sharedgeom_file) and wait_seconds < 60:
                        time.sleep(1)
                        wait_seconds += 1
                    if not os.path.exists(sharedgeom_file):
                        arcpy.AddWarning(f"Timed out waiting for SharedGeom file {sharedgeom_file}; continuing without it")
                        sharedgeom_file = None
                if sharedgeom_file != None:
                    # Read ObjectIDs
                    object_ids = []
                    with open(sharedgeom_file, "r") as f:
                        text = f.read()
                        ids = re.findall(r'OBJECTID\s*=\s*(\d+)', text)
                        object_ids = [int(i) for i in ids]
                    # convert list → SQL string
                    oid_string = ",".join(map(str, object_ids))
                    query = f"OBJECTID NOT IN ({oid_string})"
                    if len(object_ids)>0: 
                        where_clause=f"{length_field} < {length_tolerance} And {road_class_field} = {road_class_type} And {name_query} And {invisibility_field_2} = 1 And {query}"
                        arcpy.AddMessage(f"{where_clause}")
                        arcpy.management.SelectLayerByAttribute(in_layer_or_view="road_layer", selection_type="SUBSET_SELECTION", where_clause=f"{length_field} < {length_tolerance} And {road_class_field} = {road_class_type} And {name_query} And {invisibility_field_2} = 1 And {query}")
                        arcpy.AddMessage(f"deleting {count_features('road_layer')} features")
                        arcpy.management.DeleteFeatures("road_layer")
                        arcpy.AddMessage("Remove backlane function completed successfully with sharedgeom file")
                else:
                    arcpy.management.SelectLayerByAttribute(in_layer_or_view="road_layer", selection_type="SUBSET_SELECTION", where_clause=f"{length_field} < {length_tolerance} And {road_class_field} = {road_class_type} And {name_query} And {invisibility_field} = 1")
                    # Delete features
                    arcpy.AddMessage(f"deleting {count_features('road_layer')} features")
                    arcpy.management.DeleteFeatures("road_layer")
                    arcpy.AddMessage("Remove backlane function completed successfully")
        else:
            arcpy.AddMessage("Leangth value with 0 meter cannot be processed, change the the value in config file")
            arcpy.AddMessage("Skipping Remove backlane function")

    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Road feature remove in terrace area error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Road feature remove in terrace area', f'{exc_value}\n')


# changes for model to script by SIC
def resolve_segmented_symbology_fortransport(transport_layer, transport_symbology_field, transport_symbology_RCS, working_gdb, logger):
    arcpy.env.overwriteOutput = True
    arcpy.env.workspace = working_gdb
    try:
        rcs_values = []
        with arcpy.da.SearchCursor(transport_layer, [transport_symbology_RCS]) as cursor:
            for row in cursor:
                if row[0] is not None:
                    rcs_values.append(row[0])
        rcs_values = list(set(rcs_values))
        for indv_rcs in rcs_values:
            if indv_rcs == 4:
                arcpy.management.CalculateField(in_table=transport_layer, field=transport_symbology_field , expression="2",
                                                expression_type="PYTHON3",
                                                code_block="", field_type="TEXT", enforce_domains="NO_ENFORCE_DOMAINS")
                transport_layer=arcpy.management.SelectLayerByAttribute(in_layer_or_view=transport_layer, selection_type="NEW_SELECTION",
                                                        where_clause=f"{transport_symbology_RCS} = {indv_rcs}", invert_where_clause=None)
                dangle_points = arcpy.management.FeatureVerticesToPoints(in_features=transport_layer,
                                                                        out_feature_class=f"{working_gdb}\\dangle_points",
                                                                        point_location="DANGLE")
                transport_layer=arcpy.management.SelectLayerByLocation(in_layer=transport_layer, overlap_type="INTERSECT",
                                                    select_features=dangle_points,
                                                    search_distance=None, selection_type="SUBSET_SELECTION",
                                                    invert_spatial_relationship="NOT_INVERT")
                arcpy.management.CalculateField(in_table=transport_layer, field=transport_symbology_field, expression="0",
                                                expression_type="PYTHON3",
                                                code_block="", field_type="TEXT", enforce_domains="NO_ENFORCE_DOMAINS")
                transport_layer=arcpy.management.SelectLayerByAttribute(in_layer_or_view=transport_layer, selection_type="NEW_SELECTION",
                                                        where_clause=f"{transport_symbology_RCS} = {indv_rcs} And {transport_symbology_field} = 2",
                                                        invert_where_clause=None)
                arcpy.management.CalculateField(in_table=transport_layer, field=transport_symbology_field, expression="1",
                                                expression_type="PYTHON3",code_block="", field_type="TEXT", enforce_domains="NO_ENFORCE_DOMAINS")
                
        arcpy.AddMessage("Applied Resolve Segmented Symbology for Transport function")
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Resolve segmented symbology for Transport error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Resolve segmented symbology for Transport', f'{exc_value}\n')
# changes for model to script by SIC


def gen_transportation(feature_list, working_gdb, hierarchy_file, in_feature_loc, collapse_sql, carto_partition, generalize_operations, railway_sql, 
                                                       change_road_type, trans_build_up_buildings, trans_topology_features, val_dict, map_name, logger):
    
    arcpy.AddMessage('Starting transportation features generalization.....')
    # Set environment
    arcpy.env.overwriteOutput = True
    global dynamic_fc_names
    dynamic_fc_names = resolve_lyr()
    try:
        total_steps = 9
        # Remove empty string  
        input_line_list = [fc for in_line in [ dynamic_fc_names.Road_L, dynamic_fc_names.Track_L] for fc in feature_list if str(in_line) in fc]
        compare_fcs_list = list(filter(str.strip, trans_build_up_buildings))
        compare_fcs_list = sorted([fc for a_lyr in trans_build_up_buildings for fc in feature_list if str(a_lyr) in fc])
        aoi_l = f"{in_feature_loc}\\AOI_L"


        # Integrate features
        arcpy.management.Integrate(input_line_list)
        # Repair Geometry
        for  fc in input_line_list:
            if has_features(fc):
                arcpy.management.RepairGeometry(fc, 'DELETE_NULL', 'ESRI')

        # Populate hierarchy
        populate_hierarchy_new(hierarchy_file, in_feature_loc, val_dict['Resolve_conflict_build_hierarchy_field'], working_gdb)

        # Flag looping
        # for input_line in input_line_list:
        #     flag_loops(input_line, working_gdb, val_dict['Resolve_conflict_build_hierarchy_field'])
        
        hierarchy_field = val_dict['Transportation_hierarchy_field']
        invisibility_field = val_dict['Transportation_invisible_field']
        
        if val_dict["Transport_road_network_thinning"]=="Default":
            # Thin road network reducing
            thin_road_network_default(input_line_list, val_dict['Transport_minimum_length_min'], val_dict['Transport_minimum_length_max'], val_dict['Transportation_invisible_field'], 
                          val_dict['Transportation_hierarchy_field'], val_dict['Resolve_conflict_build_ref_scale'], 
                          val_dict["Transport_ThinRoad_Carto_High_sql"], val_dict["Transport_ThinRoad_Carto_low_sql"], carto_partition, working_gdb)
        if val_dict["Transport_road_network_thinning"]=="Less_thinning":
            # Thin road network reducing
            thin_road_network(input_line_list, val_dict['Transport_minimum_length_min'], val_dict['Transport_minimum_length_max'], val_dict['Transportation_invisible_field'], 
                          val_dict['Transportation_hierarchy_field'], val_dict['Resolve_conflict_build_ref_scale'], 
                          val_dict["Transport_ThinRoad_Carto_High_sql"], val_dict["Transport_ThinRoad_Carto_low_sql"], carto_partition, working_gdb)
        if val_dict["Transport_road_network_thinning"]=="Moderate_thinning":
            # Thin road network reducing
            thin_road_network(input_line_list, val_dict['Transport_minimum_length_min'], val_dict['Transport_minimum_length_max'], val_dict['Transportation_invisible_field'], 
                          val_dict['Transportation_hierarchy_field'], val_dict['Resolve_conflict_build_ref_scale'], 
                          val_dict["Transport_ThinRoad_Carto_High_sql"], val_dict["Transport_ThinRoad_Carto_low_sql"], carto_partition, working_gdb)
            
            # Area threshold, hierarchy value, thinning distance
            stages = [(500000, 5, f"{val_dict['Transport_minimum_length_min']} Meters"),(100000, 6, f"{val_dict['Transport_minimum_length_max']} Meters")]
            current_road_layer = "roads_stage_0"
            delete_if_exists(current_road_layer)
            arcpy.management.MakeFeatureLayer(in_features=input_line_list[0],out_layer=current_road_layer,where_clause=f"{invisibility_field} = 0")
            for stage_number, stage in enumerate(stages,start=1):
                area_threshold = stage[0]
                hierarchy_value = stage[1]
                thinning_distance = stage[2]
                previous_road_layer = current_road_layer
                current_road_layer = run_thinning_stage(road_source=input_line_list[0],current_road_layer=previous_road_layer,temporary_gdb=working_gdb,stage_number=stage_number,area_threshold=area_threshold,
                                                        hierarchy_value=hierarchy_value,thinning_distance=thinning_distance,hierarchy_field=hierarchy_field,invisibility_field=invisibility_field,ref_scale=val_dict['Resolve_conflict_build_ref_scale'])
                if previous_road_layer != current_road_layer:
                    delete_if_exists(previous_road_layer)

        if val_dict["Transport_road_network_thinning"]=="Aggressive_thinning":
            # Thin road network reducing
            thin_road_network(input_line_list, val_dict['Transport_minimum_length_min'], val_dict['Transport_minimum_length_max'], val_dict['Transportation_invisible_field'], 
                          val_dict['Transportation_hierarchy_field'], val_dict['Resolve_conflict_build_ref_scale'], 
                          val_dict["Transport_ThinRoad_Carto_High_sql"], val_dict["Transport_ThinRoad_Carto_low_sql"], carto_partition, working_gdb)
            node_tolerance = float(0.20)
            target_removal_percent = float(75.0)
            max_removable_route_length = float(5000.0)
            max_detour_ratio = float(15.0)
            centrality_sample_nodes = int(200)
            min_component_routes = int(1)
            current_road_layer = "roads_stage_0"
            arcpy.management.MakeFeatureLayer(in_features=input_line_list[0],out_layer=current_road_layer,where_clause=f"{invisibility_field} = 0")
            geometry_only_road_thinning_fast(
        input_roads=current_road_layer,
        hierarchy_field=hierarchy_field,
        invisibility_field=invisibility_field,
        node_tolerance=node_tolerance,
        target_removal_percent=target_removal_percent,
        max_removable_route_length=max_removable_route_length,
        max_detour_ratio=max_detour_ratio,
        centrality_sample_nodes=centrality_sample_nodes,
        min_component_routes=min_component_routes,
    )
            if previous_road_layer != current_road_layer:
                    delete_if_exists(previous_road_layer)
            
        # Road collapse and Replace
        collapse_replace(input_line_list, collapse_sql, val_dict['Transport_collapse_size'], carto_partition, working_gdb, val_dict['Resolve_conflict_build_ref_scale'])
        # Delete dangles
        delete_dngl_sql = val_dict["Transport_delete_dangle_sql"]
        # changes for model to script by SIC
        selected_compare_fcs = []
        for comp_fcs in compare_fcs_list:
            out_comp_fcs = "make_ft_"+os.path.basename(comp_fcs)+"_layer"
            arcpy.AddMessage(f"{out_comp_fcs} and {val_dict['Transport_short_delete_dangles_sql']}")
            out_comp_fcs_list = arcpy.management.MakeFeatureLayer(comp_fcs, out_comp_fcs, val_dict["Transport_short_delete_dangles_sql"])
            selected_compare_fcs.append(out_comp_fcs_list)
        selected_compare_fcs.append(aoi_l)  
        for trans_lines in input_line_list:
            if os.path.basename(trans_lines) == dynamic_fc_names.Road_L:
                if(os.path.basename(str(selected_compare_fcs[0]))==dynamic_fc_names.Road_L):
                    selected_compare_fcs.remove(selected_compare_fcs[0])
                selected_compare_fcs.insert(0, input_line_list[1])
            elif os.path.basename(trans_lines) == dynamic_fc_names.Track_L:
                if(os.path.basename(str(selected_compare_fcs[0]))==dynamic_fc_names.Track_L):
                    selected_compare_fcs.remove(selected_compare_fcs[0])
                selected_compare_fcs.insert(0, input_line_list[0])
            arcpy.AddMessage(f"{trans_lines} and {selected_compare_fcs}")
            trans_delete_dangles(trans_lines, delete_dngl_sql, selected_compare_fcs, val_dict['Transport_min_seg_length'], 
                                 val_dict['Transport_recursive_delete_dangles'], val_dict['Transport_force_delete_dangles'], working_gdb)

       

        
        # Smooth road
        # Insert main feature into topology fcs list
        topology_fcs = list(filter(str.strip, trans_topology_features))
        topology_fcs = [fc for topo in topology_fcs for fc in feature_list if str(topo) in fc]
        for input_fc in input_line_list:
            if has_features(input_fc):
                arcpy.SetProgressorLabel(f"Generalizing Shared Feature: {arcpy.da.Describe(input_fc)['baseName']}")
                if os.path.basename(input_fc) == dynamic_fc_names.Road_L:
                    main_fc = arcpy.management.MakeFeatureLayer(input_fc, "main_fc", val_dict['Transport_common_express'])
                    topology_fcs.insert(0, main_fc)
                    # selected_fc = arcpy.management.SelectLayerByAttribute(main_fc, "NEW_SELECTION", "RCS <> 5")
                    # if has_features(selected_fc):
                    track_fc = input_line_list[1]
                    gen_shared_features(main_fc, generalize_operations, val_dict['Transport_simplify_tolerance'], val_dict['Transport_smooth_tolerance'], working_gdb, topology_fcs, track_fc)
                    # arcpy.management.SelectLayerByAttribute(main_fc, "SWITCH_SELECTION", "RCS <> 5")
                    # if has_features(main_fc):
                    #     gen_shared_features(main_fc, generalize_operations, simple_tolerance, smooth_tolerance, working_gdb, topology_fcs)
                    topology_fcs.remove(main_fc)

                else:
                    main_fc = arcpy.management.MakeFeatureLayer(input_fc, "main_fc", val_dict['Transport_common_express'])
                    road_fc = input_line_list[0]
                    topology_fcs.insert(0, main_fc)
                    if has_features(main_fc):
                        gen_shared_features(main_fc, generalize_operations, val_dict['Transport_simplify_tolerance'], val_dict['Transport_smooth_tolerance'], working_gdb, topology_fcs, road_fc)
                    topology_fcs.remove(main_fc)

        # Grouping
        grouping(input_line_list, val_dict['Transport_group_sql_rd1'], val_dict['Transport_group_sql_rd2'], val_dict['Transport_group_sql_track'])
        # Polygon to point
        polygon_point_features = [fc for com_fc in [ dynamic_fc_names.Toll_Plaza_A, dynamic_fc_names.Rail_Terminal_Railway_Station_A] for fc in feature_list if str(com_fc) in fc]
        toll_plaza_p = [fc for fc in feature_list if dynamic_fc_names.Toll_Plaza_P in fc][0]
        rail_station_p = [fc for fc in feature_list if dynamic_fc_names.Rail_Terminal_Railway_Station_P in fc][0]
        temp_list = [toll_plaza_p, rail_station_p]
        for poly_fc, point_fc in zip(polygon_point_features, temp_list):
            feature2point(working_gdb, poly_fc, point_fc, val_dict['Transport_min_size'], val_dict['Transport_delete_input'], val_dict['Transport_create_one_point'], val_dict['Transport_unique_field'], None)
        # Extend polygon sides
        building_fc = [fc for fc in feature_list if dynamic_fc_names.Toll_Plaza_A in fc]
        extend_polygon_sides(building_fc, working_gdb, val_dict['Transport_minimum_length'], val_dict['Transport_minimum_width'], val_dict['Transport_additional_criteria'], None)
        # Merge parallel roads
        # RTR = 3
        railway_sql_3 = railway_sql[0]
        # RTR = 1
        railway_sql_1 = railway_sql[1]
        # Get feature class
        rail = [fc for fc in feature_list if dynamic_fc_names.Rail_Line_L in fc][0]
        merge_parallel_roads(rail, railway_sql_3, val_dict['Transport_merge_field'], val_dict['Transport_merge_distance'], val_dict['Transport_update_val'], change_road_type[0], working_gdb)
        merge_parallel_roads(rail, railway_sql_1, val_dict['Transport_merge_field'], val_dict['Transport_merge_distance'], val_dict['Transport_update_val'], change_road_type[1], working_gdb)
        
        
        # Applying remove back lane function
        road_fc=[fc for fc in feature_list if dynamic_fc_names.Road_L in fc][0]
        bldg_fc=[fc for fc in feature_list if dynamic_fc_names.Residential_Building_A in fc][0]
        road_class_field = val_dict['Transport_Terrace_road_class_field']
        road_class_type = val_dict.get('Transport_Terrace_road_class_type') or 4
        name_field=val_dict['Transport_unique_field']
        remove_short_road_in_terrace_house(road_fc, bldg_fc, val_dict['Transport_remove_backlane_distance'], working_gdb, val_dict['Transport_remove_backlane_length'], road_class_field, road_class_type, name_field, val_dict["Transportation_invisible_field"], val_dict["Transportation_backlane_invisible_field"], val_dict['Transportation_hierarchy_field'], logger)
        # calculate orientation degree field for attribute driven symbology based on connected road and dangle road
        road_fc=[fc for fc in feature_list if dynamic_fc_names.Road_L in fc][0]
        # changes for model to script by SIC
        resolve_segmented_symbology_fortransport(road_fc,val_dict["Transport_Symbology_Field"], val_dict["Transport_Symbology_Road_Class"], working_gdb, logger)
        
        
        # Apply Layer Definition on Building Feature Classes
        apply_layer_definition([dynamic_fc_names.Road_L, dynamic_fc_names.Track_L], val_dict['RCL_Apply_Layer_Definition_expression'] , map_name)
        # changes for model to script by SIC
    
    except Exception as e:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = traceback.format_exc()
        error_message = f"Transportation generalisation error: {e}\nTraceback details:\n{tb}"
        arcpy.AddError(error_message)
        logger.error(error_message)
        simplified_msgs('Transportation generalisation', f'{exc_value}\n')

