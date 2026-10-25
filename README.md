# Tramos de vigilancia con radar móvil (DGT) geolocalizados

La DGT publica los tramos de carretera donde realiza controles con radar móvil sólo como
«provincia, carretera, km inicial, km final», en un PDF
([*Puntos y tramos de control de velocidad*](https://www.dgt.es/conoce-el-estado-del-trafico/vigilancia-y-control/equipos-y-tramos-de-vigilancia/)).
Este repositorio los sitúa sobre el trazado real de cada carretera y publica el resultado en
[`mobile_zones.json`](mobile_zones.json), que usa la app *Avisador Radares*.

**URL de los datos:**
`https://raw.githubusercontent.com/secretariacyas-crypto/radares-datos/main/mobile_zones.json`

## Actualización automática

La GitHub Action [`regenerar.yml`](.github/workflows/regenerar.yml):

- **cada día** comprueba si la DGT ha publicado un PDF distinto (por su huella SHA-256) y, si
  es así, regenera los tramos;
- **el día 1 de cada mes** los regenera aunque el PDF no haya cambiado, para recoger las mejoras
  de OpenStreetMap y CartoCiudad;
- también se puede lanzar a mano desde *Actions → Regenerar tramos → Run workflow*.

Antes de publicar se ejecutan los tests del generador y una salvaguarda: si los km situados
bajan más de un 10 % respecto a la publicación anterior, o el PDF trae menos de 300 tramos (p.
ej. porque la DGT cambia su formato), no se publica nada y se abre una *issue*. La app sigue
usando los últimos datos buenos.

[`fuente.json`](fuente.json) indica qué PDF se usó y la cobertura obtenida;
[`report.json`](report.json) lista lo que no se pudo situar.

## Cómo se sitúan los tramos

[`build_mobile_zones.py`](build_mobile_zones.py):

1. Extrae del PDF los tramos de radar móvil y une los contiguos de la misma vía.
2. Descarga de OpenStreetMap el trazado de cada carretera por provincia.
3. Reúne puntos «km → coordenadas»: hitos kilométricos de OSM, el geocodificador CartoCiudad del
   IGN y los radares fijos de la DGT. Descarta los que no caen sobre la carretera y los que no
   encajan con sus vecinos.
4. Recorta la carretera entre el km inicial y el final; si el trazado OSM está cortado, conserva
   los trozos que sí se pueden situar.
5. Para lo que falta, sigue la red viaria real entre los puntos km (*map matching*) y, como
   último recurso, los une en línea recta (trozos marcados `approx`).

Validado con las incidencias en tiempo real de la DGT (que traen carretera, km y coordenadas):
el 98 % queda a menos de 60 m del trazado.

```sh
pip install pdfplumber
python -m unittest discover -s . -p "test_*.py"
python build_mobile_zones.py --if-changed      # o sin opciones para regenerar siempre
python build_mobile_zones.py --only GR,Z       # pruebas con algunas provincias (no publica)
```

## Formato

```json
{"generated":"2026-10-25","dgtReportDate":"03/08/2026","zones":[
  {"id":"GR-N-432-399.8","province":"Granada","road":"N-432","pkStart":399.82,"pkEnd":431.82,
   "path":[37.25,-3.77,37.26,-3.76]}]}
```

`path` es la lista plana `lat, lon, lat, lon…` del trazado. Un tramo de la DGT puede aparecer en
varios trozos si no se pudo situar entero.

## Fuentes y licencia de los datos

- Tramos: Dirección General de Tráfico.
- Trazados y red viaria: © colaboradores de OpenStreetMap, disponibles bajo la
  [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). Como base de datos
  derivada, `mobile_zones.json` se distribuye también bajo la ODbL.
- Puntos kilométricos: CartoCiudad © Instituto Geográfico Nacional / CNIG (CC BY 4.0).

Los tramos son orientativos: la DGT puede situar radares móviles en otros puntos.
