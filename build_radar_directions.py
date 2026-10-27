#!/usr/bin/env python3
"""
Calcula el sentido de circulación que vigila cada radar de la DGT, como rumbo de brújula.

El inventario de radares de la DGT (DATEX II) no trae rumbos, pero sí:
  - en los radares fijos, carretera, km y si vigilan el sentido de km crecientes
    (directionRelative = positive) o decrecientes (negative); un radar que vigila los dos
    sentidos aparece como dos registros;
  - en los radares de tramo, el punto de inicio y el de fin, en el orden en que se recorren.
    (Su directionRelative no es fiable: hay tramos «positive» que van del km 32,7 al 15,4.)

Este script:

1. Toma de OpenStreetMap la vía más cercana a cada punto (preferentemente la que lleva el número
   de la carretera) y calcula su orientación local, sin sentido (p. ej. «norte-sur»).
2. Radares fijos: averigua hacia dónde crecen los km con puntos kilométricos cercanos
   (CartoCiudad y los demás radares de la DGT de la misma carretera), elige con eso uno de los dos
   sentidos de la vía y lo invierte si el radar es «negative».
   Radares de tramo: orienta la vía en cada extremo según el recorrido del inicio al fin.
3. Descarta el sentido (el radar se avisará en los dos, como sin este fichero) cuando hay
   contradicciones, porque un sentido equivocado haría callar a la app ante un radar real:
     - si el radar está sobre una calzada de sentido único de OpenStreetMap (autovías y
       autopistas) que va en sentido contrario al calculado. Pasa con un 5 % de los radares:
       el PDF de la DGT confirma el sentido, así que lo más probable es que la coordenada esté en
       la otra calzada, pero no se puede asegurar;
     - si el PDF «Puntos y tramos de control de velocidad» de la DGT da otro sentido o «Ambos».
4. Escribe radar_directions.json.

Uso:
  python build_radar_directions.py                # regenera siempre
  python build_radar_directions.py --if-changed   # sólo si ha cambiado el inventario de la DGT

Salvaguarda: no se publica nada si Overpass no responde, si se obtiene el sentido de menos del
MIN_COVERAGE de los radares fijos o si menos del MIN_AGREEMENT de los radares sobre calzadas de
sentido único coinciden con ellas (señal de que algo ha cambiado en los datos o en el cálculo).

Datos: © DGT; © colaboradores de OpenStreetMap (ODbL); CartoCiudad © IGN/CNIG (CC BY 4.0).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import build_mobile_zones as bmz
from build_mobile_zones import CACHE, OSM_CACHE_DAYS, OVERPASS, cached, distance, http_get, log, norm_road

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "radar_directions.json"

NS = "{http://datex2.eu/schema/1_0/1_0}"
INE_PROVINCE = {code: name for name, code in bmz.INE.items()}

# Tolerancias.
NEAR_ROAD_M = 60               # vía OSM más lejana que se considera la del radar
OTHER_REF_PENALTY_M = 25       # una vía sin el número de la carretera cuenta como si estuviera 25 m más lejos
LINK_PENALTY_M = 25            # ídem para ramales de enlace
TANGENT_HALF_M = 80            # la orientación de la vía se mide entre 80 m antes y 80 m después
TWIN_KM = 0.2                  # puntos km más cercanos que esto al radar no sirven para orientar
MAX_PK_SPAN_KM = 5             # los puntos km que orientan la vía, a menos de esto entre sí
MAX_CHORD_KM = 2.5             # sin vía OSM, la recta entre puntos km sirve de rumbo si son más cercanos
AMBIGUOUS = (60, 120)          # vía casi perpendicular a la dirección de los km: no se decide
VALIDATE_MAX_M = 12            # contraste: radar a menos de esto de una calzada de sentido único
PDF_MATCH_KM = 0.15            # un radar del PDF es el mismo si está en la misma vía a menos de 150 m de km
MIN_COVERAGE = 0.7             # salvaguarda: radares fijos con sentido
MIN_AGREEMENT = 0.8            # salvaguarda: coincidencia con las calzadas de sentido único


# --------------------------------------------------------------------------- utilidades

def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def flip(b: float) -> float:
    return (b + 180) % 360


# --------------------------------------------------------------------------- 1. inventario DGT

@dataclass
class Point:
    lat: float
    lon: float
    road: str | None = None
    km: float | None = None
    positive: bool | None = None
    province: str | None = None


@dataclass
class Radar:
    id: str                      # sin el prefijo GUID_, como en la app («CABINA_…», «CVM_…»)
    section: bool
    points: list[Point]          # fijo: uno; tramo: inicio y fin
    directions: list[float | None] = field(default_factory=list)
    method: str = ""


def parse_radars(body: bytes) -> list[Radar]:
    root = ET.fromstring(body)
    radars = []
    for loc in root.iter(f"{NS}predefinedLocation"):
        rid = loc.get("id")
        if not rid:
            continue  # predefinedLocation anidado (el que lleva xsi:type)
        refs = []
        for ref in loc.iter(f"{NS}referencePoint"):
            def text(tag):
                e = ref.find(f".//{NS}{tag}")
                return e.text.strip() if e is not None and e.text else None
            km = text("referencePointDistance")
            direction = text("directionRelative")
            refs.append(dict(
                road=text("roadNumber"),
                km=float(km) / 1000 if km else None,
                positive=None if direction not in ("positive", "negative") else direction == "positive",
                province=INE_PROVINCE.get(text("provinceINEIdentifier") or ""),
            ))

        def coords(parent) -> tuple[float, float] | None:
            lat = parent.find(f".//{NS}latitude")
            lon = parent.find(f".//{NS}longitude")
            return (float(lat.text), float(lon.text)) if lat is not None and lon is not None else None

        linear = loc.find(f".//{NS}tpeglinearLocation")
        if linear is not None:
            start = coords(linear.find(f"{NS}from"))
            end = coords(linear.find(f"{NS}to"))
            if not start or not end or len(refs) < 2:
                continue
            points = [Point(*start, **refs[0]), Point(*end, **refs[1])]
            radars.append(Radar(rid.removeprefix("GUID_"), True, points))
        else:
            c = coords(loc)
            if not c or not refs:
                continue
            radars.append(Radar(rid.removeprefix("GUID_"), False, [Point(*c, **refs[0])]))
    return radars


def inventory_sha(radars: list[Radar]) -> str:
    """Huella del contenido (el XML lleva la hora de publicación y cambia en cada descarga)."""
    rows = sorted(
        (r.id, tuple((p.lat, p.lon, p.road, p.km, p.positive) for p in r.points)) for r in radars)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def pdf_senses(pdf_bytes: bytes) -> dict[str, list[tuple[float, str]]]:
    """Radares fijos del PDF de la DGT: carretera → [(km, «Creciente»|«Decreciente»|«Ambos»)]."""
    import io

    import pdfplumber

    row = re.compile(r"^.+? (?P<road>\S+) Radar Fijo (?P<pk>[\d.]+) (?P<s>Creciente|Decreciente|Ambos)(?= |$)")
    out: dict[str, list[tuple[float, str]]] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                m = row.match(line.strip())
                if m:
                    out.setdefault(norm_road(m["road"]), []).append((float(m["pk"]), m["s"]))
    return out


def pdf_disagrees(p: Point, senses: dict[str, list[tuple[float, str]]]) -> bool:
    """El PDF trae este radar con otro sentido o con «Ambos» (y no con el del XML)."""
    if not p.road or p.km is None:
        return False
    found = {s for km, s in senses.get(norm_road(p.road), []) if abs(km - p.km) < PDF_MATCH_KM}
    return bool(found) and ("Creciente" if p.positive else "Decreciente") not in found


# --------------------------------------------------------------------------- 2. vías de OSM

@dataclass
class Way:
    geometry: list[tuple[float, float]]
    refs: set[str]
    highway: str
    oneway: int                  # 1 en el orden de la geometría, -1 al revés, 0 doble sentido


ROAD_TYPES = "motorway|trunk|primary|secondary|tertiary|unclassified|residential"


def fetch_ways(points: list[Point]) -> list[Way]:
    """Vías de OSM a menos de NEAR_ROAD_M de cada punto, en una sola consulta a Overpass."""
    clauses = "\n".join(
        f'  way["highway"~"^({ROAD_TYPES})(_link)?$"](around:{NEAR_ROAD_M},{p.lat:.6f},{p.lon:.6f});'
        for p in points)
    query = f"[out:json][timeout:900];\n(\n{clauses}\n);\nout geom;"
    key = hashlib.sha1(query.encode()).hexdigest()[:12]

    def fetch():
        last = None
        for endpoint in OVERPASS:
            for attempt in range(3):
                try:
                    return http_get(endpoint, data=urllib.parse.urlencode({"data": query}).encode(), timeout=1000)
                except Exception as e:  # noqa: BLE001 - se reintenta con otro servidor
                    last = e
                    log(f"  Overpass {endpoint} falló ({e}); reintento")
                    time.sleep(20 * (attempt + 1))
        raise bmz.overpass_unavailable("vías junto a los radares", last)

    data = json.loads(cached(f"osm_radar_ways_{key}.json", fetch, OSM_CACHE_DAYS))
    ways = []
    for e in data["elements"]:
        if e["type"] != "way" or "geometry" not in e:
            continue
        tags = e.get("tags", {})
        highway = tags.get("highway", "")
        oneway = {"yes": 1, "true": 1, "1": 1, "-1": -1, "reverse": -1}.get(tags.get("oneway", ""), 0)
        if not oneway and tags.get("oneway") != "no" and (highway.startswith("motorway") or tags.get("junction") == "roundabout"):
            oneway = 1
        ways.append(Way(
            geometry=[(q["lat"], q["lon"]) for q in e["geometry"]],
            refs={norm_road(r) for r in tags.get("ref", "").split(";") if r.strip()},
            highway=highway,
            oneway=oneway,
        ))
    return ways


class WayIndex:
    def __init__(self, ways: list[Way]):
        self.ways = ways
        self.grid: dict[tuple[int, int], set[int]] = {}
        for i, w in enumerate(ways):
            # También puntos intermedios: una recta larga puede no tener vértices cerca del radar.
            for (la, lo), (lb, lob) in zip(w.geometry, w.geometry[1:] or w.geometry):
                steps = int(distance(la, lo, lb, lob) // 200) + 1
                for k in range(steps + 1):
                    lat, lon = la + (lb - la) * k / steps, lo + (lob - lo) * k / steps
                    self.grid.setdefault((int(lat * 200), int(lon * 200)), set()).add(i)

    def near(self, lat: float, lon: float) -> set[int]:
        cx, cy = int(lat * 200), int(lon * 200)
        out = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out |= self.grid.get((cx + dx, cy + dy), set())
        return out


def _xy(lat0: float, lat: float, lon: float) -> tuple[float, float]:
    return lon * 111_320 * math.cos(math.radians(lat0)), lat * 110_540


def project(geometry: list[tuple[float, float]], lat: float, lon: float) -> tuple[float, int, float]:
    """Distancia del punto a la polilínea, índice del segmento más cercano y posición en él (0-1)."""
    px, py = _xy(lat, lat, lon)
    best = (math.inf, 0, 0.0)
    for i in range(len(geometry) - 1):
        ax, ay = _xy(lat, *geometry[i])
        bx, by = _xy(lat, *geometry[i + 1])
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, i, t)
    return best


def tangent(geometry: list[tuple[float, float]], seg: int, t: float, half: float = TANGENT_HALF_M) -> float:
    """Rumbo de la vía (en el orden de su geometría) entre `half` metros antes y después del punto."""
    a, b = geometry[seg], geometry[seg + 1]
    here = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

    def walk(points: list[tuple[float, float]]) -> tuple[float, float]:
        pos, left = here, half
        for q in points:
            d = distance(*pos, *q)
            if d >= left:
                f = left / d
                return pos[0] + (q[0] - pos[0]) * f, pos[1] + (q[1] - pos[1]) * f
            pos, left = q, left - d
        return pos

    behind = walk(geometry[seg::-1])
    ahead = walk(geometry[seg + 1:])
    if distance(*behind, *ahead) < 5:
        return bearing(*a, *b)
    return bearing(*behind, *ahead)


@dataclass
class RoadHit:
    axis: float                  # rumbo de la vía en el orden de su geometría
    distance: float
    way: Way
    same_road: bool


def road_at(index: WayIndex, p: Point) -> RoadHit | None:
    """La vía en la que está el punto: la más cercana, con preferencia por la que lleva su número."""
    target = norm_road(p.road) if p.road else None
    best, best_score = None, math.inf
    for i in index.near(p.lat, p.lon):
        w = index.ways[i]
        if len(w.geometry) < 2:
            continue
        d, seg, t = project(w.geometry, p.lat, p.lon)
        if d > NEAR_ROAD_M:
            continue
        same = target is not None and target in w.refs
        score = d + (0 if same else OTHER_REF_PENALTY_M) + (LINK_PENALTY_M if w.highway.endswith("_link") else 0)
        if score < best_score:
            best_score = score
            best = RoadHit(tangent(w.geometry, seg, t), d, w, same)
    return best


# --------------------------------------------------------------------------- 3. hacia dónde crecen los km

@dataclass
class KmPoint:
    km: float
    lat: float
    lon: float


def plausible(p: Point, a: KmPoint) -> bool:
    """La distancia en línea recta entre el radar y el punto km encaja con su diferencia de km."""
    span = abs(a.km - p.km) * 1000
    d = distance(p.lat, p.lon, a.lat, a.lon)
    return span * 0.5 - 250 <= d <= span * 1.5 + 250


def km_bearing(p: Point, dgt_points: dict[str, list[KmPoint]], cartociudad=bmz.cartociudad) -> tuple[float, float] | None:
    """Rumbo hacia los km crecientes junto al radar y separación (km) de los puntos usados."""
    if p.km is None or not p.road:
        return None
    road = norm_road(p.road)
    others = [a for a in dgt_points.get(road, []) if abs(a.km - p.km) >= TWIN_KM]

    def side(sign: int) -> KmPoint | None:
        """Punto km más cercano por debajo (sign=-1) o por encima (+1) del radar."""
        own = sorted((a for a in others if sign * (a.km - p.km) > 0), key=lambda a: abs(a.km - p.km))
        first = (math.floor if sign < 0 else math.ceil)(p.km + sign * TWIN_KM)
        best = next((a for a in own[:3] if plausible(p, a)), None)
        if p.province:
            for km in (first, first + sign):
                if km < 0 or (best and abs(best.km - p.km) <= abs(km - p.km)):
                    break
                cc = cartociudad(p.road, km, p.province)
                if cc:
                    a = KmPoint(cc.km, cc.lat, cc.lon)
                    if plausible(p, a):
                        best = a
                        break
        return best

    lo, hi = side(-1), side(+1)
    if lo and hi and hi.km - lo.km <= MAX_PK_SPAN_KM:
        return bearing(lo.lat, lo.lon, hi.lat, hi.lon), hi.km - lo.km
    for a in (x for x in (lo, hi) if x):
        if abs(a.km - p.km) <= MAX_PK_SPAN_KM and distance(p.lat, p.lon, a.lat, a.lon) >= 200:
            b = bearing(a.lat, a.lon, p.lat, p.lon) if a is lo else bearing(p.lat, p.lon, a.lat, a.lon)
            return b, abs(a.km - p.km)
    return None


def orient(axis: float | None, towards: float, chord_km: float) -> float | None:
    """De los dos sentidos de la vía (`axis` y su opuesto), el que va hacia `towards`.
    Sin vía, `towards` sirve tal cual si viene de puntos cercanos."""
    if axis is None:
        return towards if chord_km <= MAX_CHORD_KM else None
    diff = angle_diff(axis, towards)
    if diff <= AMBIGUOUS[0]:
        return axis
    if diff >= AMBIGUOUS[1]:
        return flip(axis)
    return towards if chord_km <= MAX_CHORD_KM else None


# --------------------------------------------------------------------------- 4. cálculo

def solve(radars: list[Radar], index: WayIndex, cartociudad=bmz.cartociudad,
          senses: dict[str, list[tuple[float, str]]] | None = None) -> dict:
    dgt_points: dict[str, list[KmPoint]] = {}
    for r in radars:
        for p in r.points:
            if p.road and p.km is not None:
                dgt_points.setdefault(norm_road(p.road), []).append(KmPoint(p.km, p.lat, p.lon))

    def one(r: Radar):
        hits = [road_at(index, p) for p in r.points]
        if r.section:
            start, end = r.points
            chord = bearing(start.lat, start.lon, end.lat, end.lon)
            length = distance(start.lat, start.lon, end.lat, end.lon) / 1000
            r.directions = [orient(h.axis if h else None, chord, length) for h in hits]
            r.method = "tramo"
            return
        p, hit = r.points[0], hits[0]
        if p.positive is None:
            r.method = "sin sentido en la DGT"
            return
        if senses and pdf_disagrees(p, senses):
            r.method = "el PDF da otro sentido"
            return
        kb = km_bearing(p, dgt_points, cartociudad)
        if kb is None:
            r.method = "sin puntos km"
            return
        d = orient(hit.axis if hit else None, *kb)
        if d is None:
            r.method = "vía perpendicular a los km" if hit else "sin vía"
            return
        r.directions = [d if p.positive else flip(d)]
        r.method = "vía OSM" if hit else "puntos km"

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, radars))

    # Contraste: radares fijos sobre una calzada de sentido único de su carretera.
    checked = agree = 0
    disagreements = []
    for r in radars:
        if r.section or not r.directions:
            continue
        hit = road_at(index, r.points[0])
        if hit and hit.same_road and hit.way.oneway and hit.distance <= VALIDATE_MAX_M:
            travel = hit.axis if hit.way.oneway > 0 else flip(hit.axis)
            checked += 1
            if angle_diff(travel, r.directions[0]) < 90:
                agree += 1
            else:
                disagreements.append(r.id)
                r.directions = []
                r.method = "contradice la calzada OSM"

    fixed = [r for r in radars if not r.section]
    methods: dict[str, int] = {}
    for r in radars:
        methods[r.method] = methods.get(r.method, 0) + 1
    return {
        "fixed": len(fixed),
        "fixedWithDirection": sum(1 for r in fixed if r.directions),
        "sections": len(radars) - len(fixed),
        "sectionsWithDirection": sum(1 for r in radars if r.section and all(d is not None for d in r.directions)),
        "checked": checked,
        "agree": agree,
        "disagreements": sorted(disagreements),
        "methods": methods,
    }


# --------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--if-changed", action="store_true",
                        help="no hacer nada si el inventario de la DGT es el mismo que el de la última publicación")
    args = parser.parse_args()

    body = cached(f"dgt_radares_{date.today():%Y%m%d}.xml", lambda: bmz.retry(bmz.DGT_RADARS_XML))
    radars = parse_radars(body)
    sha = inventory_sha(radars)
    previous = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    if args.if_changed and previous.get("dgtSha256") == sha:
        log("El inventario de radares de la DGT no ha cambiado: nada que hacer.")
        bmz.github_output(changed="false")
        return
    log(f"{len(radars)} radares de la DGT")

    try:
        senses = pdf_senses(bmz.download_pdf()[1])
        log(f"{sum(map(len, senses.values()))} radares fijos en el PDF de la DGT")
    except (Exception, SystemExit) as e:  # noqa: BLE001 - es sólo una comprobación adicional
        log(f"Aviso: sin el PDF de la DGT no se contrasta su sentido ({e})")
        senses = None

    ways = fetch_ways([p for r in radars for p in r.points])
    log(f"{len(ways)} vías de OpenStreetMap junto a los radares")
    stats = solve(radars, WayIndex(ways), senses=senses)

    coverage = stats["fixedWithDirection"] / max(stats["fixed"], 1)
    agreement = stats["agree"] / stats["checked"] if stats["checked"] else 1.0
    summary = (f"Radares fijos con sentido: {stats['fixedWithDirection']} de {stats['fixed']} ({coverage:.0%})\n"
               f"Radares de tramo con sentido: {stats['sectionsWithDirection']} de {stats['sections']}\n"
               f"Contraste con calzadas de sentido único de OSM: {stats['agree']} de {stats['checked']} "
               f"({agreement:.0%})\n"
               f"Métodos: {stats['methods']}")
    log("\n" + summary)
    if stats["disagreements"]:
        log("Sin sentido por no coincidir con la calzada OSM:", ", ".join(stats["disagreements"]))

    if bmz.OVERPASS_FAILURES:
        sys.exit("Salvaguarda: Overpass no respondió. No se publica nada.")
    if coverage < MIN_COVERAGE:
        sys.exit(f"Salvaguarda: sólo {coverage:.0%} de radares fijos con sentido (mínimo {MIN_COVERAGE:.0%}).")
    if stats["checked"] >= 50 and agreement < MIN_AGREEMENT:
        sys.exit(f"Salvaguarda: sólo {agreement:.0%} coincide con OSM (mínimo {MIN_AGREEMENT:.0%}).")

    OUTPUT.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "dgtSha256": sha,
        "attribution": "DGT; © colaboradores de OpenStreetMap (ODbL); CartoCiudad © IGN/CNIG (CC BY 4.0)",
        "stats": {k: v for k, v in stats.items() if k != "disagreements"},
        "radars": {
            r.id: [None if d is None else round(d) % 360 for d in r.directions]
            for r in sorted(radars, key=lambda r: r.id)
            if any(d is not None for d in r.directions)
        },
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    bmz.github_output(changed="true", fixed=f"{stats['fixedWithDirection']}/{stats['fixed']}",
                      agreement=f"{agreement:.0%}")
    bmz.github_summary(f"### Sentido de los radares\n\n```\n{summary}\n```")
    log(f"Fichero: {OUTPUT} ({OUTPUT.stat().st_size / 1e3:.0f} kB)")


if __name__ == "__main__":
    main()
