#!/usr/bin/env python3
"""
Genera los tramos de vigilancia con radar móvil geolocalizados para la app.

La DGT publica los tramos sólo como «provincia, carretera, km inicial, km final» en un PDF
(https://www.dgt.es/conoce-el-estado-del-trafico/vigilancia-y-control/equipos-y-tramos-de-vigilancia/).
Este script:

1. Descarga ese PDF y extrae los tramos de radar móvil (une los tramos contiguos).
2. Descarga de OpenStreetMap (Overpass) el trazado de esas carreteras en cada provincia.
3. Reúne puntos de referencia «km → coordenadas» de tres fuentes y descarta los que no caen
   sobre la carretera:
     - hitos kilométricos de OpenStreetMap (highway=milestone),
     - el geocodificador CartoCiudad del IGN («N-110 km 262»),
     - el inventario de radares de la DGT (que trae carretera, km y coordenadas).
4. Sitúa el km inicial y final de cada tramo recorriendo el grafo de la carretera entre dos
   puntos de referencia (escalando por la longitud real) y recorta el trazado entre ambos.
5. Escribe mobile_zones.json, report.json (lo que no se pudo situar) y fuente.json (qué PDF
   de la DGT se usó y la cobertura obtenida).

Uso:
  python build_mobile_zones.py                 # regenera siempre
  python build_mobile_zones.py --if-changed    # sólo si la DGT ha publicado un PDF distinto
  python build_mobile_zones.py --only GR,Z     # pruebas con algunas provincias (no publica)
Requiere: pip install pdfplumber
Las descargas se guardan en .cache para no repetirlas (las de OpenStreetMap caducan a los 30 días).

Salvaguarda: si los km situados bajan más de un 10 % respecto a la última publicación (p. ej.
porque la DGT cambia el formato del PDF o falla un servicio), termina con error sin escribir nada.

Datos: © DGT; © colaboradores de OpenStreetMap (ODbL); CartoCiudad © IGN/CNIG (CC BY 4.0).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import heapq
import json
import math
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
OUTPUT = HERE / "mobile_zones.json"
REPORT = HERE / "report.json"
SOURCE = HERE / "fuente.json"

OSM_CACHE_DAYS = 30            # los datos de OpenStreetMap se vuelven a descargar al mes
MIN_KM_RATIO = 0.9             # salvaguarda: no publicar si se sitúan menos km que el 90 % de antes
MIN_ZONES = 300                # salvaguarda: el PDF debe traer al menos estos tramos

DGT_PAGE = "https://www.dgt.es/conoce-el-estado-del-trafico/vigilancia-y-control/equipos-y-tramos-de-vigilancia/"
DGT_RADARS_XML = "https://nap.dgt.es/datex2/dgt/PredefinedLocationsPublication/radares/content.xml"
CARTOCIUDAD = "https://www.cartociudad.es/geocoder/api/geocoder/candidates"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "AvisadorRadares-tools/0.1 (generador de tramos de radar movil)"

# Descargas de Overpass que han fallado en esta ejecución. Si hay alguna no se publica nada: una
# provincia entera podría quedarse sin tramos por una caída pasajera del servidor.
OVERPASS_FAILURES: list[str] = []


def overpass_unavailable(what: str, last: Exception) -> RuntimeError:
    OVERPASS_FAILURES.append(f"{what}: {last}")
    return RuntimeError(f"Overpass no responde: {last}")

# Nombre de provincia tal y como lo escribe la DGT → códigos ISO 3166-2 que puede tener en OSM
# (las comunidades uniprovinciales pueden estar etiquetadas con el código de la comunidad).
PROVINCES = {
    "Albacete": ["AB"], "Alicante": ["A"], "Almería": ["AL"], "Asturias": ["O", "AS"],
    "Badajoz": ["BA"], "Baleares": ["PM", "IB"], "Barcelona": ["B"], "Burgos": ["BU"],
    "Cantabria": ["S", "CB"], "Castellón": ["CS"], "Ceuta": ["CE"], "Ciudad Real": ["CR"],
    "Coruña, A": ["C"], "Cuenca": ["CU"], "Cáceres": ["CC"], "Cádiz": ["CA"], "Córdoba": ["CO"],
    "Girona": ["GI"], "Granada": ["GR"], "Guadalajara": ["GU"], "Huelva": ["H"], "Huesca": ["HU"],
    "Jaén": ["J"], "León": ["LE"], "Lleida": ["L"], "Lugo": ["LU"], "Madrid": ["M", "MD"],
    "Melilla": ["ML"], "Murcia": ["MU", "MC"], "Málaga": ["MA"], "Navarra": ["NA", "NC"],
    "Ourense": ["OR"], "Palencia": ["P"], "Palmas, Las": ["GC"], "Pontevedra": ["PO"],
    "Rioja, La": ["LO", "RI"], "Salamanca": ["SA"], "Santa Cruz de Tenerife": ["TF"],
    "Segovia": ["SG"], "Sevilla": ["SE"], "Soria": ["SO"], "Tarragona": ["T"], "Teruel": ["TE"],
    "Toledo": ["TO"], "Valencia": ["V"], "Valladolid": ["VA"], "Zamora": ["ZA"],
    "Zaragoza": ["Z"], "Ávila": ["AV"], "Álava": ["VI"], "Gipuzkoa": ["SS"], "Bizkaia": ["BI"],
}

# Código INE de cada provincia: CartoCiudad lo devuelve en cada resultado ("provinceCode").
INE = {
    "Álava": "01", "Albacete": "02", "Alicante": "03", "Almería": "04", "Ávila": "05",
    "Badajoz": "06", "Baleares": "07", "Barcelona": "08", "Burgos": "09", "Cáceres": "10",
    "Cádiz": "11", "Castellón": "12", "Ciudad Real": "13", "Córdoba": "14", "Coruña, A": "15",
    "Cuenca": "16", "Girona": "17", "Granada": "18", "Guadalajara": "19", "Gipuzkoa": "20",
    "Huelva": "21", "Huesca": "22", "Jaén": "23", "León": "24", "Lleida": "25", "Rioja, La": "26",
    "Lugo": "27", "Madrid": "28", "Málaga": "29", "Murcia": "30", "Navarra": "31", "Ourense": "32",
    "Asturias": "33", "Palencia": "34", "Palmas, Las": "35", "Pontevedra": "36", "Salamanca": "37",
    "Santa Cruz de Tenerife": "38", "Cantabria": "39", "Segovia": "40", "Sevilla": "41",
    "Soria": "42", "Tarragona": "43", "Teruel": "44", "Toledo": "45", "Valencia": "46",
    "Valladolid": "47", "Bizkaia": "48", "Zamora": "49", "Zaragoza": "50", "Ceuta": "51",
    "Melilla": "52",
}

# Nombre de provincia en CartoCiudad, cuando no coincide con el de la DGT. El filtro por nombre
# falla con tildes (Ávila), así que sólo se usa como segundo intento.
CARTOCIUDAD_PROVINCE = {
    "Alicante": "Alacant/Alicante", "Castellón": "Castelló/Castellón", "Valencia": "València/Valencia",
    "Baleares": "Illes Balears", "Coruña, A": "A Coruña", "Palmas, Las": "Las Palmas",
    "Rioja, La": "La Rioja", "Álava": "Araba/Álava",
}

ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6"}

# Tolerancias (metros).
ANCHOR_MAX_OFF_ROAD = 150      # un punto de referencia debe estar a menos de esto de la carretera
DENSIFY_STEP = 50              # distancia máxima entre vértices del grafo
BRIDGE_GAP = 1000              # huecos que se cosen (rotondas, travesías sin la referencia de la vía)
BRIDGE_COST = 3.0              # coste relativo de esos puentes frente a la carretera real
ANCHOR_VOTE_KM = 20            # una referencia se contrasta con las que hay a menos de 20 km
TWIN_KM = 0.05                 # referencias a menos de 50 m de km se consideran el mismo punto
CHAIN_MAX_GAP_KM = 3           # alternativa sin trazado: puntos de referencia como mucho cada 3 km
MIN_PIECE_KM = 2               # trozo mínimo cuando un tramo sólo se puede situar en parte
LOCATE_MAX_ERROR = 1500        # error máximo admitido al situar un km
SIMPLIFY_TOLERANCE = 15        # Douglas-Peucker sobre el trazado final


# --------------------------------------------------------------------------- utilidades

def log(*args):
    print(*args, flush=True)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm_road(name: str) -> str:
    """'N-II' → 'N-2', 'N-IV' → 'N-4', 'n-iia' → 'N-2A', 'A-2' → 'A-2'. Sirve para comparar vías."""
    s = name.strip().upper().replace(" ", "")
    # Los numerales largos primero: con «I» delante, «N-IV» se leería como «I» + sufijo «V».
    m = re.fullmatch(r"N-(III|II|IV|VI|V|I)([A-Z]?)", s)
    if m:
        s = f"N-{ROMAN[m.group(1)]}{m.group(2)}"
    return s


def canonical_province(raw: str) -> str | None:
    """El PDF trae caracteres rotos ('Almer�a'): se casa con la lista usando '.' como comodín."""
    pattern = re.escape(raw.strip()).replace("�", ".")
    for name in PROVINCES:
        if re.fullmatch(pattern, name):
            return name
    return None


def province_key(name: str) -> str:
    """Palabra clave para comparar con la provincia que devuelve CartoCiudad."""
    base = strip_accents(name.split(",")[0]).lower()
    return {"baleares": "balears"}.get(base, base)


def http_get(url: str, *, data: bytes | None = None, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout).read()


def cached(name: str, fetch, max_age_days: float | None = None) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists() and (max_age_days is None or time.time() - path.stat().st_mtime < max_age_days * 86400):
        return path.read_bytes()
    body = fetch()
    path.write_bytes(body)
    return body


def distance(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# --------------------------------------------------------------------------- 1. PDF de la DGT

@dataclass
class Zone:
    province: str
    road: str
    pk_start: float
    pk_end: float
    pieces: list["Piece"] = field(default_factory=list)
    problem: str | None = None


@dataclass
class Piece:
    """Parte situada de un tramo (un tramo puede quedar en varios trozos si hay huecos)."""
    pk_start: float
    pk_end: float
    path: list[tuple[float, float]]
    approx: bool = False


def download_pdf() -> tuple[str, bytes]:
    """URL y contenido del PDF de radares vigente en la web de la DGT."""
    page = retry(DGT_PAGE).decode("utf-8", "replace")
    links = re.findall(r'href="([^"]*INFORME_CINEMOMETROS[^"]*\.pdf)"', page, re.I)
    if not links:
        sys.exit("No se encuentra el PDF INFORME_CINEMOMETROS en la página de la DGT")
    url = urllib.parse.urljoin(DGT_PAGE, links[0])
    log("PDF de la DGT:", url)
    return url, retry(url)


def parse_pdf(pdf_bytes: bytes) -> tuple[list[Zone], str | None]:
    import io

    import pdfplumber

    row = re.compile(
        r"^(?P<prov>.+?) (?P<road>\S+) Radar (?P<tipo>M.vil|Fijo|Tramo) "
        r"(?P<pk>[\d.]+(?: - [\d.]+)?) (?P<sentido>Creciente|Decreciente|Ambos)"
        r"(?: (?P<fecha>\d\d/\d\d/\d{4}))?\s*$"
    )
    zones, unparsed, report_date = [], [], None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                line = line.strip()
                if not line or line.startswith("PROVINCIA") or re.fullmatch(r"\(?[\d.,]+ m\)?", line):
                    continue
                m = row.match(line)
                if not m:
                    unparsed.append(line)
                    continue
                report_date = report_date or m["fecha"]
                if not m["tipo"].startswith("M") or " - " not in m["pk"]:
                    continue
                province = canonical_province(m["prov"])
                if province is None:
                    unparsed.append(line)
                    continue
                a, b = sorted(map(float, m["pk"].split(" - ")))
                zones.append(Zone(province, m["road"], a, b))
    if unparsed:
        log(f"Aviso: {len(unparsed)} líneas del PDF sin reconocer, p. ej. {unparsed[:3]}")
    return merge_contiguous(zones), report_date


def merge_contiguous(zones: list[Zone]) -> list[Zone]:
    """Une tramos seguidos de la misma vía (p. ej. 340,7-347,2 y 347,2-391,0)."""
    zones = sorted(zones, key=lambda z: (z.province, norm_road(z.road), z.pk_start))
    merged: list[Zone] = []
    for z in zones:
        last = merged[-1] if merged else None
        if (last and last.province == z.province and norm_road(last.road) == norm_road(z.road)
                and z.pk_start - last.pk_end <= 0.2):
            last.pk_end = max(last.pk_end, z.pk_end)
        else:
            merged.append(z)
    return merged


# --------------------------------------------------------------------------- 2. OpenStreetMap

def fetch_osm(province: str, roads: list[str]) -> dict:
    refs = sorted({r for road in roads for r in (road, norm_road(road))})
    rx = "|".join(re.escape(r) for r in refs)
    isos = "|".join(PROVINCES[province])
    query = f"""[out:json][timeout:600];
area["ISO3166-2"~"^ES-({isos})$"]->.a;
(
  way["highway"]["ref"~"(^|;) *({rx}) *(;|$)",i](area.a);
  node["highway"="milestone"](area.a);
);
out geom;"""
    key = hashlib.sha1(query.encode()).hexdigest()[:12]

    def fetch():
        last = None
        for endpoint in OVERPASS:
            for attempt in range(3):
                try:
                    return http_get(endpoint, data=urllib.parse.urlencode({"data": query}).encode(), timeout=700)
                except Exception as e:  # noqa: BLE001 - se reintenta con otro servidor
                    last = e
                    log(f"  Overpass {endpoint} falló ({e}); reintento")
                    time.sleep(20 * (attempt + 1))
        raise overpass_unavailable(province, last)

    return json.loads(cached(f"osm_{PROVINCES[province][0]}_{key}.json", fetch, OSM_CACHE_DAYS))


def fetch_network(south: float, west: float, north: float, east: float) -> dict:
    """Carreteras principales de un rectángulo, sin filtrar por referencia. Se usa cuando OSM no
    etiqueta la vía con su número (o su trazado está cortado): se sigue la red real entre puntos km."""
    bbox = f"{south:.2f},{west:.2f},{north:.2f},{east:.2f}"
    query = f"""[out:json][timeout:300];
way["highway"~"^(motorway|trunk|primary|secondary|tertiary|unclassified)(_link)?$"]({bbox});
out geom;"""

    def fetch():
        last = None
        for endpoint in OVERPASS:
            for attempt in range(3):
                try:
                    return http_get(endpoint, data=urllib.parse.urlencode({"data": query}).encode(), timeout=400)
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(15 * (attempt + 1))
        raise overpass_unavailable(f"red viaria {bbox}", last)

    return json.loads(cached(f"net_{bbox.replace(',', '_')}.json", fetch, OSM_CACHE_DAYS))


class RoadGraph:
    """Grafo no dirigido de una carretera (todas sus vías OSM), densificado cada 50 m."""

    def __init__(self, ways: list[list[tuple[float, float]]]):
        self.coords: list[tuple[float, float]] = []
        self.index: dict[tuple[int, int], int] = {}
        self.adj: dict[int, list[tuple[int, float]]] = {}
        self.grid: dict[tuple[int, int], list[int]] = {}
        for geometry in ways:
            prev = None
            for lat, lon in geometry:
                node = self._node(lat, lon)
                if prev is not None and prev != node:
                    self._densified_edge(prev, node)
                prev = node
        self._bridge_gaps()

    def _node(self, lat: float, lon: float, *, shared: bool = True) -> int:
        key = (round(lat * 1e6), round(lon * 1e6))
        if shared and key in self.index:
            return self.index[key]
        n = len(self.coords)
        self.coords.append((lat, lon))
        self.adj[n] = []
        if shared:
            self.index[key] = n
        self.grid.setdefault((int(lat * 200), int(lon * 200)), []).append(n)
        return n

    def _edge(self, a: int, b: int):
        d = distance(*self.coords[a], *self.coords[b])
        self.adj[a].append((b, d))
        self.adj[b].append((a, d))

    def _densified_edge(self, a: int, b: int):
        (la, lo), (lb, lob) = self.coords[a], self.coords[b]
        steps = int(distance(la, lo, lb, lob) // DENSIFY_STEP)
        prev = a
        for i in range(1, steps + 1):
            t = i / (steps + 1)
            mid = self._node(la + (lb - la) * t, lo + (lob - lo) * t, shared=False)
            self._edge(prev, mid)
            prev = mid
        self._edge(prev, b)

    def _bridge_gaps(self):
        """En OSM las rotondas no suelen llevar la referencia de la carretera, así que el grafo
        queda partido. Se une cada extremo suelto con el nodo más cercano de otro trozo."""
        parent = list(range(len(self.coords)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, edges in self.adj.items():
            for b, _ in edges:
                parent[find(a)] = find(b)
        for n in [n for n, edges in self.adj.items() if len(edges) == 1]:
            lat, lon = self.coords[n]
            best = None
            cx, cy = int(lat * 200), int(lon * 200)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for m in self.grid.get((cx + dx, cy + dy), ()):
                        if find(m) == find(n):
                            continue
                        d = distance(lat, lon, *self.coords[m])
                        if d <= BRIDGE_GAP and (best is None or d < best[1]):
                            best = (m, d)
            if best:
                # Mucho más «cara» que la carretera real: en enlaces con muchos extremos sueltos, un
                # puente recto podría servir de atajo y sacar el trazado de la vía.
                self.adj[n].append((best[0], best[1] * BRIDGE_COST))
                self.adj[best[0]].append((n, best[1] * BRIDGE_COST))
                parent[find(n)] = find(best[0])

    def nearest(self, lat: float, lon: float, max_m: float) -> tuple[int, float] | None:
        cx, cy = int(lat * 200), int(lon * 200)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n in self.grid.get((cx + dx, cy + dy), ()):
                    d = distance(lat, lon, *self.coords[n])
                    if d <= max_m and (best is None or d < best[1]):
                        best = (n, d)
        return best

    def dijkstra(self, source: int) -> tuple[dict[int, float], dict[int, int]]:
        dist, prev, heap = {source: 0.0}, {}, [(0.0, source)]
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist[n]:
                continue
            for m, w in self.adj[n]:
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m], prev[m] = nd, n
                    heapq.heappush(heap, (nd, m))
        return dist, prev


# --------------------------------------------------------------------------- 3. puntos de referencia

@dataclass
class Anchor:
    km: float
    lat: float
    lon: float
    source: str
    node: int = -1


def osm_milestones(osm: dict, road: str) -> list[Anchor]:
    out = []
    target = norm_road(road)
    for e in osm["elements"]:
        tags = e.get("tags", {})
        if e["type"] != "node" or tags.get("highway") != "milestone":
            continue
        refs = {norm_road(r) for r in re.split(r"\s*;\s*", tags.get("ref", "")) if r}
        m = re.match(r"\s*([\d]+(?:[.,]\d+)?)", tags.get("distance", "") or tags.get("pk", ""))
        if target in refs and m:
            out.append(Anchor(float(m.group(1).replace(",", ".")), e["lat"], e["lon"], "osm"))
    return out


def dgt_radar_anchors() -> dict[str, list[Anchor]]:
    """Radares fijos de la DGT: carretera, km y coordenadas (sirven de referencia)."""
    body = cached(f"dgt_radares_{date.today():%Y%m%d}.xml", lambda: http_get(DGT_RADARS_XML))
    ns = {"d": "http://datex2.eu/schema/1_0/1_0"}
    out: dict[str, list[Anchor]] = {}
    root = ET.fromstring(body)
    for loc in root.iter("{http://datex2.eu/schema/1_0/1_0}predefinedLocation"):
        if not loc.get("id", "").startswith("GUID_CABINA"):
            continue
        lat = loc.find(".//d:pointCoordinates/d:latitude", ns)
        lon = loc.find(".//d:pointCoordinates/d:longitude", ns)
        road = loc.find(".//d:referencePoint/d:roadNumber", ns)
        dist = loc.find(".//d:referencePoint/d:referencePointDistance", ns)
        if None in (lat, lon, road, dist):
            continue
        out.setdefault(norm_road(road.text), []).append(
            Anchor(float(dist.text) / 1000, float(lat.text), float(lon.text), "dgt"))
    return out


def retry(url: str, attempts: int = 4) -> bytes:
    for i in range(attempts):
        try:
            return http_get(url, timeout=30)
        except Exception:  # noqa: BLE001
            if i == attempts - 1:
                raise
            time.sleep(2 * (i + 1))
    raise AssertionError


def cartociudad(road: str, km: int, province: str) -> Anchor | None:
    """Punto «carretera km» del geocodificador CartoCiudad (IGN).

    Ojo: con la palabra «km» en la consulta la búsqueda difusa falla; «N-432 416» funciona.
    Se busca primero sin filtro y se comprueba la provincia por su código INE; si no aparece
    entre los resultados, se repite filtrando por el nombre de la provincia."""
    code = INE[province]
    names = list(dict.fromkeys([road, norm_road(road)]))
    attempts = [{"q": f"{name} {km}", "limit": 20} for name in names]
    attempts += [{"q": f"{name} {km}", "limit": 10, "provincia_filter": CARTOCIUDAD_PROVINCE.get(province, province)}
                 for name in names]
    for params in attempts:
        url = CARTOCIUDAD + "?" + urllib.parse.urlencode(params)
        try:
            body = cached("cc3_" + hashlib.sha1(url.encode()).hexdigest()[:16] + ".json", lambda url=url: retry(url))
            results = json.loads(body)
        except Exception as e:  # noqa: BLE001 - un km sin respuesta no invalida el resto
            log(f"  CartoCiudad sin respuesta para {params['q']}: {e}")
            continue
        for r in results or []:
            m = re.match(r"^(\S+) km (\d+)\b", r.get("address") or "")
            if (r.get("type") == "portal" and m and norm_road(m.group(1)) == norm_road(road)
                    and int(m.group(2)) == km and r.get("provinceCode") == code):
                return Anchor(float(km), r["lat"], r["lng"], "cartociudad")
    return None


# --------------------------------------------------------------------------- 4. situar los tramos

class RoadLocator:
    def __init__(self, graph: RoadGraph, anchors: list[Anchor]):
        self.graph = graph
        self.anchors: list[Anchor] = []
        for a in anchors:
            hit = graph.nearest(a.lat, a.lon, ANCHOR_MAX_OFF_ROAD)
            if hit:
                a.node = hit[0]
                self.anchors.append(a)
        self.anchors.sort(key=lambda a: a.km)
        self._sp: dict[int, tuple[dict[int, float], dict[int, int]]] = {}
        self.discarded = self._drop_inconsistent()

    def sp(self, node: int):
        if node not in self._sp:
            self._sp[node] = self.graph.dijkstra(node)
        return self._sp[node]

    def consistent(self, a: Anchor, b: Anchor) -> bool:
        """La distancia por la carretera entre dos referencias encaja con su diferencia de km."""
        d = self.sp(a.node)[0].get(b.node)
        if d is None:
            return False
        span = abs(b.km - a.km) * 1000
        if span < 300:
            return d < 800
        return 0.7 <= d / span <= 1.4

    def agrees(self, a: Anchor, b: Anchor) -> bool | None:
        """Versión estricta de consistent(): error absoluto ≤ 250 m + 10 % de la distancia.
        None si no hay camino entre ambas (trazado OSM cortado): no es prueba ni a favor ni en contra."""
        d = self.sp(a.node)[0].get(b.node)
        if d is None:
            return None
        span = abs(b.km - a.km) * 1000
        return abs(d - span) <= 250 + 0.1 * span

    def _drop_inconsistent(self) -> list[Anchor]:
        """Quita referencias erróneas (p. ej. un «km 0» de CartoCiudad en el otro extremo de la
        vía, o un radar con un km que no corresponde a su posición).

        1. Regla local: una referencia que no encaja con su vecina anterior ni con la siguiente,
           mientras esas dos sí encajan entre sí, sobra. Las gemelas (mismo km, p. ej. los dos
           radares de un punto, uno por sentido) no cuentan como vecinas: se avalarían entre sí.
        2. Votación: una referencia que no encaja con la mayoría de las cercanas, sobra."""

        def vote(x: Anchor) -> float:
            others = [y for y in self.anchors if y is not x and TWIN_KM <= abs(y.km - x.km) <= ANCHOR_VOTE_KM]
            votes = [v for y in others if (v := self.agrees(*sorted((x, y), key=lambda r: r.km))) is not None]
            if len(votes) < 2:
                return 1.0
            return sum(votes) / len(votes)

        dropped = []
        while len(self.anchors) >= 3:
            a = self.anchors
            local_bad = []
            for i, x in enumerate(a):
                prev = next((a[j] for j in range(i - 1, -1, -1) if x.km - a[j].km >= TWIN_KM), None)
                nxt = next((a[j] for j in range(i + 1, len(a)) if a[j].km - x.km >= TWIN_KM), None)
                if (prev and nxt and self.agrees(prev, x) is False and self.agrees(x, nxt) is False
                        and self.agrees(prev, nxt) is True):
                    local_bad.append(i)
            candidates = local_bad or range(len(a))
            scores = {i: vote(a[i]) for i in candidates}
            worst = min(scores, key=scores.get)
            if not local_bad and scores[worst] >= 0.5:
                break
            dropped.append(a.pop(worst))
        return dropped

    def _pairs(self, km: float):
        """Parejas de referencias candidatas: primero las que rodean al km, luego las más cercanas."""
        below = [a for a in self.anchors if a.km <= km][::-1][:4]
        above = [a for a in self.anchors if a.km > km][:4]
        pairs = [(lo, hi) for lo in below for hi in above]
        pairs.sort(key=lambda p: p[1].km - p[0].km)
        near = sorted(self.anchors, key=lambda a: abs(a.km - km))[:5]
        pairs += [(a, b) for i, a in enumerate(near) for b in near[i + 1:] if abs(a.km - b.km) >= 1]
        return pairs

    def locate(self, km: float) -> tuple[int, float] | None:
        """Nodo del grafo que corresponde al km, y error estimado en metros."""
        for a in self.anchors:
            if abs(a.km - km) < 0.1:
                return a.node, abs(a.km - km) * 1000
        for a, b in self._pairs(km):
            if a.node == b.node:
                continue
            da, _ = self.sp(a.node)
            db, _ = self.sp(b.node)
            d_ab = da.get(b.node)
            span = abs(b.km - a.km) * 1000
            if d_ab is None or span == 0 or not 0.75 <= d_ab / span <= 1.35:
                continue  # no conectadas o referencias incoherentes
            scale = d_ab / span
            ta, tb = abs(km - a.km) * 1000 * scale, abs(km - b.km) * 1000 * scale
            best, best_cost = None, math.inf
            for n, dn in da.items():
                dbn = db.get(n)
                if dbn is None:
                    continue
                cost = abs(dn - ta) + abs(dbn - tb)
                if cost < best_cost:
                    best, best_cost = n, cost
            if best is not None and best_cost <= LOCATE_MAX_ERROR:
                return best, best_cost
        return None

    def path(self, a: int, b: int) -> list[tuple[float, float]] | None:
        dist, prev = self.sp(a)
        if b not in dist:
            return None
        nodes = [b]
        while nodes[-1] != a:
            nodes.append(prev[nodes[-1]])
        return [self.graph.coords[n] for n in reversed(nodes)]


def simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas-Peucker en metros (proyección local equirectangular)."""
    if len(points) < 3:
        return points
    lat0 = math.radians(points[0][0])
    xy = [(lon * 111_320 * math.cos(lat0), lat * 110_540) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy) or 1e-9
        worst, worst_d = None, tolerance
        for k in range(i + 1, j):
            x, y = xy[k]
            d = abs(dy * (x - x1) - dx * (y - y1)) / length
            if d > worst_d:
                worst, worst_d = k, d
        if worst is not None:
            keep[worst] = True
            stack += [(i, worst), (worst, j)]
    return [p for p, k in zip(points, keep) if k]


def path_length(points) -> float:
    return sum(distance(*points[i], *points[i + 1]) for i in range(len(points) - 1))


def uncovered(z: Zone, min_km: float = 0.0) -> list[tuple[float, float]]:
    """Rangos de km del tramo que aún no tienen trazado."""
    gaps, pos = [], z.pk_start
    for p in sorted(z.pieces, key=lambda p: p.pk_start):
        if p.pk_start - pos > min_km:
            gaps.append((pos, p.pk_start))
        pos = max(pos, p.pk_end)
    if z.pk_end - pos > min_km:
        gaps.append((pos, z.pk_end))
    return gaps


def keep_piece(z: Zone, a: float, b: float) -> bool:
    """Trozos de al menos MIN_PIECE_KM, o el tramo entero si es más corto."""
    return b - a >= MIN_PIECE_KM or (a <= z.pk_start + 0.2 and b >= z.pk_end - 0.2)


def process_province(province: str, zones: list[Zone], dgt_anchors: dict[str, list[Anchor]]):
    log(f"{province}: {len(zones)} tramos")
    osm = fetch_osm(province, [z.road for z in zones])
    by_road: dict[str, list[list[tuple[float, float]]]] = {}
    for e in osm["elements"]:
        if e["type"] == "way" and "geometry" in e:
            geometry = [(p["lat"], p["lon"]) for p in e["geometry"]]
            for ref in re.split(r"\s*;\s*", e["tags"].get("ref", "")):
                by_road.setdefault(norm_road(ref), []).append(geometry)

    for road in sorted({norm_road(z.road) for z in zones}):
        road_zones = [z for z in zones if norm_road(z.road) == road]
        kms = set()
        for z in road_zones:
            kms.update({round(z.pk_start), round(z.pk_end)})
            kms.update(range(math.ceil(z.pk_start), math.floor(z.pk_end) + 1, 5))
        with ThreadPoolExecutor(6) as ex:
            cc = [a for a in ex.map(lambda k: cartociudad(road_zones[0].road, k, province), sorted(kms)) if a]
        anchors = osm_milestones(osm, road) + cc + dgt_anchors.get(road, [])

        ways = by_road.get(road)
        locator = RoadLocator(RoadGraph(ways), anchors) if ways else None
        for z in road_zones:
            if locator:
                trace_on_graph(z, locator)
            # Lo que no se ha podido trazar con la vía etiquetada se intenta siguiendo la red
            # viaria entre puntos km y, si tampoco, uniendo los puntos en línea recta.
            for a, b in uncovered(z, MIN_PIECE_KM):
                pts = range_anchors(z, a, b, anchors, province)
                try:
                    trace_on_network(z, a, b, pts)
                except Exception as e:  # noqa: BLE001 - se sigue con la alternativa
                    log(f"  red viaria no disponible para {z.road} {a:.1f}-{b:.1f}: {e}")
                for c, d in uncovered(z, MIN_PIECE_KM):
                    if c >= a - 0.01 and d <= b + 0.01:
                        anchor_chain(z, c, d, pts)
            if not z.pieces:
                z.problem = ("carretera no encontrada en OpenStreetMap" if not ways else
                             f"sin referencias suficientes ({len(locator.anchors)} puntos en la vía)")


def trace_on_graph(z: Zone, locator: RoadLocator, a: float | None = None, b: float | None = None,
                   ratio: tuple[float, float] = (0.6, 1.6)):
    """Recorta el trazado OSM del tramo. Une, en orden de km, el inicio, las referencias
    intermedias y el fin; donde dos puntos seguidos no encajan (hueco en OSM, referencia
    errónea) el tramo se parte y se conservan los trozos que sí se pueden trazar."""
    a = z.pk_start if a is None else a
    b = z.pk_end if b is None else b
    points: list[tuple[float, int]] = []
    start = locator.locate(a)
    if start:
        points.append((a, start[0]))
    points += [(x.km, x.node) for x in locator.anchors if a < x.km < b]
    end = locator.locate(b)
    if end:
        points.append((b, end[0]))
    points.sort()

    def close(piece_start: float, piece_end: float, coords: list[tuple[float, float]]):
        if len(coords) >= 2 and keep_piece(z, piece_start, piece_end):
            z.pieces.append(Piece(piece_start, piece_end, simplify(coords, SIMPLIFY_TOLERANCE)))

    if not points:
        return
    piece_start, coords = points[0][0], [locator.graph.coords[points[0][1]]]
    piece_end = piece_start
    for (k1, n1), (k2, n2) in zip(points, points[1:]):
        segment = locator.path(n1, n2) if n1 != n2 else [locator.graph.coords[n1]]
        span = (k2 - k1) * 1000
        ok = segment is not None and (
            path_length(segment) < 1500 if span < 500 else ratio[0] <= path_length(segment) / span <= ratio[1])
        if ok:
            coords += segment[1:]
            piece_end = k2
        else:
            close(piece_start, piece_end, coords)
            piece_start = piece_end = k2
            coords = [locator.graph.coords[n2]]
    close(piece_start, piece_end, coords)


def range_anchors(z: Zone, a: float, b: float, anchors: list[Anchor], province: str) -> list[Anchor]:
    """Puntos km del rango [a, b] (con margen), pidiendo a CartoCiudad todos los km del rango."""
    first, last = math.floor(a) - 1, math.ceil(b) + 1
    with ThreadPoolExecutor(6) as ex:
        extra = [x for x in ex.map(lambda k: cartociudad(z.road, k, province), range(max(first, 0), last + 1)) if x]
    pts: dict[float, Anchor] = {}
    for x in anchors + extra:
        if a - 1.5 <= x.km <= b + 1.5:
            pts.setdefault(round(x.km, 1), Anchor(x.km, x.lat, x.lon, x.source))
    return [pts[k] for k in sorted(pts)]


def trace_on_network(z: Zone, a: float, b: float, pts: list[Anchor]):
    """Sigue la red viaria real (sin filtrar por referencia) entre los puntos km del rango."""
    if len(pts) < 2:
        return
    margin = 0.03
    south = min(p.lat for p in pts) - margin
    north = max(p.lat for p in pts) + margin
    west = min(p.lon for p in pts) - margin
    east = max(p.lon for p in pts) + margin
    # Rectángulos enormes (puntos erróneos muy lejos) no merecen la descarga.
    if (north - south) * (east - west) > 1.0:
        return
    net = fetch_network(south, west, north, east)
    ways = [[(q["lat"], q["lon"]) for q in e["geometry"]] for e in net["elements"]
            if e["type"] == "way" and "geometry" in e]
    if not ways:
        return
    before = len(z.pieces)
    # Más exigente que con la vía etiquetada: en la red general una ruta que se desvía por otras
    # calles o carreteras sale bastante más larga que la diferencia de km.
    trace_on_graph(z, RoadLocator(RoadGraph(ways), pts), a, b, ratio=(0.8, 1.3))
    # Sólo se aceptan los trozos que caen dentro del rango pedido.
    z.pieces[before:] = [q for q in z.pieces[before:] if q.pk_start >= a - 0.2 and q.pk_end <= b + 0.2]


def anchor_chain(z: Zone, a: float, b: float, pts: list[Anchor]):
    """Último recurso entre los km a y b: une los puntos kilométricos en línea recta.

    Se descartan los puntos incoherentes con sus vecinos y se parte la cadena donde haya huecos
    de más de CHAIN_MAX_GAP_KM."""
    chain = list(pts)

    def excess(i):
        """Cuánto más lejos están dos puntos seguidos de lo que permiten sus km."""
        p, q = chain[i], chain[i + 1]
        return distance(p.lat, p.lon, q.lat, q.lon) - (q.km - p.km) * 1000 * 1.2 - 300

    # Quita puntos incoherentes hasta que todos los saltos encajen con los km.
    while len(chain) >= 3:
        worst = max(range(len(chain) - 1), key=excess)
        if excess(worst) <= 0:
            break
        # De los dos extremos del salto, se quita el que también desentona con su otro vecino.
        left_bad = worst > 0 and excess(worst - 1) > 0
        chain.pop(worst if left_bad or worst + 2 >= len(chain) else worst + 1)
    if len(chain) < 2 or any(excess(i) > 0 for i in range(len(chain) - 1)):
        return

    runs, run = [], [chain[0]]
    for p, q in zip(chain, chain[1:]):
        if q.km - p.km <= CHAIN_MAX_GAP_KM:
            run.append(q)
        else:
            runs.append(run)
            run = [q]
    runs.append(run)
    for run in runs:
        lo, hi = max(run[0].km, a), min(run[-1].km, b)
        if len(run) >= 2 and hi > lo and keep_piece(z, lo, hi):
            z.pieces.append(Piece(lo, hi, [(p.lat, p.lon) for p in run], approx=True))


# --------------------------------------------------------------------------- main

def github_output(**values):
    """Pasa valores a los pasos siguientes de la GitHub Action (si se ejecuta en una)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def github_summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", help="PDF de la DGT ya descargado (si no, se descarga)")
    parser.add_argument("--only", help="códigos ISO de provincia separados por comas, para pruebas (no publica)")
    parser.add_argument("--if-changed", action="store_true",
                        help="no hacer nada si el PDF de la DGT es el mismo que el de la última publicación")
    args = parser.parse_args()

    if args.pdf:
        pdf_url, pdf = Path(args.pdf).resolve().as_uri(), Path(args.pdf).read_bytes()
    else:
        pdf_url, pdf = download_pdf()
    pdf_sha = hashlib.sha256(pdf).hexdigest()
    previous = json.loads(SOURCE.read_text(encoding="utf-8")) if SOURCE.exists() else {}
    if args.if_changed and previous.get("pdfSha256") == pdf_sha:
        log(f"El PDF de la DGT no ha cambiado (informe {previous.get('dgtReportDate')}): nada que hacer.")
        github_output(changed="false")
        return

    zones, report_date = parse_pdf(pdf)
    log(f"{len(zones)} tramos de radar móvil (tras unir contiguos), informe DGT {report_date}")
    if args.only:
        wanted = set(args.only.upper().split(","))
        zones = [z for z in zones if PROVINCES[z.province][0] in wanted]
    elif len(zones) < MIN_ZONES:
        sys.exit(f"El PDF sólo trae {len(zones)} tramos (mínimo {MIN_ZONES}): ¿ha cambiado su formato?")

    dgt_anchors = dgt_radar_anchors()
    provinces = sorted({z.province for z in zones})
    for province in provinces:
        try:
            process_province(province, [z for z in zones if z.province == province], dgt_anchors)
        except Exception as e:  # noqa: BLE001 - una provincia con problemas no para el resto
            log(f"  ERROR en {province}: {e}")
            for z in zones:
                if z.province == province and not z.pieces:
                    z.problem = f"error: {e}"

    pieces = [(z, p) for z in zones for p in sorted(z.pieces, key=lambda p: p.pk_start)]
    km_all = sum(z.pk_end - z.pk_start for z in zones)
    km_ok = sum(p.pk_end - p.pk_start for _, p in pieces)
    km_approx = sum(p.pk_end - p.pk_start for _, p in pieces if p.approx)
    full = sum(1 for z in zones if z.pieces and not uncovered(z, 0.5))
    partial = sum(1 for z in zones if z.pieces and uncovered(z, 0.5))
    summary = (f"Tramos DGT: {len(zones)} · completos {full} · en parte {partial} · sin situar "
               f"{len(zones) - full - partial}\n"
               f"Km situados: {km_ok:.0f} de {km_all:.0f} ({km_ok / km_all:.0%}), de ellos aproximados "
               f"(unidos por puntos km) {km_approx:.0f}")
    log("\n" + summary)

    if args.only:
        out, report_path = CACHE / "prueba_mobile_zones.json", CACHE / "prueba_report.json"
    else:
        if OVERPASS_FAILURES:
            sys.exit(f"Salvaguarda: Overpass no respondió en {len(OVERPASS_FAILURES)} descarga(s) "
                     f"({'; '.join(OVERPASS_FAILURES)}). No se publica nada; lo ya descargado queda "
                     f"en caché para el siguiente intento.")
        previous_km = previous.get("kmSituados")
        if previous_km and km_ok < previous_km * MIN_KM_RATIO:
            sys.exit(f"Salvaguarda: se han situado {km_ok:.0f} km frente a {previous_km:.0f} en la última "
                     f"publicación (mínimo {MIN_KM_RATIO:.0%}). No se publica nada.")
        out, report_path = OUTPUT, REPORT

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated": date.today().isoformat(),
        "dgtReportDate": report_date,
        "attribution": "DGT; © colaboradores de OpenStreetMap (ODbL); CartoCiudad © IGN/CNIG (CC BY 4.0)",
        "zones": [{
            "id": f"{PROVINCES[z.province][0]}-{z.road}-{p.pk_start:.1f}",
            "province": z.province,
            "road": z.road,
            "pkStart": round(p.pk_start, 3),
            "pkEnd": round(p.pk_end, 3),
            **({"approx": True} if p.approx else {}),
            "path": [round(v, 5) for c in p.path for v in c],
        } for z, p in pieces],
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    report = []
    for z in zones:
        gaps = uncovered(z, 0.5)
        if gaps:
            report.append({
                "province": z.province, "road": z.road, "pkStart": z.pk_start, "pkEnd": z.pk_end,
                "sinSituar": [[round(a, 1), round(b, 1)] for a, b in gaps],
                "problem": z.problem or "situado en parte",
            })
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.only:
        SOURCE.write_text(json.dumps({
            "pdfUrl": pdf_url,
            "pdfSha256": pdf_sha,
            "dgtReportDate": report_date,
            "generated": date.today().isoformat(),
            "tramos": len(zones),
            "kmTotal": round(km_all),
            "kmSituados": round(km_ok),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        github_output(changed="true", report_date=report_date or "", km=f"{km_ok:.0f}",
                      percent=f"{km_ok / km_all:.0%}")
        github_summary(f"### Tramos de radar móvil regenerados\n\nInforme DGT {report_date}\n\n```\n{summary}\n```")
    log(f"Fichero: {out} ({out.stat().st_size / 1e6:.1f} MB, {len(pieces)} trozos). Pendientes en {report_path.name}")


if __name__ == "__main__":
    main()
