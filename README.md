# 🌍 Bot de Telegram para traducir documentos

Bot de Telegram que recibe **PDF, EPUB, TXT o MD**, los traduce con el
**traductor web de Google (gratuito, sin API key)** y los devuelve traducidos.
También traduce mensajes de texto sueltos.

- **PDF** → conserva el diseño original: imágenes, tablas, colores, negritas y
  la posición del texto. La traducción se superpone exactamente donde estaba
  el texto original.
- **EPUB** → conserva capítulos, estilos CSS, imágenes, enlaces, orden de
  lectura y los índices (NCX y navegación). El resto de archivos se copia byte
  a byte.
- **TXT / MD** → texto plano traducido línea a línea.

Pensado también para **novelas web de gran volumen (+1000 capítulos)**: traduce
por lotes, se frena solo si Google limita la velocidad, guarda el progreso
capítulo a capítulo y **reanuda** donde se quedó.

---

## Instalación rápida (Windows)

1. Consigue un token:
   - Abre Telegram y habla con [@BotFather](https://t.me/BotFather).
   - Envía `/newbot`, elige nombre y usuario, y copia el token que te da
     (algo como `123456789:AA...`).

2. Haz doble clic en **`run.bat`**.
   - La primera vez crea el entorno virtual, instala dependencias y genera
     el archivo `.env`, y se cierra pidiendo que pegues el token.
   - Abre `.env` con el Bloc de notas, pega el token en
     `TELEGRAM_BOT_TOKEN=` y guarda.
   - Vuelve a ejecutar `run.bat`: el bot arranca y queda a la escucha.

### Instalación manual (Windows / Linux / macOS)

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # y edita el token
python bot.py
```

Requiere **Python 3.10 o superior** (probado con 3.11) y conexión a internet.
Al arrancar crea la carpeta `data/` con los archivos de trabajo, el progreso y
la caché de resultados.

---

## Desplegarlo en Render (GitHub → Render)

El bot funciona como **Web Service** (el plan gratuito vale) o como
**Background Worker** (de pago, más cómodo para trabajos de horas). Render
ejecuta `python bot.py`, que además abre un pequeño servidor HTTP de salud en
el puerto que Render asigna (`/health` y `/status`).

### 1. Sube el proyecto a GitHub

Desde la carpeta del proyecto (necesitas tener Git instalado y una cuenta en
GitHub):

```bash
git init
git add .
git commit -m "Bot traductor de documentos para Telegram"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
git push -u origin main
```

El archivo `.env` **no se sube nunca** (está en `.gitignore`): el token va
solo en Render. Puedes hacer el repositorio privado sin problema.

### 2. Crea el servicio en Render

1. Entra en [render.com](https://render.com) → **New +** → **Blueprint**.
2. Elige el repositorio: Render leerá **`render.yaml`** y propondrá el
   servicio `traductor-bot` (plan gratuito, `python bot.py`, health check
   `/health`).
3. Pega el token de @BotFather en la variable `TELEGRAM_BOT_TOKEN`
   (aparece marcada como secreta) y aplica.
4. Espera al primer despliegue y abre tu bot en Telegram: `/start`.

*(Alternativa manual: **New +** → **Web Service**, runtime `Python 3`,
build `pip install -r requirements.txt`, start `python bot.py`,
health check `/health`.)*

> Si el despliegue se queja de la versión de Python (`PYTHON_VERSION` de
> `render.yaml`), cambia el valor por otra 3.12.x/3.11.x o borra esa variable
> para usar la versión por defecto de Render.

### 3. Que no se duerma (plan gratuito)

El plan gratuito **suspende el servicio tras ~15 minutos sin visitas HTTP**.
Para que el bot siga escuchando (y no se corte una traducción larga), crea un
monitor gratuito en [UptimeRobot](https://uptimerobot.com) o
[cron-job.org](https://cron-job.org) que visite:

```
https://TU-BOT.onrender.com/health
```

cada 5 minutos. Con eso el servicio permanece despierto (el plan gratuito
incluye 750 horas al mes y un servicio despierto consume ~730).

> El plan gratuito tiene 512 MB de RAM y **disco efímero**: al reiniciar o
> redeplegar se pierden la caché y el progreso. El bot lo detecta y empieza
> limpio; si reenvías el mismo archivo continúa donde iba (si el progreso
> sobrevivió) o lo vuelve a traducir.

### 4. Opción recomendada para novelas enormes: Background Worker

Un **Background Worker** de pago (*Starter*, 7 $/mes) no se duerme, no
necesita el servidor de salud y aguanta mejor los trabajos de varias horas.
En `render.yaml` está comentado el bloque exacto; resumido:

```yaml
services:
  - type: worker
    name: traductor-bot
    runtime: python
    plan: starter
    buildCommand: pip install -r requirements.txt
    startCommand: python bot.py
    envVars:
      - {key: PYTHON_VERSION, value: "3.12.7"}
      - {key: TELEGRAM_BOT_TOKEN, sync: false}
```

---

## Uso

1. Abre tu bot en Telegram y pulsa **Iniciar**.
2. Envíale un archivo (PDF, EPUB, TXT o MD).
3. El bot muestra el progreso y te devuelve el archivo traducido.

### Comandos

| Comando | Qué hace |
|---|---|
| `/start`, `/help` | Ayuda y estado actual |
| `/lang` | Teclado para elegir el idioma de destino |
| `/lang en` | Cambia el idioma directamente (cualquier código de Google: `pt`, `fr`, `ja`, `zh`, `ar`…) |
| `/status` | Cola, tareas en curso y tamaño de la caché |
| `/cancel` | Cancela la tarea en curso |

También puedes escribirle un texto normal y te lo traduce al momento.

---

## Novelas largas: qué tener en cuenta

- **Límite de Telegram**: los bots solo pueden descargar archivos de hasta
  **20 MB**. Para obras gigantes, divide el libro (por ejemplo un EPUB cada
  ~500 capítulos) y envía las partes por separado.
- **Reanudación**: el progreso se guarda por capítulos (EPUB), por páginas
  (PDF) y por párrafos (TXT). Si el proceso se reinicia o lo cancelas, al
  reenviar el mismo archivo continúa por donde iba en lugar de empezar de cero.
- **Caché**: cuando un documento termina, el resultado se guarda (7 días o
  512 MB por defecto). Si vuelves a enviar el mismo archivo, te lo devuelve
  **al instante** sin traducir nada.
- **Velocidad**: con el endpoint web gratuito una novela de 1500 capítulos
  (unas 4.000–8.000 peticiones agrupadas) tarda aproximadamente **entre 20
  minutos y 2 horas**, según el tamaño y las pausas por límite de Google.
- **Si Google bloquea el ritmo**: el bot se frena solo, espera y reintenta; no
  necesita que hagas nada. Puedes bajar `TRANSLATE_CONCURRENCY` (por ejemplo a
  3) si ves muchas pausas.
- **Volumen profesional**: si traduces muchas novelas al mes, activa el backend
  oficial (ver más abajo). Google Cloud Translation cuesta del orden de
  **20 $ por millón de caracteres** (los primeros 500.000 son gratis), así que
  una novela de 1500 capítulos sale por unos 120–160 $ **y no sufre bloqueos**.

### Backend de pago (opcional): API oficial de Google

1. En Google Cloud crea una clave de API con **Cloud Translation API**
   habilitada y facturación activa.
2. En `.env` (o en las variables de Render):

```
TRANSLATOR_BACKEND=api
GOOGLE_API_KEY=tu-clave
```

El resto no cambia: el bot usa la API oficial enviando hasta 64 párrafos por
petición y sin límites agresivos.

---

## Cómo funciona

- **Lotes de traducción**: en vez de una petición por párrafo (lo que
  multiplicaría el tráfico en una novela), los párrafos cortos se unen con
  saltos de línea y se traducen en un solo envío. Si Google devuelve un número
  distinto de líneas, se reintenta ese lote párrafo a párrafo: nunca se pierde
  texto.
- **Freno automático**: si Google responde `429` o corta la conexión, todas las
  tareas pausan unos segundos (con reintentos exponenciales) y luego continúan.
- **Motor gratuito**: endpoint público que usa el propio traductor de Google en
  el navegador (`translate.googleapis.com/translate_a/single`). No necesita API
  key ni cuenta.
- **PDF** (PyMuPDF): se extrae cada bloque de texto con su posición, tamaño,
  color y estilo; se traduce el bloque completo; se borra el texto original
  solo dentro de esa caja (respetando imágenes y gráficos) y se escribe la
  traducción en el mismo sitio, ampliando la caja hacia abajo si hay hueco y
  reduciendo la fuente por pasos si aun así no cabe.
- **EPUB** (BeautifulSoup + lxml): el libro se trata como ZIP. Los bloques de
  texto se traducen como unidad y los que contienen enlaces o imágenes se
  traducen nodo a nodo para no perder estructura. Se actualiza `dc:language` y
  el título lleva el sufijo del idioma.
- **Cola de trabajo**: las tareas se procesan en cola (1 a la vez por defecto)
  para no saturar el traductor; varios usuarios pueden enviar documentos.

## Estructura del proyecto

```
bot.py              # capa de Telegram: comandos, cola de trabajo, envío
pipeline.py         # orquesta cada tarea, caché y reanudación
translator.py       # traductor web de Google (lotes, freno, reintentos)
translator_api.py   # backend opcional con la API oficial de Google Cloud
pdf_translate.py    # traducción de PDF conservando el diseño
epub_translate.py   # traducción de EPUB conservando estructura y estilos
checkpoints.py      # progreso persistente por documento y caché de resultados
health.py           # servidor HTTP de salud para Render (/health, /status)
languages.py        # catálogo de idiomas
users.py            # preferencias de idioma por usuario (users.json)
config.py           # variables de entorno y límites
errors.py           # excepciones compartidas
render.yaml         # despliegue en Render (Web Service gratuito o worker)
tests/selftest.py   # pruebas automáticas (simuladas y reales)
data/               # (se crea sola) trabajo, progreso y caché
```

## Configuración (`.env`)

| Variable | Por defecto | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token de @BotFather (obligatorio) |
| `DEFAULT_TARGET_LANG` | `es` | Idioma destino por defecto |
| `TRANSLATOR_BACKEND` | `web` | `web` (gratis) o `api` (Google Cloud oficial) |
| `GOOGLE_API_KEY` | — | Clave de la API oficial (solo con `api`) |
| `TRANSLATE_CONCURRENCY` | `6` | Peticiones simultáneas al traductor |
| `MAX_CONCURRENT_JOBS` | `1` | Documentos traducidos a la vez |
| `MAX_INPUT_MB` | `20` | Tamaño máximo de entrada |
| `MAX_OUTPUT_MB` | `48` | Tamaño máximo de salida |
| `DATA_DIR` | `data` | Carpeta de trabajo, progreso y caché |
| `CACHE_TTL_DAYS` | `7` | Días que se guarda un resultado ya traducido |
| `CACHE_MAX_MB` | `512` | Tamaño máximo de la caché |
| `HEALTH_PORT` | `PORT` | Puerto del servidor de salud (Render lo asigna solo) |

## Pruebas

```bash
python tests/selftest.py           # rápidas, con traductor simulado
python tests/selftest.py --real    # además, una traducción real con Google
```

Las pruebas verifican: reemplazo real del texto (sin duplicarlo), conservación
de imágenes/colores/negritas en PDF, conservación de enlaces, cursivas,
`&nbsp;`, índices y metadatos en EPUB, detección de escaneos y DRM,
cancelación, agrupación en lotes, reintentos tras bloqueos, reanudación de
EPUB/PDF/TXT a medias, la caché de resultados, el servidor de salud y la cola
de trabajo completa con un bot falso.

## Limitaciones (importante)

- **Tamaño**: los bots de Telegram no pueden descargar archivos de más de
  **20 MB**. Para libros mayores, divide el documento en partes.
- **PDF escaneado**: este bot no hace OCR. Si el PDF es una imagen escaneada
  (sin capa de texto) el bot te lo avisa.
- **EPUB con DRM**: no se puede traducir (el bot lo detecta y avisa).
- **Idiomas de destino RTL en PDF** (árabe, hebreo): el texto se inserta, pero
  la disposición de derecha a izquierda puede no ser perfecta. En EPUB funciona
  bien.
- **Límite de velocidad**: el endpoint gratuito de Google puede bloquear
  temporalmente si se hacen muchísimas peticiones seguidas; el bot se frena
  solo y reintenta, y si aun así falla te avisa para que lo intentes más tarde.
- **Disco efímero en hosting gratuito**: al reiniciar el servicio se pierden la
  caché y el progreso (no el libro original, que sigue en Telegram).

## Aviso de uso

El endpoint web de Google es público y gratuito, pero no está pensado para
grandes volúmenes: úsalo de forma razonable. Respeta los términos de servicio
de Google y los derechos de autor de los documentos que traduzcas.
