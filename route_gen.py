from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from math import radians, sin, cos, sqrt, atan2
from typing import Any

ROUTE_GEN_TIMEOUT_SECONDS = 60

_route_gen_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="route-gen")

_route_graph_cache_data: dict[str, Any] = {}
_route_graph_cache_lock = threading.Lock()


def _run_with_timeout(fn, timeout_s: float = ROUTE_GEN_TIMEOUT_SECONDS, *args, **kwargs):
    future = _route_gen_executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    except Exception as exc:
        raise RuntimeError(
            f"La generación de ruta tardó más de {int(timeout_s)}s (descarga de red OSM lenta). "
            "Inténtalo más tarde o con una distancia menor."
        ) from exc


def _get_route_graph(lat: float, lon: float, radius_m: int = 5000, network_type: str = "walk") -> Any:
    import osmnx as ox

    cache_key = f"{round(lat, 3)}_{round(lon, 3)}_{radius_m}_{network_type}"
    with _route_graph_cache_lock:
        cached = _route_graph_cache_data.get(cache_key)
        if cached and time.time() - cached.get("ts", 0) < 3600:
            return cached["graph"]

    def _download() -> Any:
        G = ox.graph_from_point((lat, lon), dist=radius_m, network_type=network_type)
        G = ox.add_edge_speeds(G)
        G = ox.add_edge_travel_times(G)
        return G

    G = _download()
    with _route_graph_cache_lock:
        _route_graph_cache_data[cache_key] = {"graph": G, "ts": time.time()}

    return G


def _generate_loop_route(
    lat: float,
    lon: float,
    target_distance_km: float,
    elevation_gain_target: int = 0,
    network_type: str = "walk",
    max_results: int = 3,
) -> list[dict[str, Any]]:
    import osmnx as ox
    import networkx as nx

    G = _get_route_graph(lat, lon, network_type=network_type)
    orig_node = ox.distance.nearest_nodes(G, lon, lat)

    target_m = target_distance_km * 1000
    tolerance = target_m * 0.15

    all_nodes = list(G.nodes(data=True))
    candidates = []
    for node_id, data in all_nodes:
        node_lat = data.get("y", data.get("lat", 0))
        node_lon = data.get("x", data.get("lon", 0))
        dlat = radians(node_lat - lat)
        dlon = radians(node_lon - lon)
        a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(node_lat)) * sin(dlon/2)**2
        dist = 2 * 6371000 * atan2(sqrt(a), sqrt(1-a))
        if target_m * 0.3 < dist < target_m * 0.7:
            candidates.append((node_id, dist))

    candidates.sort(key=lambda x: x[1])

    results = []
    for cand_node, _ in candidates[:min(10, len(candidates))]:
        try:
            path_out = nx.shortest_path(G, orig_node, cand_node, weight="length")
            path_back = nx.shortest_path(G, cand_node, orig_node, weight="length")

            coords = []
            seen = set()
            for node in path_out + path_back[1:]:
                if node in seen:
                    continue
                seen.add(node)
                nd = G.nodes[node]
                coords.append({"lat": nd.get("y", nd.get("lat", 0)), "lon": nd.get("x", nd.get("lon", 0))})

            total_dist_m = sum(
                G.edges[path_out[i], path_out[i+1], 0].get("length", 0)
                for i in range(len(path_out)-1)
            ) + sum(
                G.edges[path_back[i], path_back[i+1], 0].get("length", 0)
                for i in range(len(path_back)-1)
            )

            deviation = abs(total_dist_m - target_m)
            if deviation > tolerance:
                continue

            results.append({
                "distance_km": round(total_dist_m / 1000, 2),
                "distance_m": round(total_dist_m),
                "elevation_gain_m": elevation_gain_target,
                "points": coords,
                "point_count": len(coords),
                "deviation_pct": round(deviation / target_m * 100, 1),
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["deviation_pct"])
    return results[:max_results]
