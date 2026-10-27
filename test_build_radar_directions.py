"""Tests del cálculo del sentido de los radares. Ejecutar: python -m unittest discover -s . -p "test_*.py" """
import unittest

import build_radar_directions as d

# Carretera recta de prueba hacia el norte (km crecientes hacia el norte): 1 km ≈ 0,009046° de latitud.
KM = 1 / 110.54
LON = -3.0
EAST_15M = 15 / (111_320 * 0.766)  # 15 m al este a 40° de latitud


def at(km: float, lon: float = LON) -> tuple[float, float]:
    return 40.0 + km * KM, lon


def no_cartociudad(*_):
    return None


def radar(rid: str, km: float, positive: bool, lon: float = LON) -> d.Radar:
    return d.Radar(rid, False, [d.Point(*at(km, lon), road="A-1", km=km, positive=positive, province="Madrid")])


def km_marker(rid: str, km: float) -> d.Radar:
    """Otro radar de la misma carretera: sirve de punto km."""
    return d.Radar(rid, False, [d.Point(*at(km), road="A-1", km=km, positive=None)])


def two_way_road() -> d.WayIndex:
    return d.WayIndex([d.Way([at(0), at(10)], {"A-1"}, "primary", 0)])


def dual_carriageway() -> d.WayIndex:
    """Autovía: calzada norte en LON (sentido km crecientes) y calzada sur 15 m al este."""
    return d.WayIndex([
        d.Way([at(0), at(10)], {"A-1"}, "motorway", 1),
        d.Way([at(10, LON + EAST_15M), at(0, LON + EAST_15M)], {"A-1"}, "motorway", 1),
    ])


DATEX = """<?xml version="1.0" encoding="UTF-8"?>
<d2LogicalModel xmlns:_0="http://datex2.eu/schema/1_0/1_0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<payloadPublication><_0:predefinedLocationSet id="CinemometrosVelocidadMedia">
 <_0:predefinedLocation id="GUID_CVM_1">
  <_0:predefinedLocation xsi:type="_0:Linear">
   <_0:tpeglinearLocation>
    <_0:to><_0:pointCoordinates><_0:latitude>42.141</_0:latitude><_0:longitude>-4.6731</_0:longitude></_0:pointCoordinates></_0:to>
    <_0:from><_0:pointCoordinates><_0:latitude>42.215</_0:latitude><_0:longitude>-4.8479</_0:longitude></_0:pointCoordinates></_0:from>
   </_0:tpeglinearLocation>
   <_0:referencePointLinear>
    <_0:referencePointPrimaryLocation><_0:referencePoint><_0:roadNumber>CL-613</_0:roadNumber>
     <_0:directionRelative>positive</_0:directionRelative><_0:referencePointDistance>32700.0</_0:referencePointDistance>
    </_0:referencePoint></_0:referencePointPrimaryLocation>
    <_0:referencePointSecondaryLocation><_0:referencePoint><_0:roadNumber>CL-613</_0:roadNumber>
     <_0:directionRelative>positive</_0:directionRelative><_0:referencePointDistance>15410.0</_0:referencePointDistance>
    </_0:referencePoint></_0:referencePointSecondaryLocation>
   </_0:referencePointLinear>
  </_0:predefinedLocation>
 </_0:predefinedLocation>
</_0:predefinedLocationSet><_0:predefinedLocationSet id="CabinasCinemometro">
 <_0:predefinedLocation id="GUID_CABINACINEMOMETRO_2">
  <_0:predefinedLocation xsi:type="_0:Point">
   <_0:tpegpointLocation><_0:point><_0:pointCoordinates><_0:latitude>40.58943</_0:latitude><_0:longitude>-3.5830321</_0:longitude></_0:pointCoordinates></_0:point></_0:tpegpointLocation>
   <_0:referencePoint><_0:roadNumber>A-1</_0:roadNumber><_0:directionRelative>negative</_0:directionRelative>
    <_0:referencePointDistance>24000.0</_0:referencePointDistance>
    <_0:referencePointExtension><_0:ExtendedReferencePoint><_0:provinceINEIdentifier>28</_0:provinceINEIdentifier></_0:ExtendedReferencePoint></_0:referencePointExtension>
   </_0:referencePoint>
  </_0:predefinedLocation>
 </_0:predefinedLocation>
</_0:predefinedLocationSet></payloadPublication></d2LogicalModel>"""


class ParseTest(unittest.TestCase):
    def test_radares_fijos_y_de_tramo(self):
        radars = {r.id: r for r in d.parse_radars(DATEX.encode())}
        self.assertEqual(set(radars), {"CVM_1", "CABINACINEMOMETRO_2"})

        section = radars["CVM_1"]
        self.assertTrue(section.section)
        start, end = section.points
        self.assertEqual((start.lat, start.lon, start.km), (42.215, -4.8479, 32.7))  # «from» con su km de inicio
        self.assertEqual((end.lat, end.km), (42.141, 15.41))

        fixed = radars["CABINACINEMOMETRO_2"]
        self.assertFalse(fixed.section)
        p = fixed.points[0]
        self.assertEqual((p.road, p.km, p.positive, p.province), ("A-1", 24.0, False, "Madrid"))


class OrientTest(unittest.TestCase):
    def test_elige_el_sentido_de_la_via_hacia_los_km(self):
        self.assertEqual(d.orient(10, 30, 1), 10)
        self.assertEqual(d.orient(190, 30, 1), 10)

    def test_via_perpendicular(self):
        self.assertEqual(d.orient(100, 10, 1), 10)         # puntos km cercanos: se usa su rumbo
        self.assertIsNone(d.orient(100, 10, 4))            # lejanos: no se decide

    def test_sin_via(self):
        self.assertEqual(d.orient(None, 10, 2), 10)
        self.assertIsNone(d.orient(None, 10, 4))


class RoadTest(unittest.TestCase):
    def test_tangente_en_una_curva_suave(self):
        geometry = [at(0), at(1), (at(2)[0], LON + 0.002)]
        dist, seg, t = d.project(geometry, *at(0.5))
        self.assertLess(dist, 1)
        self.assertLess(d.angle_diff(d.tangent(geometry, seg, t), 0), 1)

    def test_prefiere_la_via_con_el_numero_de_la_carretera(self):
        index = d.WayIndex([
            d.Way([at(0, LON + EAST_15M), at(10, LON + EAST_15M)], {"A-1"}, "motorway", 1),
            d.Way([(40.0 + 5 * KM, LON - 0.01), (40.0 + 5 * KM, LON + 0.01)], {"M-40"}, "motorway", 1),
        ])
        hit = d.road_at(index, d.Point(*at(5), road="A-1"))
        self.assertTrue(hit.same_road)
        self.assertLess(d.angle_diff(hit.axis, 0), 1)


class SolveTest(unittest.TestCase):
    def solve(self, radars, index, senses=None):
        markers = [km_marker("m3", 3), km_marker("m7", 7)]
        stats = d.solve(radars + markers, index, cartociudad=no_cartociudad, senses=senses)
        return {r.id: r.directions for r in radars}, stats

    def test_crecientes_y_decrecientes_en_doble_sentido(self):
        dirs, _ = self.solve([radar("up", 5, True), radar("down", 5, False)], two_way_road())
        self.assertLess(d.angle_diff(dirs["up"][0], 0), 1)
        self.assertLess(d.angle_diff(dirs["down"][0], 180), 1)

    def test_autovia_cada_radar_en_su_calzada(self):
        dirs, stats = self.solve(
            [radar("up", 5, True), radar("down", 5, False, LON + EAST_15M)], dual_carriageway())
        self.assertLess(d.angle_diff(dirs["up"][0], 0), 1)
        self.assertLess(d.angle_diff(dirs["down"][0], 180), 1)
        self.assertEqual((stats["checked"], stats["agree"]), (2, 2))

    def test_radar_en_la_calzada_contraria_se_queda_sin_sentido(self):
        # La DGT dice «decreciente» pero la coordenada está en la calzada de km crecientes.
        dirs, stats = self.solve([radar("x", 5, False)], dual_carriageway())
        self.assertEqual(dirs["x"], [])
        self.assertEqual(stats["disagreements"], ["x"])

    def test_sin_puntos_km_no_hay_sentido(self):
        r = radar("solo", 5, True)
        d.solve([r], two_way_road(), cartociudad=no_cartociudad)
        self.assertEqual(r.directions, [])

    def test_el_pdf_contradice_o_dice_ambos(self):
        senses = {"A-1": [(5.0, "Ambos")]}
        dirs, _ = self.solve([radar("up", 5, True)], two_way_road(), senses)
        self.assertEqual(dirs["up"], [])
        dirs, _ = self.solve([radar("up", 5, True)], two_way_road(), {"A-1": [(5.05, "Creciente")]})
        self.assertLess(d.angle_diff(dirs["up"][0], 0), 1)

    def test_tramo_del_inicio_al_fin(self):
        section = d.Radar("t", True, [d.Point(*at(8), road="A-1", km=8), d.Point(*at(6), road="A-1", km=6)])
        d.solve([section], two_way_road(), cartociudad=no_cartociudad)
        self.assertTrue(all(d.angle_diff(x, 180) < 1 for x in section.directions), section.directions)


if __name__ == "__main__":
    unittest.main()
