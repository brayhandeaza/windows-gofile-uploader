# Windows GoFile uploader

Uploader de originales de Webtor a GoFile para Windows 10/11 x64. Transfiere con buffer sin guardar la pelicula completa ni convertir codecs. No necesita FFmpeg para este flujo.

## Preparacion

1. Instala Python 3.11 o superior con el launcher `py` desde python.org.
2. Descarga o clona este repositorio y ejecuta `Setup-Windows.cmd`.
3. Edita `.env.local` y completa `WEBTOR_API_KEY` y `GOFILE_TOKEN`.
4. Copia tu catalogo existente a `apps/scraper/output/WebTorrent.movie.json`. El catalogo y el historial de tu proyecto anterior no estan incluidos. Para conservar el progreso, copia tambien su `webtor-gofile-mp4-state.json` a esa carpeta antes de iniciar.
5. Ejecuta `Upload-GoFile.cmd -Check` para comprobar imports y leer las pendientes sin subir archivos.

## Ejecutar

```bat
Upload-GoFile.cmd -Workers 1 -Limit 1
Upload-GoFile.cmd -Workers 2
```

`-Limit` cuenta subidas confirmadas; cero funciona continuamente. `-Once` hace una pasada sin reintentos. Ctrl+C detiene el proceso. Conserva `apps/scraper/output` para reanudar y no ejecutes estados independientes contra las mismas peliculas simultaneamente.

Deduplica por TMDB incluyendo subcarpetas y confirma tamano y MD5. El formato original se conserva, incluyendo MKV, HDR y pistas de audio; la compatibilidad de reproduccion depende del dispositivo. El estado conserva el nombre historico `webtor-gofile-mp4-state.json`.

Los lanzadores aceptan el Python incluido en `.runtime` de un paquete anterior o el entorno `.venv` creado por Setup. No se incluyen runtimes, claves, catalogos ni archivos multimedia en Git.

Los modulos bajo `apps/scraper/scripts` incluyen las dependencias locales que importa el uploader. Se mantiene esa estructura para conservar las rutas de configuracion y estado. No se incluye la aplicacion principal ni el uploader de Vidhold. Utiliza contenido para el que tengas permiso de transferencia.
