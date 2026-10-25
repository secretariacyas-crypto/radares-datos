"""Tests del generador de tramos. Ejecutar: python -m unittest discover -s . -p "test_*.py" """
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_mobile_zones as b

# Carretera recta de prueba hacia el norte: 1 km ≈ 0,009046° de latitud.
KM = 1 / 110.54
LON = -3.0


def at(km: float) -> tuple[float, float]:
    return 40.0 + km * KM, LON


def anchor(km: float, real_km: float | None = None, source: str = "test") -> b.Anchor:
    """Referencia que dice estar en `km` pero está físicamente en `real_km`."""
    lat, lon = at(km if real_km is None else real_km)
    return b.Anchor(km, lat, lon, source)


def straight_road(km_from: float, km_to: float) -> b.RoadGraph:
    return b.RoadGraph([[at(km_from), at(km_to)]])


class NormRoadTest(unittest.TestCase):
    def test_numerales_romanos(self):
        cases = {
            "N-I": "N-1", "N-II": "N-2", "N-III": "N-3", "N-IV": "N-4", "N-V": "N-5", "N-VI": "N-6",
            "N-IIa": "N-2A", "N-IVa": "N-4A", "n-iie": "N-2E", "N-340a": "N-340A", "A-2": "A-2",
        }
        for raw, expected in cases.items():
            self.assertEqual(b.norm_road(raw), expected, raw)


class AnchorCleaningTest(unittest.TestCase):
    def test_quita_radares_gemelos_mal_situados(self):
        # Dos radares (uno por sentido) dicen estar en el km 17,7 pero están en el 22,3.
        anchors = [anchor(12), anchor(13), anchor(17.7, 22.3, "dgt"), anchor(17.7, 22.3, "dgt"),
                   anchor(18), anchor(21), anchor(23), anchor(24)]
        locator = b.RoadLocator(straight_road(0, 30), anchors)
        self.assertEqual(sorted(a.km for a in locator.discarded), [17.7, 17.7])
        self.assertEqual([a.km for a in locator.anchors], [12, 13, 18, 21, 23, 24])

    def test_quita_km_0_en_el_otro_extremo(self):
        anchors = [anchor(0, 48), anchor(5), anchor(10), anchor(15), anchor(20)]
        locator = b.RoadLocator(straight_road(0, 50), anchors)
        self.assertEqual([a.km for a in locator.discarded], [0])

    def test_conserva_referencias_buenas(self):
        anchors = [anchor(k) for k in (0, 5, 10, 15, 20)] + [anchor(7.3, source="dgt")]
        locator = b.RoadLocator(straight_road(0, 25), anchors)
        self.assertEqual(locator.discarded, [])


class TraceTest(unittest.TestCase):
    def test_tramo_completo(self):
        z = b.Zone("Madrid", "M-1", 2.0, 12.0)
        locator = b.RoadLocator(straight_road(0, 20), [anchor(k) for k in (0, 5, 10, 15)])
        b.trace_on_graph(z, locator)
        self.assertEqual(len(z.pieces), 1)
        self.assertAlmostEqual(z.pieces[0].pk_start, 2.0)
        self.assertAlmostEqual(z.pieces[0].pk_end, 12.0)
        self.assertAlmostEqual(b.path_length(z.pieces[0].path), 10_000, delta=150)
        self.assertEqual(b.uncovered(z, 0.5), [])

    def test_hueco_en_osm_parte_el_tramo(self):
        # La vía tiene un hueco de 3 km (del 8 al 11) que no se puede coser.
        graph = b.RoadGraph([[at(0), at(8)], [at(11), at(20)]])
        locator = b.RoadLocator(graph, [anchor(k) for k in (0, 2, 4, 6, 8, 11, 13, 15, 17)])
        z = b.Zone("Madrid", "M-1", 1.0, 16.0)
        b.trace_on_graph(z, locator)
        spans = sorted((round(p.pk_start), round(p.pk_end)) for p in z.pieces)
        self.assertEqual(spans, [(1, 8), (11, 16)])
        self.assertEqual([(round(x), round(y)) for x, y in b.uncovered(z, 0.5)], [(8, 11)])

    def test_cadena_aproximada_se_parte_en_huecos_grandes(self):
        z = b.Zone("Madrid", "M-1", 0.0, 12.0)
        pts = [anchor(k) for k in (0, 1, 2, 3, 4, 9, 10, 11, 12)]  # hueco de 5 km
        b.anchor_chain(z, 0.0, 12.0, pts)
        self.assertEqual(sorted((p.pk_start, p.pk_end) for p in z.pieces), [(0, 4), (9, 12)])
        self.assertTrue(all(p.approx for p in z.pieces))


class AutomationTest(unittest.TestCase):
    """Lógica de la regeneración automática (sin red: se sustituye la descarga del PDF)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(b, "SOURCE", self.dir / "fuente.json"),
            mock.patch.object(b, "OUTPUT", self.dir / "mobile_zones.json"),
            mock.patch.object(b, "REPORT", self.dir / "report.json"),
            mock.patch.object(b, "download_pdf", return_value=("https://dgt/x.pdf", b"PDF-1")),
            mock.patch.object(b, "github_output"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def run_main(self, *args):
        with mock.patch.object(sys, "argv", ["build_mobile_zones.py", *args]):
            b.main()

    def test_sin_cambios_no_regenera(self):
        b.SOURCE.write_text(json.dumps({"pdfSha256": hashlib.sha256(b"PDF-1").hexdigest()}))
        with mock.patch.object(b, "parse_pdf") as parse:
            self.run_main("--if-changed")
        parse.assert_not_called()
        b.github_output.assert_called_once_with(changed="false")
        self.assertFalse(b.OUTPUT.exists())

    def test_pdf_con_formato_raro_no_publica(self):
        with mock.patch.object(b, "parse_pdf", return_value=([b.Zone("Madrid", "M-1", 0, 5)], "01/01/2027")):
            with self.assertRaises(SystemExit):
                self.run_main("--if-changed")
        self.assertFalse(b.OUTPUT.exists())
        self.assertFalse(b.SOURCE.exists())

    def test_fallo_de_overpass_no_publica(self):
        zones = [b.Zone("Madrid", f"M-{i}", 0, 5) for i in range(b.MIN_ZONES)]

        def caida(province, *_):
            raise b.overpass_unavailable(province, TimeoutError("timed out"))

        with mock.patch.object(b, "parse_pdf", return_value=(zones, "01/01/2027")), \
                mock.patch.object(b, "dgt_radar_anchors", return_value={}), \
                mock.patch.object(b, "process_province", side_effect=caida), \
                mock.patch.object(b, "OVERPASS_FAILURES", []):
            with self.assertRaises(SystemExit) as e:
                self.run_main()
        self.assertIn("Overpass", str(e.exception.code))
        self.assertFalse(b.OUTPUT.exists())
        self.assertFalse(b.SOURCE.exists())


if __name__ == "__main__":
    unittest.main()
