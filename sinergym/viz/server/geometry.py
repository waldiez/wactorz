"""
Dynamic building-geometry extractor.

Parses an EnergyPlus epJSON and emits a compact, renderer-agnostic geometry
document the web viz turns into Babylon meshes. Nothing about the building is
hardcoded — point this at a different epJSON and the twin adapts.

Output shape (JSON):
{
  "building": "<file stem>",
  "bbox": {"min": [x,y,z], "max": [x,y,z]},
  "floors": ["bottom","mid","top",...],          # ordered low→high
  "zones": {
    "<zone>": {
      "occupied": bool,                           # has setpoints/people (vs plenum)
      "floor": "bottom"|"mid"|"top"|"plenum"|...,
      "z_range": [zmin, zmax],
      "centroid": [x,y,z],
      "surfaces": [ {"type": "Floor"|"Wall"|...,
                     "vertices": [[x,y,z], ...]} ]
    }
  },
  "windows": [ {"zone": "<zone>", "surface": "<wall>", "vertices": [[x,y,z],...]} ]
}

The viz binds live temperature onto each zone by name (the labeler publishes
`air_temperature_<zone>` on …/observation/labeled), so the zone keys here line
up with the MQTT data 1:1.
"""
from __future__ import annotations

import json
import os
from typing import Any

# A zone is "occupied" (vs a plenum) if the model gives it people/thermostats.
# We detect that structurally so this stays building-agnostic.
_OCCUPIED_MARKERS = ("People", "ZoneControl:Thermostat")


def _verts(surface: dict[str, Any]) -> list[list[float]]:
    """BuildingSurface:Detailed — vertices as a list of {vertex_x/y/z_coordinate}."""
    out = []
    for v in surface.get("vertices", []):
        out.append([
            float(v["vertex_x_coordinate"]),
            float(v["vertex_y_coordinate"]),
            float(v["vertex_z_coordinate"]),
        ])
    return out


def _fen_verts(obj: dict[str, Any]) -> list[list[float]]:
    """FenestrationSurface:Detailed — vertices as flat numbered fields
    (vertex_1_x_coordinate, vertex_1_y_coordinate, …), NOT a list."""
    out = []
    n = int(obj.get("number_of_vertices", 4))
    for i in range(1, n + 1):
        x = obj.get(f"vertex_{i}_x_coordinate")
        if x is None:
            break
        out.append([
            float(x),
            float(obj.get(f"vertex_{i}_y_coordinate", 0.0)),
            float(obj.get(f"vertex_{i}_z_coordinate", 0.0)),
        ])
    return out


def _occupied_zones(model: dict[str, Any]) -> set[str]:
    occ: set[str] = set()
    # People objects reference a zone_or_zonelist_name (or zone_name).
    for obj in model.get("People", {}).values():
        z = obj.get("zone_or_zonelist_or_space_or_spacelist_name") or obj.get("zone_name")
        if z:
            occ.add(z)
    for obj in model.get("ZoneControl:Thermostat", {}).values():
        z = obj.get("zone_or_zonelist_name") or obj.get("zone_name")
        if z:
            occ.add(z)
    return occ


def _floor_label(zmin: float, occupied_bands: list[tuple[float, float]]) -> str:
    """Map a zone's z-floor to an ordinal floor name (bottom/mid/top/…)."""
    names = ["bottom", "mid", "top", "floor4", "floor5", "floor6"]
    for i, (lo, _hi) in enumerate(occupied_bands):
        if abs(zmin - lo) < 0.5:
            return names[i] if i < len(names) else f"floor{i+1}"
    return "plenum"


def extract(epjson_path: str) -> dict[str, Any]:
    with open(epjson_path) as f:
        model = json.load(f)

    surfaces = model.get("BuildingSurface:Detailed", {})
    occupied = _occupied_zones(model)

    # group surfaces by zone
    by_zone: dict[str, list[dict[str, Any]]] = {}
    for name, s in surfaces.items():
        z = s.get("zone_name")
        if not z:
            continue
        by_zone.setdefault(z, []).append({
            "type": s.get("surface_type", "Wall"),
            "vertices": _verts(s),
            "_name": name,
        })

    # building bbox + per-zone z-range/centroid
    bmin = [float("inf")] * 3
    bmax = [float("-inf")] * 3
    zones: dict[str, Any] = {}
    for z, surfs in by_zone.items():
        zmin, zmax = float("inf"), float("-inf")
        cx = cy = cz = 0.0
        n = 0
        for s in surfs:
            for vx, vy, vz in s["vertices"]:
                bmin = [min(bmin[0], vx), min(bmin[1], vy), min(bmin[2], vz)]
                bmax = [max(bmax[0], vx), max(bmax[1], vy), max(bmax[2], vz)]
                zmin, zmax = min(zmin, vz), max(zmax, vz)
                cx += vx; cy += vy; cz += vz; n += 1
        zones[z] = {
            "occupied": z in occupied,
            "z_range": [zmin, zmax],
            "centroid": [cx / n, cy / n, cz / n] if n else [0, 0, 0],
            "surfaces": [{"type": s["type"], "vertices": s["vertices"]} for s in surfs],
        }

    # derive occupied floor bands (distinct zmins of occupied zones), low→high
    occ_zmins = sorted({round(zones[z]["z_range"][0], 3) for z in zones if zones[z]["occupied"]})
    bands = [(zm, zm) for zm in occ_zmins]
    floors_seen: list[str] = []
    for z, zinfo in zones.items():
        label = _floor_label(zinfo["z_range"][0], bands) if zinfo["occupied"] else "plenum"
        zinfo["floor"] = label
        if zinfo["occupied"] and label not in floors_seen:
            floors_seen.append(label)

    # windows (fenestration) → map to owning zone via building_surface_name
    surf_zone = {name: s.get("zone_name") for name, s in surfaces.items()}
    windows = []
    for name, fen in model.get("FenestrationSurface:Detailed", {}).items():
        host = fen.get("building_surface_name")
        windows.append({
            "zone": surf_zone.get(host),
            "surface": host,
            "kind": fen.get("surface_type", "Window"),
            "vertices": _fen_verts(fen),
        })

    return {
        "building": os.path.splitext(os.path.basename(epjson_path))[0],
        "bbox": {"min": bmin, "max": bmax},
        "floors": floors_seen,
        "zones": zones,
        "windows": windows,
    }


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(os.path.dirname(__file__), "..", "..",
                     "ASHRAE901_OfficeMedium_STD2019_Denver_MultiAgent.epJSON")
    geo = extract(path)
    print(json.dumps(geo, indent=2)[:1200])
    print("...")
    print(f"\nbuilding : {geo['building']}")
    print(f"bbox     : {geo['bbox']}")
    print(f"floors   : {geo['floors']}")
    occ = [z for z, v in geo['zones'].items() if v['occupied']]
    ple = [z for z, v in geo['zones'].items() if not v['occupied']]
    print(f"zones    : {len(geo['zones'])} ({len(occ)} occupied, {len(ple)} plenum)")
    print(f"windows  : {len(geo['windows'])}")
    print("\nper-zone surface counts:")
    for z, v in geo['zones'].items():
        types = {}
        for s in v['surfaces']:
            types[s['type']] = types.get(s['type'], 0) + 1
        print(f"  {z:22s} floor={v['floor']:7s} z={v['z_range'][0]:.2f}-{v['z_range'][1]:.2f}  {dict(types)}")
