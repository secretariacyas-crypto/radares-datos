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

Antes de publicar se ejecutan los tests del generador y una salvaguarda: si Overpass
(OpenStreetMap) no responde en alguna descarga, si los km situados bajan más de un 10 % respecto
a la publicación anterior, o si el PDF trae menos de 300 tramos (p. ej. porque la DGT cambia su
formato), no se publica nada y se abre una *issue*. La app sigue usando los últimos datos buenos.
Lo descargado se guarda en caché igualmente, así que el siguiente intento es más rápido.

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

## Sentido de los radares

[`radar_directions.json`](radar_directions.json) indica qué sentido de circulación vigila cada
radar fijo y de tramo de la DGT, como rumbo de brújula. La app lo usa para no avisar de los
radares del sentido contrario (en una autovía, los de la otra calzada quedan a 20-30 m).

**URL:** `https://raw.githubusercontent.com/secretariacyas-crypto/radares-datos/main/radar_directions.json`

La DGT no publica rumbos, sólo si un radar fijo vigila los km crecientes o decrecientes (los que
vigilan los dos sentidos aparecen dos veces) y, en los de tramo, su inicio y su fin.
[`build_radar_directions.py`](build_radar_directions.py):

1. Toma de OpenStreetMap la vía en la que está cada radar y su orientación local.
2. Radares fijos: averigua hacia dónde crecen los km con puntos kilométricos cercanos
   (CartoCiudad y otros radares de la misma carretera) y elige el sentido de la vía que
   corresponde. Radares de tramo: el sentido que va del inicio al fin.
3. Por prudencia, deja sin sentido (la app avisará en los dos) los radares con datos
   contradictorios: un sentido equivocado haría callar a la app ante un radar real.
   - Si el radar está sobre una calzada de sentido único de OSM que va al revés. Pasa con
     un 5 % de los radares; el PDF de la DGT confirma su sentido, así que lo más probable es
     que la coordenada esté en la otra calzada.
   - Si el PDF de la DGT da otro sentido o «Ambos».

Resultado actual: sentido para el 78 % de los radares fijos y 43 de los 47 de tramo. De los
radares sobre calzadas de sentido único, el 89 % coincide con la calzada; si eso bajara del 80 %,
o la cobertura del 70 %, no se publica nada y se abre una *issue*. Se recalcula cuando cambia el
inventario de radares de la DGT y el día 1 de cada mes.

```sh
python build_radar_directions.py --if-changed   # o sin opciones para recalcular siempre
```

```json
{"generated":"2026-10-27","radars":{"CABINACINEMOMETRO_120322":[189],"CVM_161274":[290,301]}}
```

Las claves son los identificadores de la DGT sin `GUID_`. Radares fijos: un rumbo (0 = norte,
90 = este). Radares de tramo: el rumbo al entrar y al salir (`null` si no se pudo calcular).

## Fuentes y licencia de los datos

- Tramos: Dirección General de Tráfico.
- Trazados y red viaria: © colaboradores de OpenStreetMap, disponibles bajo la
  [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/). Como base de datos
  derivada, `mobile_zones.json` se distribuye también bajo la ODbL.
- Puntos kilométricos: CartoCiudad © Instituto Geográfico Nacional / CNIG (CC BY 4.0).
- Radares y su sentido: Dirección General de Tráfico. `radar_directions.json` usa también
  OpenStreetMap y se distribuye bajo la ODbL.

Los tramos son orientativos: la DGT puede situar radares móviles en otros puntos.
