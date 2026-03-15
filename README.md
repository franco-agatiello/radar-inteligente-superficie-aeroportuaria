# Radar Inteligente de Superficie Aeroportuaria (RISA)

Sistema de vigilancia y prediccion de conflictos en superficie aeroportuaria, con visualizacion GIS, fusion de telemetria y monitoreo en tiempo real para aeronaves y vehiculos de apoyo (GSE).

## Objetivo

RISA busca mejorar la conciencia situacional en plataforma, calles de rodaje y pista mediante:

- Seguimiento de trafico en superficie con datos de telemetria.
- Prediccion de trayectorias en la red de movimiento aeroportuaria.
- Deteccion temprana de conflictos entre aeronaves, vehiculos y obstaculos.
- Visualizacion interactiva tipo radar con enfoque operativo.

## Funcionalidades principales

- Vista GIS de aeropuerto con overlays por sectores (runway, taxiway, apron).
- Modo torre (north-up) y modo cockpit AMMD (track-up).
- Alertas predictivas por severidad y TTC (time-to-conflict).
- Editor de reglas de seguridad (distancias por tipo, sector y velocidad).
- Editor de ajuste de zonas (buffer y opacidad por sector).
- Soporte de trafico en tierra simulado: vehiculos, FOD y wildlife.
- Dibujos diferenciados por tipo de vehiculo en el radar.

## Galeria de resultados

Las siguientes capturas muestran el comportamiento operativo del radar en escenarios de conflicto y seguimiento.

Guarda tus imagenes en docs/images con estos nombres para mantener la galeria sincronizada:

- 01-anillo-critico-trayectoria-peligrosa.png
- 02-anillo-sin-amenaza-trayectoria-segura.png
- 03-anillo-critico.-trayectoria critica.png
- 04-anillo-estable.png
- 05-anillo-critico-trayectoria-critica.png
- 06-anillo-semi-critico-trayectoria-critica.png

### 1) Alerta critica con trayectoria peligrosa

![01-anillo-critico-trayectoria-peligrosa](docs/images/01-anillo-critico-trayectoria-peligrosa.png)

Lectura: aeronave seleccionada con halo cian y anillo rojo dominante, indicando condicion de riesgo en el entorno inmediato mientras sigue una trayectoria proyectada la cual al encontrarse el color anaranjado indica una posible colision.

### 2) Trayectoria segura sin amenaza inmediata

![02-anillo-sin-amenaza-trayectoria-segura](docs/images/02-anillo-sin-amenaza-trayectoria-segura.png)

Lectura: se observan ramificaciones punteadas en azul que representan alternativas de movimiento seguro en nodos cercanos de la red de rodaje.

### 3) Escenario critico con trayectoria conflictiva

![03-anillo-critico.-trayectoria critica](docs/images/03-anillo-critico.-trayectoria%20critica.png)

Lectura: trayectoria critica con anillo rojo consolidado y capas de soporte, priorizando la atencion del operador sobre posible conflicto.

### 4) Estado estable de separacion

![04-anillo-estable](docs/images/04-anillo-estable.png)

Lectura: estado estable con predominio de anillos de menor severidad, apropiado para seguimiento normal de superficie.

### 5) Criticidad alta con soporte de datos operativos

![05-anillo-critico-trayectoria-critica](docs/images/05-anillo-critico-trayectoria-critica.png)

Lectura: combinacion de trayectoria critica y anillos de alta severidad, con panel de datos operativos para evaluar decision inmediata.

### 6) Contexto ampliado en condicion semi-critica

![06-anillo-semi-critico-trayectoria-critica](docs/images/06-anillo-semi-critico-trayectoria-critica.png)

Lectura: escenario semi-critico en contexto amplio, util para analizar trayectoria critica con mayor conciencia espacial alrededor del actor principal.

## Interpretacion visual rapida

- Halo cian: actor seleccionado o en seguimiento.
- Trazos punteados: trayectoria predictiva y posibles ramas.
- Anillo verde: zona de margen seguro.
- Anillo amarillo: advertencia temprana.
- Anillo rojo: condicion critica o conflicto de alta prioridad.
- Semi-critico: transicion entre advertencia y riesgo alto, requiere vigilancia activa.

## Stack tecnologico

- Python 3.11+
- PySide6 (GUI)
- Shapely (geometrias)
- NetworkX (grafo de rutas)
- Geopy (calculos geodesicos)
- Requests (telemetria OpenSky)

## Estructura del proyecto

```text
asmgcs/
  app/
  domain/
  fusion/
  infrastructure/
  physics/
  viewmodels/
  views/
data/
img/
tests/
main.py
```

## Instalacion

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## Ejecucion

```bash
python main.py
```

## Telemetria OpenSky (opcional)

Puedes usar credenciales por variables de entorno:

- `OPENSKY_USERNAME`
- `OPENSKY_PASSWORD`

O OAuth client credentials:

- `OPENSKY_CLIENT_ID`
- `OPENSKY_CLIENT_SECRET`

Si no hay acceso online, el sistema puede apoyarse en datos de cache local para demo.

## Tests

```bash
python -m unittest discover -s tests
```

## Flujo de uso recomendado

1. Elegir aeropuerto desde el menu inicial.
2. Revisar trafico, predicciones y alertas en vista principal.
3. Probar modo cockpit seleccionando una aeronave.
4. Ajustar reglas en Safety Criteria.
5. Ajustar buffers/opacidad en Zone Polish.
6. Volver a simular y comparar comportamiento.

## Roadmap

- Exportacion de reportes de eventos y metricas.
- Integracion de mas fuentes de telemetria.
- Persistencia de configuraciones por escenario.
- Mejoras de UX para analisis post-operacion.

## Autor

Franco Agatiello

## Licencia

FI. UNLP.
