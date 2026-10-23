# 🌍 Bot de Telegram para traducir documentos

Bot de Telegram que recibe **PDF, EPUB, TXT o MD**, los traduce con
**tres traductores web gratuitos (Google, Microsoft Edge/Bing y DeepL, sin API
key)** y los devuelve traducidos. También traduce mensajes de texto sueltos.
Cada usuario elige su **motor de traducción** (automático, solo Google, solo
Microsoft o solo DeepL) con el botón **🧠 Motor** o el comando `/motor`.

- **PDF** → conserva el diseño original: imágenes, tablas, colores, negritas y
  la posición del texto. La traducción se superpone exactamente donde estaba
  el texto original.
- **EPUB** → conserva capítulos, estilos CSS, imágenes, enlaces, orden de
  lectura y los índices (NCX y navegación). El resto de archivos se copia byte
  a byte.
- **TXT / MD** → texto plano traducido línea a línea.
- **✂️ /dividir** → parte una novela larga en secciones **sin traducir** (EPUB
  por capítulos, TXT/MD por sus encabezados, PDF por índice o páginas) para
  traducirlas por separado y reenviar solo la que falle.
- **🔀 /ambos** → divide la novela y traduce cada sección por separado: primero
  recibes todas las secciones sin tocar y después cada una traducida. El tamaño
  de las secciones se elige con botones (**25 / 50 / 75 / 100 capítulos**), nada
  automático.
- **🧠 Motor** → elige quién traduce: **Automático** (Google primero y, si se
  bloquea, sigue Microsoft o DeepL), **solo Google**, **solo Microsoft** o
  **solo DeepL**. También al vuelo desde la pregunta de cada archivo.
- **Menú de botones fijo** → un teclado siempre visible (🌐 Traducir · ✂️
  Dividir · 🔀 Dividir y traducir · ⚙️ Idioma · 🧠 Motor · 📊 Estado · 🚫
  Cancelar · ❓ Ayuda). Si envías un archivo sin haber elegido nada, el bot te
  pregunta qué hacer con botones en vez de ponerse a traducir solo.
- **Cola de varios volúmenes** → envía varios archivos seguidos; se procesan en
  orden, de uno en uno. Límite por defecto: 10 archivos / 100 MB.
- **☁️ Supabase opcional** → guarda el original, la cola, los checkpoints y los
  resultados. Si Render se reinicia, el bot reanuda solo y no hay que reenviar
  nada. Sin Supabase sigue funcionando con el disco local.

Pensado también para **novelas web de gran volumen (+1000 capítulos)**: traduce
por lotes (el motor de Google nuevo admite **listas enteras de párrafos en una
sola petición**), **si Google limita la velocidad cambia solo a Microsoft o
DeepL** (cuotas independientes) y el progreso muestra **con qué motor está
traduciendo en ese momento**, muestra el avance también **dentro de cada
capítulo**, guarda el progreso capítulo a capítulo, **reanuda** donde se quedó y
puede **dividir la novela en secciones** para que un fallo solo cueste reenviar
una parte.

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

## Acceso privado (solo tú y quien autorices)

El bot usa una **lista blanca de IDs numéricos de Telegram**:

1. Habla con [@userinfobot](https://t.me/userinfobot) y copia tu ID (por
   ejemplo `123456789`).
2. Define la variable `AUTHORIZED_USER_IDS` con tu ID y los de las personas
   autorizadas, separados por comas:
   ```env
   AUTHORIZED_USER_IDS=123456789,987654321
   ```
3. Reinicia el bot o vuelve a desplegarlo en Render.

Con la variable definida, cualquier otro usuario recibe un aviso de bot privado
y **no puede ejecutar comandos, botones ni enviar documentos**. El aviso incluye
su propio ID por si quieres autorizarlo después. Los intentos denegados quedan
en los logs.

Si `AUTHORIZED_USER_IDS` está vacía, el bot queda abierto a cualquiera (es el
comportamiento anterior y sirve para probar en local). En producción no la
dejes vacía. La lista blanca filtra **usuarios**, no chats: si añades el bot a
un grupo, un usuario autorizado podrá usarlo allí y los archivos se enviarán a
ese grupo. Para uso personal, úsalo en un chat privado.

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
> redeplegar se pierden la cola, la caché y el progreso. Para que los trabajos
> largos sobrevivan, configura **Supabase** (sección siguiente). Sin Supabase,
> el bot empieza limpio y hay que reenviar el archivo.

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

## Supabase: que los trabajos sobrevivan a reinicios (recomendado)

Render (plan gratuito) usa **disco efímero**: al reiniciar o redeployar se
pierden la cola, el original y el progreso. El bot puede guardarlos en
**Supabase** (Postgres + Storage) y recuperarlos solo al arrancar. Así puedes
enviar varios volúmenes, cerrar Telegram y no volver a enviar nada aunque el
servidor se reinicie.

Supabase no sustituye a Render: Render sigue ejecutando Python y traduciendo;
Supabase solo guarda el estado y los archivos. El plan gratuito de Supabase
incluye **1 GB de Storage**, más que suficiente para la cola máxima de 100 MB.

### Configurarlo (5 minutos)

1. Crea un proyecto gratis en [supabase.com](https://supabase.com).
2. Entra en **SQL Editor → New query**, pega el contenido de
   **`supabase_schema.sql`** y pulsa **Run**. Crea las tablas `bot_jobs` (cola
   y estado) y `bot_users` (idioma y motor).
3. Ve a **Project Settings → API** y copia:
   - **Project URL** (algo como `https://xxxx.supabase.co`).
   - **service_role** (en «Project API keys»; **no** uses la `anon`).
4. En Render → tu servicio → **Environment**, añade:
   ```
   SUPABASE_URL=https://xxxx.supabase.co
   SUPABASE_SERVICE_KEY=eyJhbGciOi...   # service_role
   SUPABASE_BUCKET=traductor
   ```
5. Guarda y deja que Render redepliegue. El bot crea el bucket privado
   `traductor` si no existe (también puedes crearlo a mano en **Storage**).

> ⚠️ **Seguridad:** la clave `service_role` se salta RLS. Úsala solo en las
> variables de entorno de Render, nunca en el repositorio, en el móvil ni en
> una web pública. Las tablas tienen RLS activado y no tienen políticas: sin la
> service_role no se puede leer nada.

### Qué se guarda y cuándo

- **Original** → se sube a Storage al empezar cada tarea. Si el bot se reinicia
  a mitad, lo descarga de ahí en vez de pedírtelo otra vez.
- **Cola y estado** → `bot_jobs`: archivo, usuario, idioma, motor, modo,
  progreso, mensaje de estado y resultado.
- **Checkpoints** → PDF parcial, fragmentos EPUB y chunks de texto. Se suben
  después de cada avance; al reanudar se descargan y la traducción sigue donde
  iba.
- **Resultado** → se guarda en `cache/` para reenviarlo al instante. Se limpia
  con la misma antigüedad/tamaño que la caché local (`CACHE_TTL_DAYS` y
  `CACHE_MAX_MB`). El original se borra de Supabase cuando la tarea se envía.
- **Preferencias** → `bot_users` (idioma y motor), con copia local en
  `users.json`.

Al arrancar, el bot busca tareas `queued`, `running`, `ready`… y las vuelve a
poner en la cola **en el mismo orden**. Si el mensaje de estado original se
borró, crea uno nuevo.

### Si Supabase falla o no está configurado

El bot **no se rompe**: si faltan `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`, si la
tabla no existe o si Supabase está pausado, sigue trabajando con el disco local
como siempre. La diferencia es que un reinicio de Render ya no se recupera
solo. Los avisos aparecen en los logs de Render.

> Los proyectos gratuitos de Supabase se pausan tras una semana sin actividad.
> Si eso pasa, el bot cae al modo local; entra en el panel de Supabase para
> reactivarlo.

---

## Uso

1. Abre tu bot en Telegram y pulsa **Iniciar**: verás el **menú de botones**
   fijo bajo el campo de texto.
2. Toca **🌐 Traducir** (u otra opción) y envíale **uno o varios archivos**
   seguidos; o envía los archivos directamente y el bot te preguntará qué hacer
   con cada uno:
   *«🌐 Traducir · ✂️ Dividir · 🔀 Dividir y traducir cada parte · 🗑️ Descartar»*.
   Si eliges 🔀, primero te pide **cuántos capítulos por sección** (25/50/75/100)
   con botones.
3. El bot los procesa **de uno en uno, en orden**, muestra el progreso de cada
   uno y te devuelve los resultados. Puedes seguir enviando archivos mientras
   trabaja: se añaden a la cola.

El modo que elijas (Traducir, Dividir o Dividir y traducir) **sigue activo para
todos los archivos que envíes** hasta que pulses **🚫 Desactivar modo**. Si no
hay modo activo, el bot solo te pregunta y no hace nada hasta que elijas.
También puedes elegir la acción desde el pie de foto del propio archivo
(`/traducir`, `/dividir 25`, `/ambos`).

### Cola de varios volúmenes

Puedes enviar los volúmenes de una novela uno detrás de otro sin esperar a que
termine el anterior. El bot mantiene una cola **por orden de llegada** y
procesa un documento a la vez (`MAX_CONCURRENT_JOBS=1`), que es lo más seguro
para los traductores gratuitos.

- Límites por defecto: **10 archivos** y **100 MB** en total
  (`QUEUE_MAX_FILES`, `QUEUE_MAX_MB`). Cada archivo sigue limitado a 20 MB por
  Telegram.
- `/status` muestra la cola y la tarea en curso.
- Si un paso tarda (por ejemplo, Google está limitando o se está subiendo un checkpoint), el mensaje muestra «sigo trabajando» cada ~45 s. No hace falta pulsar `/status` para desbloquearlo: el proceso sigue solo.
- `/cancel` cancela **la tarea activa y todas las que tengas en cola**.
- Si envías varios archivos sin modo, cada uno recibe su propia pregunta y
  puedes responderlas en cualquier orden.
- Con Supabase configurado, la cola y el progreso se recuperan solos tras un
  reinicio o redeploy.

### Comandos

| Comando | Qué hace |
|---|---|
| `/start`, `/help` | Ayuda y estado actual |
| `/lang` | Teclado para elegir el idioma de destino |
| `/lang en` | Cambia el idioma directamente (cualquier código de Google: `pt`, `fr`, `ja`, `zh`, `ar`…) |
| `/motor` | Teclado para elegir el motor de traducción (automático, Google, Microsoft o DeepL) |
| `/motor google` | Fija el motor: `auto`, `google`, `microsoft` o `deepl` |
| `/traducir` | Los archivos que envíes se traducen tal cual (modo persistente) |
| `/dividir` | Los archivos que envíes se parten en secciones sin traducir |
| `/dividir 25` | …en secciones de 25 capítulos (en PDF, 25 páginas) |
| `/dividir 8 partes` | …en 8 secciones iguales |
| `/ambos` | Los archivos que envíes se dividen y se traduce cada sección (te pide el tamaño con botones) |
| `/ambos 25` | …con secciones de 25 capítulos, sin preguntar |
| `/status` | Cola, tareas en curso, motor en uso, Supabase y tamaño de la caché (con botón 🔄) |
| `/cancel` | Cancela la tarea activa y las que estén en cola |

También puedes escribirle un texto normal y te lo traduce al momento.

### ✂️ Dividir novelas en secciones (`/dividir`)

Para novelas enormes es más cómodo trabajar por secciones. Envía `/dividir` y
después el archivo (o directamente el archivo con la leyenda «`/dividir 25`» en
el pie de foto) y el bot te devuelve el libro **sin traducir** troceado:

- **EPUB** → un EPUB válido por sección, con su índice (NCX/navegación)
  recortado, estilos e imágenes intactos y el título marcado como
  «Libro — Parte 3 de 8». Los capítulos se detectan por el *spine* del libro.
- **TXT / MD** → corta por los encabezados («Capítulo 12», «Chapter 12»,
  «第12章», «12화»…). Los índices al principio del archivo se ignoran.
- **PDF** → usa los marcadores/índice del PDF si los tiene; si no, corta por
  páginas.

Los archivos de salida se llaman «Libro - Parte 03 de 08.epub»: el nombre solo
indica la parte. El rango de capítulos queda en el pie de foto del mensaje,
porque la numeración detectada no siempre coincide con la real del libro.

Después envía cada sección como un archivo normal: se traduce por separado, se
guarda en la caché por separado y, si algo falla o el bot se reinicia, **con
Supabase se reanuda automáticamente** y sin Supabase solo reenvías esa sección
en lugar de la novela completa.

### 🔀 Dividir y traducir (`/ambos`)

Si prefieres que el bot haga las dos cosas, toca **🔀 Dividir y traducir** (o
envía el archivo con la leyenda «`/ambos`»). El bot te pide **cuántos capítulos
por sección** con botones (25 / 50 / 75 / 100; en PDF se cuentan páginas) y
después:

1. Te manda **todas las secciones sin traducir** (guárdalas de reserva).
2. Traduce **cada sección por separado** y te la envía terminada, con su
   progreso («parte 3 de 8 · capítulo 12/50») en el mismo mensaje.

Cada sección se guarda en la caché con su propia clave y la división es
reproducible byte a byte: si una falla, reenvías el archivo original y solo se
vuelve a traducir lo que faltaba (las partes ya hechas se recuperan al instante
de la caché).

### 🧠 Elegir el motor de traducción (`/motor`)

Toca **🧠 Motor** (o escribe `/motor`) para elegir quién traduce. La elección
es **por usuario** y se guarda en `users.json`, así que se aplica a tus
próximas traducciones:

- **🔄 Automático** (por defecto) — Google primero; si se bloquea (varios `429`
  seguidos), el bot continúa con **Microsoft** (y con **DeepL** cuando el idioma
  lo permite) para no dejar el libro a medias, y vuelve a probar Google más
  tarde.
- **🔵 Solo Google** — el más rápido y estable: usa el **método nuevo** de la
  web de Google (`translate-pa`), que aguanta IPs de datacenter y traduce
  **listas enteras de párrafos en una sola petición**.
- **🟣 Solo Microsoft** — el traductor de Edge/Bing, sin cuotas compartidas con
  Google.
- **🟢 Solo DeepL** — la **mejor calidad** de traducción; va más despacio (una
  petición a la vez, como su web) y solo aparece para los idiomas que DeepL
  soporta (no se ofrece para latín, catalán, gallego ni euskera).

También puedes cambiarlo justo antes de traducir: en la pregunta de cada
archivo, el botón **🧠 Motor: …** abre el mismo selector y, al elegir, vuelve a
la pregunta con el motor nuevo. El motor elegido aparece en el mensaje del
archivo (`🧠 DeepL`), en el progreso mientras traduce (`⚙️ Google`) y en
`/status` («🧠 Tu motor»). Las tareas ya en curso no cambian; el ajuste afecta a
las siguientes.

---

## Novelas largas: qué tener en cuenta

- **Límite de Telegram**: los bots solo pueden descargar archivos de hasta
  **20 MB**. Para obras gigantes usa **`/dividir`**: trocea el libro en
  secciones (por ejemplo de 50 capítulos) y envía las partes por separado; así
  ninguna se acerca al límite y, si algo falla, solo reenvías una parte.
- **Reanudación**: el progreso se guarda por capítulos (EPUB), por páginas
  (PDF) y por párrafos (TXT). Con Supabase, al reiniciar el bot recupera el
  original y los checkpoints y continúa **sin que reenvíes nada**. Sin Supabase,
  al reenviar el mismo archivo continúa por donde iba.
- **Caché**: cuando un documento termina, el resultado se guarda (7 días o
  512 MB por defecto; también en Supabase si está activo). Si vuelves a enviar
  el mismo archivo, te lo devuelve **al instante** sin traducir nada.
- **Velocidad**: con los endpoints web gratuitos una novela de 1500 capítulos
  (unas 4.000–8.000 peticiones agrupadas) tarda aproximadamente **entre 20
  minutos y 2 horas**, según el tamaño y las pausas por límite de los
  proveedores.
- **Si Google bloquea el ritmo (`HTTP 429`)**: no tienes que hacer nada; el bot
  espacia las peticiones, cambia de endpoint, respeta la espera que pida Google
  y, si aun así insiste, **pasa automáticamente al traductor de Microsoft** (o
  a DeepL) para que el libro siga avanzando; pasado un rato vuelve a intentar
  Google. El método nuevo de Google (`translate-pa`, el de su web actual) es
  mucho más resistente desde datacenter que los endpoints clásicos, así que
  normalmente ni verás el aviso. Desde un servidor (Render, VPS…) los límites
  siguen siendo **más estrictos** que desde casa: si ves el aviso a menudo sube
  `WEB_MIN_INTERVAL` a `1`-`1.5` y baja `TRANSLATE_CONCURRENCY` a `2`-`3`, o
  prueba **🧠 Motor → Solo DeepL** para ese libro concreto.
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
  multiplicaría el tráfico en una novela), los párrafos cortos se agrupan y se
  traducen en un solo envío. Google lo hace de forma **nativa** (su método
  nuevo acepta la lista completa de textos en una petición; con 100 párrafos
  de golpe, sin problema) y DeepL también acepta listas. Si un motor devuelve
  un número distinto de líneas, se reintenta ese lote párrafo a párrafo: nunca
  se pierde texto.
- **Freno y ritmo adaptativos**: las peticiones se espacian (por defecto 0,6 s
  entre cada una). Si un proveedor responde `429` o corta la conexión, el bot
  baja el ritmo, espera (incluida la cabecera `Retry-After`), rota al siguiente
  endpoint equivalente y reintenta con espera exponencial hasta 8 veces.
- **Tres motores gratuitos con relevo automático** (modo `auto`, el de por
  defecto):
  - **Google (método nuevo)**: los mismos endpoints que usa la web actual de
    Google Translate y el plugin de Calibre «Ebook Translator»
    (`translate-pa.googleapis.com/v1/translate` para textos sueltos y
    `/v1/translateHtml` para lotes enteros). Si el método nuevo falla, el bot
    vuelve temporalmente a los endpoints clásicos (`client=gtx` sobre
    `translate.googleapis.com`, `clients5` y `translate.google.com`; el último
    recurso usa `client=dict-chrome-ex`) mientras se recupera.
  - **Microsoft** (Edge/Bing): `edge.microsoft.com/translate/translatetext`,
    el mismo que usa el traductor de Microsoft en el navegador.
    No comparte cuota con Google: cuando Google encadena `429` (típico desde
    IP de datacenter como la de Render) el bot le da el turno en segundos,
    sigue traduciendo con Microsoft y vuelve a probar Google más tarde.
  - **DeepL** gratuito: `oneshot-free.www.deepl.com/v1/storefront/translate`,
    el traductor de su web (sin API key). Es el de **más calidad**, pero solo
    se ofrece para los idiomas que DeepL soporta y va con pausa (una petición a
    la vez). Si DeepL «devuelve el texto sin traducir» (su forma de limitar),
    el bot lo detecta y el relevo continúa.
  - Ninguno necesita API key ni cuenta. Si quieres fijar uno solo, usa el
    botón **🧠 Motor** (por usuario) o `TRANSLATOR_BACKEND=google` /
    `microsoft` / `deepl` (por defecto del servidor).
- **PDF** (PyMuPDF): se extrae cada bloque de texto con su posición, tamaño,
  color y estilo; se traduce el bloque completo; se borra el texto original
  solo dentro de esa caja (respetando imágenes y gráficos) y se escribe la
  traducción en el mismo sitio, ampliando la caja hacia abajo si hay hueco y
  reduciendo la fuente por pasos si aun así no cabe.
- **EPUB** (BeautifulSoup + lxml): el libro se trata como ZIP. Los bloques de
  texto se traducen como unidad y los que contienen enlaces o imágenes se
  traducen nodo a nodo para no perder estructura. Se actualiza `dc:language` y
  el título lleva el sufijo del idioma. El progreso avanza **documento a
  documento y fragmento a fragmento**, así que la barra se mueve aunque un
  capítulo tarde varios minutos.
- **Divisor de novelas** (`splitter.py`): detecta los capítulos (spine del
  EPUB, encabezados del TXT/MD, índice o páginas del PDF) y genera secciones
  independientes: en el EPUB se recorta el índice NCX/navegación, se ajustan
  las referencias y los capítulos que no van en la sección se eliminan del
  paquete (los estilos, imágenes y demás recursos se copian intactos).
- **Cola de trabajo**: puedes enviar varios archivos seguidos; se procesan en
  orden, uno a la vez por defecto (`MAX_CONCURRENT_JOBS=1`) para no saturar el
  traductor. Si Supabase está configurado, la cola sobrevive a los reinicios.
- **Persistencia opcional en Supabase**: original, cola, checkpoints y caché en
  Postgres/Storage. Si no hay credenciales o Supabase falla, todo sigue
  funcionando en local.

## Estructura del proyecto

```
bot.py              # capa de Telegram: menú de botones, comandos, cola y envío
pipeline.py         # orquesta cada tarea, caché y reanudación
translator.py       # traductores web gratuitos (Google nuevo, Microsoft, DeepL) + relevo
translator_api.py   # backend opcional con la API oficial de Google Cloud
pdf_translate.py    # traducción de PDF conservando el diseño
epub_translate.py   # traducción de EPUB conservando estructura y estilos
splitter.py         # detector de capítulos y divisor de novelas en secciones
checkpoints.py      # progreso persistente, caché y espejo en Supabase
supabase_store.py   # cliente REST de Supabase (Postgres + Storage)
supabase_schema.sql # tablas que hay que crear en el SQL Editor de Supabase
health.py           # servidor HTTP de salud para Render (/health, /status)
languages.py        # catálogo de idiomas
users.py            # preferencias por usuario (local + Supabase)
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
| `AUTHORIZED_USER_IDS` | vacío | IDs numéricos autorizados, separados por comas. Vacío = abierto a cualquiera |
| `DEFAULT_TARGET_LANG` | `es` | Idioma destino por defecto |
| `TRANSLATOR_BACKEND` | `auto` | Motor por defecto del servidor: `auto` (Google + relevo Microsoft/DeepL), `google`/`web`, `microsoft`/`ms`, `deepl` o `api` (Google Cloud oficial). Cada usuario puede cambiarlo con 🧠 Motor |
| `GOOGLE_API_KEY` | — | Clave de la API oficial (solo con `api`) |
| `TRANSLATE_CONCURRENCY` | `6` | Peticiones simultáneas al traductor |
| `WEB_MIN_INTERVAL` | `0.6` | Segundos mínimos entre peticiones web (desde Render usa `1`-`1.5`) |
| `WEB_MAX_INTERVAL` | `8` | Ritmo más lento al que se frena si un proveedor limita (`429`) |
| `MAX_CONCURRENT_JOBS` | `1` | Documentos traducidos a la vez |
| `QUEUE_MAX_FILES` | `10` | Archivos que puedes tener en cola a la vez |
| `QUEUE_MAX_MB` | `100` | Tamaño total máximo de la cola |
| `PENDING_MAX_FILES` | `20` | Preguntas «¿qué hago con este archivo?» pendientes |
| `MAX_INPUT_MB` | `20` | Tamaño máximo de entrada |
| `MAX_OUTPUT_MB` | `48` | Tamaño máximo de salida |
| `SUPABASE_URL` | — | URL del proyecto Supabase (opcional; activa la persistencia) |
| `SUPABASE_SERVICE_KEY` | — | Clave `service_role` de Supabase (solo servidor) |
| `SUPABASE_BUCKET` | `traductor` | Bucket privado de Storage (se crea solo) |
| `SUPABASE_TIMEOUT` | `12` | Espera máxima de las peticiones a Supabase (segundos) |
| `SUPABASE_JOBS_TABLE` | `bot_jobs` | Tabla de cola/estado |
| `SUPABASE_USERS_TABLE` | `bot_users` | Tabla de preferencias |
| `SPLIT_CHAPTERS_PER_PART` | `50` | Capítulos por sección en `/dividir` (TXT/MD/EPUB) |
| `SPLIT_PAGES_PER_PART` | `100` | Páginas por sección en PDF sin índice |
| `SPLIT_MAX_PARTS` | `100` | Máximo de secciones que genera `/dividir` |
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
cancelación, agrupación en lotes (incluido el **método nuevo de Google** con su
lote nativo y el respaldo clásico), reintentos tras bloqueos, el relevo
automático Google → Microsoft, **DeepL gratuito** (códigos de idioma y guarda
anti-eco), la **selección de motor** (`auto`/`google`/`microsoft`/`deepl`), las
preferencias por usuario, reanudación de EPUB/PDF/TXT a medias, la caché de
resultados, el divisor de novelas (capítulos, secciones válidas, sin pérdidas y
reproducibles byte a byte), el menú fijo y los teclados de acción (incluido el
selector de motor), el servidor de salud, la cola de varios archivos (orden,
límites y cancelación), la persistencia remota simulada (checkpoints, caché y
tareas) y la cola de trabajo completa (traducir, dividir y
dividir-y-traducir) con un bot falso.

## Limitaciones (importante)

- **Tamaño**: los bots de Telegram no pueden descargar archivos de más de
  **20 MB**. Para libros mayores usa **`/dividir`** y envía las secciones.
- **PDF escaneado**: este bot no hace OCR. Si el PDF es una imagen escaneada
  (sin capa de texto) el bot te lo avisa.
- **EPUB con DRM**: no se puede traducir (el bot lo detecta y avisa).
- **Idiomas de destino RTL en PDF** (árabe, hebreo): el texto se inserta, pero
  la disposición de derecha a izquierda puede no ser perfecta. En EPUB funciona
  bien.
- **Límite de velocidad**: los endpoints gratuitos pueden bloquear
  temporalmente si se hacen muchísimas peticiones seguidas; el bot se frena
  solo, cambia de endpoint y, si Google insiste, pasa al traductor de Microsoft
  (o a DeepL) para no dejar el libro a medias. Si aun así falla te avisa para
  que lo intentes más tarde — o cambia de motor con 🧠 **Motor**.
- **Disco efímero en hosting gratuito**: al reiniciar el servicio se pierden la
  cola, la caché y el progreso locales. Configura **Supabase** para que el
  original, la cola y los checkpoints sobrevivan; sin él tendrás que reenviar
  los archivos pendientes.

## Aviso de uso

Los endpoints web de Google, Microsoft y DeepL son públicos y gratuitos, pero
no están pensados para grandes volúmenes: úsalos de forma razonable. Respeta
los términos de servicio de cada proveedor y los derechos de autor de los
documentos que traduzcas.
