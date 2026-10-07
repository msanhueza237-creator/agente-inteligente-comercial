# Vista previa histórica autenticada

`POST /api/historical-imports/preview` reutiliza el normalizador existente sin
guardar datos. Ahora requiere `Authorization: Bearer <sesión del usuario CRM>`.

Variables del servicio web, vacías por defecto:

- `IMPORT_SUPABASE_URL`: origen HTTPS del Supabase del CRM, sin ruta.
- `IMPORT_SUPABASE_ANON_KEY`: clave pública anon; nunca una clave de servicio.
- `IMPORT_ALLOWED_ORIGINS`: orígenes HTTPS del frontend separados por comas, sin
  slash final ni comodines.

Se comprueba la sesión en `/auth/v1/user` y el perfil `role,active` mediante
PostgREST usando el mismo token. Solo pueden importar administradores y vendedores
activos. Los metadatos de usuario no conceden permisos. Las consultas son GET;
la vista previa no utiliza la base del agente ni los RPC de guardado del CRM.

La autorización ocurre antes del procesamiento multipart. Se limita el archivo
a 25 MiB y el cuerpo multipart a 25 MiB más 64 KiB; se acepta un solo archivo.
La fecha, extensión y cantidad de empresas conservan sus validaciones. La
normalización CPU se ejecuta en el thread pool. No se siguen redirecciones de
Supabase y no se muestran tokens ni errores internos de sus respuestas.

El proxy público debe ofrecer HTTPS y **solo** la ruta de vista previa hacia
`app:8000`, con controles de tamaño, tiempo y tasa. No exponer las rutas heredadas
de importación o monitor sin protección. No crear otro worker ni iniciar el Hub
embebido para habilitar esta función.

Coordinar con `CRM/docs/historical-import-cloud.md`: desplegar el backend
verificado/configurado antes del frontend. Conservar overlays Facto/prospección
de la imagen en uso; no reconstruir ciegamente desde una base anterior. No hay
migraciones, cambios de claves ni nuevos servicios incluidos en esta corrección.

Pruebas: `python -m pytest -q tests/test_historical_import_api.py
tests/test_historical_import.py`. En Cloud se usa `pytest-socket` para bloquear
conexiones externas. Las nuevas variables ausentes deshabilitan la vista previa
remota con HTTP 503 y no cambian la configuración de los workers existentes.
