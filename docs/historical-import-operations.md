# Importador historico: autorizacion operativa final

Estado al 07-10-2026 (America/Santiago): solo inspeccion y archivos locales.
Este documento reemplaza la solicitud anterior. No autoriza por si mismo cambios.

## 1. Destinos y revisiones

- Cuenta/proyecto: Clima Activa; VPS Hostinger 1762778, 187.77.53.52.
- Agente existente: Dokploy Compose SaHAMnge3oCj4HNIxCrEc.
- Frontend existente: Dokploy application v8OxI5RagW3PhD2_LmzLc.
- Dominio nuevo del endpoint: https://importaciones.latinchile.cl.
- Repositorio agente: msanhueza237-creator/agente-inteligente-comercial.
  Rama propuesta: codex/historical-import-app-only, basada en f7bc8eb y con los
  archivos operativos revisados. Registrar su SHA final antes de publicar.
- Repositorio CRM: msanhueza237-creator/CRM.
  Rama: fix/historical-import-cloud, base de codigo
  0f7e6b8f755644bd56f24f16196924e98db6bdf9, mas el documento revisado
  docs/historical-import-release-workflow.md. Registrar SHA final antes de publicar.
- No merge ni push a main. No nuevos proyectos, agentes, workers o bases.

## 2. Comando exacto de Dokploy

Directorio: /etc/dokploy/compose/crm-climactiva-agente-inteligente-comercial-amxo4p/code

Campo Command, SIN anteponer docker (Dokploy lo agrega):

```sh
compose -p crm-climactiva-agente-inteligente-comercial-amxo4p -f docker-compose.import-cloud.yml up -d --no-deps --no-build --pull never app
```

Invocacion Docker efectiva dentro del wrapper de Dokploy:

```sh
env -i PATH="$PATH" HOME="$HOME" docker compose -p crm-climactiva-agente-inteligente-comercial-amxo4p -f docker-compose.import-cloud.yml up -d --no-deps --no-build --pull never app
```

Dokploy clona la rama seleccionada y genera su .env antes de este comando.
Compose Path sera ./docker-compose.import-cloud.yml; isolatedDeployment=false,
randomize=false y autoDeploy=false. No agregar dominios duplicados en la UI:
el archivo versionado contiene la regla HTTPS exacta, no PathPrefix amplio.
COMPOSE_REMOVE_ORPHANS=false en la configuracion guardada; cualquier valor true,
opcion inesperada o configuracion heredada que cambie la seleccion bloquea el paso.

El archivo contiene solamente app, sin build, depends_on, migrate ni volumenes.
HISTORICAL_IMPORT_APP_IMAGE sera el ID sha256 completo de la imagen ya construida
y probada. No se usara latest. --no-build y --pull never impiden cambiar esa imagen
durante el reemplazo. No hay down, --remove-orphans, --renew-anon-volumes o prune.

Redes externas conservadas:
- crm-climactiva-agente-inteligente-comercial-amxo4p_default: acceso interno actual.
- dokploy-network: conexion con Traefik. No recrear ni eliminar ninguna red.

### Evidencia de comportamiento

- Versiones comprobadas en VPS: Dokploy v0.29.14 y Compose v5.1.4.
- El formateador de comandos instalado se extrajo de /app/dist/server.mjs.map.
  Ejecutado de forma aislada, devuelve exactamente el Command indicado. No se
  importo ni ejecuto el servidor Dokploy. El comando custom sustituye al general
  que incluye --build --remove-orphans: no se concatena con el.
- Docker Compose config valido estructura, redes y ausencia de dependencias.
- Simulacion --dry-run: solo Recreate/Start de app. Exit 0. Los workers y db
  aparecen como huerfanos intencionales, SIN acciones de eliminacion.
- Identificadores, imagenes, inicio y estado de los contenedores no cambiaron.
- Limite: se uso entrada estandar, entorno sintetico e imagen existente de
  referencia para no escribir archivos ni crear imagenes en el VPS. No prueba
  el arranque de la imagen candidata. Tras autorizar, repetir con el .env real
  protegido, archivo definitivo e ID de imagen exacto antes de reemplazar app.

## 3. Puerta obligatoria de recuperacion

La autorizacion agrupa dos etapas: A prepara/prueba recuperacion y candidatos;
B reemplaza servicios SOLO si A termina satisfactoriamente. Si A falla, detenerse
sin sustituir app ni frontend. No ampliar el alcance para sortear un fallo.

ANTES de reemplazar cualquiera de los dos servicios:

1. Comprobar que no hay otro despliegue ni cambios concurrentes. Registrar IDs,
   imagenes, StartedAt y estado de workers/db/servicios no afectados, ademas de
   revisiones, redes, puertos y version del servicio Swarm del frontend.
2. Crear carpeta privada de recuperacion, permisos 0700 y archivos 0600, fuera
   de Git y del checkout que Dokploy elimina al clonar. Capacidad libre minima:
   maximo entre 8 GiB y el doble del tamano estimado de archivos/imagenes nuevos.
   No limpiar disco. Copiar solamente configuracion de estos dos recursos y
   routing afectado. Las configuraciones con secretos no van en logs ni Git.
3. APP: volver a comprobar SHA256SUMS del export existente y coincidencia con el
   contenedor vivo. Importar rootfs.tar a una imagen privada local, reconstruyendo
   solo metadatos de ejecucion no secretos desde container-config.json (usuario,
   directorio, comando, entrypoint, PATH). Secretos permanecen en configuracion
   privada aparte. No publicar imagenes de recuperacion en un registro.
4. APP: verificar inventario/hashes y dependencias de la imagen recuperada. Probar
   arranque HTTP en un contenedor efimero sin red externa, sin puertos publicados,
   sin volumenes productivos y con entorno sintetico, HUB_EMBEDDED_WORKER=false.
   Consultar solamente /openapi.json y /import GET dentro del contenedor, sin
   escribir datos. No ejecutar /health contra bases productivas ni ningun worker.
5. FRONTEND: obtener el ID de imagen del contenedor en servicio, no solo latest.
   Crear etiqueta privada inmutable y registrar sus hashes de index.html/assets
   y la especificacion Swarm privada. Probar nginx -t y servir el HTML/assets
   en contenedor efimero sin red externa ni puertos publicados. No autenticar
   ni llamar a Supabase desde esta prueba aislada.
6. Exportar AMBAS imagenes con docker image save a archivos privados, inspeccionar
   manifiestos y SHA256, cargarlas sin eliminar imagenes existentes y comprobar
   que mantienen sus IDs. La carga no cambia servicios en ejecucion. Repetir
   pruebas de arranque usando esos IDs recuperados. No confiar solo en etiquetas:
   el VPS tiene una limpieza automatica de imagenes no utilizadas.
7. Generar y validar un Compose privado de rollback con SOLO app: imagen de
   recuperacion, comando, usuario, entorno, redes, puertos y politica previos.
   Sin build/depends_on/volumenes; secretos separados en formato literal seguro,
   sin interpolacion accidental de dolares. Comprobar equivalencia estructurada
   con docker inspect y simular su comando con --dry-run.
8. Guardar manifest.json sin secretos: SHAs de Git, IDs de imagen, hashes de
   archivos, parametros seleccionados, resultados de pruebas, marca temporal y
   ruta de cada respaldo. Comprobar acceso a todos los artefactos. No se promete
   restauracion probada de bases ni 30 dias de retencion Hostinger.

La ausencia de Docker local en Windows no se resuelve instalando servicios alli.
Estas pruebas se haran en el VPS solo despues de la autorizacion, aisladas de
produccion. Hoy los archivos .tar de recuperacion finales NO estan verificados.

## 4. Construccion y publicacion controladas

- Construir aparte la imagen candidata app usando Dockerfile.import-cloud y el
  runtime recuperado. El contexto permite solo los cuatro archivos del parche.
  Pruebas de sesion, roles, limites, CORS y no-escritura con dependencias reales
  y servicios externos simulados. No reinstalar dependencias del runtime.
- Guardar el ID candidato inmutable; en Dokploy asociarlo al SHA revisado. No
  permitir una rama que haya avanzado desde la revision aprobada. Las variables
  vigentes se conservan; agregar solo las del importador y control del despliegue.
- Instalar el transporte Traefik acotado; TLS para POST/OPTIONS de la ruta exacta.
  Limites: 26 MiB, 6 solicitudes/minuto/IP con rafaga 3, respuesta upstream 60 s.
  Publicar 8000 solo en 127.0.0.1. No tocar DNS ni firewall global.
- Cambiar autoDeploy a false en ambos recursos y seleccionar las ramas indicadas.
  Desplegar app mediante el comando exacto, sin reconstruccion al sustituirlo.
- Probar backend antes del frontend: TLS, CORS, 401 sin sesion, 403 con perfil
  no autorizado, 200 para vista previa sintetica autorizada, rutas heredadas no
  expuestas. No guardar/importar registros. Si no hay sesion de prueba adecuada,
  no extraer cookies/tokens ni crear usuarios: detener la promocion.
- Compilar frontend con VITE_HISTORICAL_IMPORT_URL=https://importaciones.latinchile.cl
  usando Dokploy existente, verificando commit, build y artefactos resultantes.
  Hacer solo preview sintetico en navegador; verificar ausencia de localhost.

## 5. Fallos y reversion expresamente incluida

Se detiene la etapa siguiente ante cualquiera de estos casos:
- Archivo de recuperacion ausente/corrupto, imagen no recuperable, arranque de
  recuperacion fallido, falta de espacio o diferencia concurrente no explicada.
- Simulacion que incluya db, migrate, workers, borrado, pull/build inesperado,
  redes eliminadas o destino/revision distinto del aprobado.
- Error al desplegar, app reiniciandose, servicio sin estabilizarse en 120 s,
  TLS invalido, 5xx o errores CORS reproducibles en dos comprobaciones.
- Fallo de seguridad: acceso sin sesion o con rol no autorizado, ruta heredada
  publicada, archivos por sobre el limite aceptados, o cualquier secreto expuesto.
- Vista previa sintetica incorrecta, frontend que no carga/usa revision diferente,
  o navegador que intenta llamar localhost.
- Cambios inesperados en IDs/imagenes/inicio de workers, db, Supabase o Red Tecnicos.

Si ya se sustituyo un servicio y el fallo es atribuible a esta publicacion,
la misma autorizacion permite revertir UNICAMENTE nuestros cambios:

1. Impedir otro despliegue de estos dos recursos mientras se revierte. Revisar
   version/configuracion actuales contra el estado esperado de nuestra operacion.
   Si hay cambios ajenos concurrentes, NO sobrescribirlos: detener y pedir revision.
2. Si se publico frontend, recuperar SU imagen guardada (docker load si hace falta)
   y actualizar solo la imagen del servicio crm-climactiva-crm-hkjzwz con una
   etiqueta privada inmutable y --no-resolve-image. Durante esa recuperacion,
   usar update-failure-action=pause para no volver automaticamente a la imagen
   fallida si la recuperacion tambien falla. No usar rollback ciego al
   "ultimo despliegue", no reconstruir main y no borrar/recrear el servicio.
   Mantener redes, replicas, montaje y etiquetas previos; comparar especificacion.
   Esperar estabilizacion y verificar HTML/assets contra los hashes guardados.
   Solo despues, restaurar la politica de actualizacion previa guardada.

```sh
docker service update --no-resolve-image --update-failure-action pause --image "$FRONTEND_PREVIOUS_IMAGE" crm-climactiva-crm-hkjzwz
```

   FRONTEND_PREVIOUS_IMAGE se fija a la etiqueta privada verificada en etapa A;
   se comprueba que resuelve al ID anterior guardado, nunca a latest.
3. Si se sustituyo app, cargar SU imagen privada si hace falta y ejecutar:

```sh
docker compose -p crm-climactiva-agente-inteligente-comercial-amxo4p -f /RUTA_PRIVADA_VERIFICADA/app.rollback.json up -d --no-deps --no-build --pull never app
```

   RUTA_PRIVADA_VERIFICADA se determina y registra en la etapa A; nunca se ejecuta
   como marcador literal. El archivo contiene solo app y su configuracion previa.
   Restaurar los puertos anteriores revierte nuestra restriccion de 8000, no
   constituye una nueva apertura de firewall. No ejecutar el Compose general.
4. Revertir solo variables, rama, comando, composePath y routing cambiados por
   nosotros, a partir del respaldo. El archivo de transporte nuevo solo se retira
   si sigue coincidiendo con el hash que instalamos; no tocar certificados ni
   archivos compartidos. Restaurar preferencias autoDeploy anteriores SOLO al
   final y si main no avanzo; si avanzo, mantener false y pedir revision para
   evitar una publicacion nueva no autorizada.
5. Confirmar imagenes/hashes del frontend, estado app y preservacion de los
   workers/db. Si la reversion falla, detener: no restaurar todo el VPS, no
   intervenir bases y no reiniciar/eliminar otros servicios.

Frontend recuperable observado: sha256:e3711f591560ace9370476ab7fc7740f7d1f702c798ac87c440e32209d55e93b.
Revalidar el ID al inicio de la etapa A. La reversion recupera el comportamiento
anterior, incluida la antigua dependencia del importador respecto de localhost.

## 6. Como seguir publicando Cloud -> GitHub

No hay despliegue automatico por push durante esta transicion. Esto no agrega
dependencia de Windows: Cloud prepara codigo y GitHub conserva el historial;
las compilaciones y ejecucion de servicios permanecen en el VPS.

1. Cloud parte de la rama de publicacion ACTUAL de cada repositorio, no de una
   copia local ni del main atrasado. Crea rama de trabajo y PR hacia:
   - CRM: fix/historical-import-cloud.
   - Agente: codex/historical-import-app-only.
2. Revisar diff, pruebas y alcance. El cambio del agente no puede modificar el
   Compose general, workers ni migraciones como efecto secundario del importador.
   Mantener archivos de despliegue y procedimiento en el mismo PR.
3. Tras aprobar el PR, incorporar a su rama de publicacion y registrar el SHA.
   Un push NO despliega y la fusion del PR NO es autorizacion operativa.
4. Solicitar/promover una revision concreta en los recursos Dokploy EXISTENTES.
   El operador autorizado puede hacerlo desde Cloud con acceso seguro o desde
   un navegador remoto; nunca compartir contrasenas por HTTP:3000. Si Cloud no
   tiene acceso al VPS/Dokploy, entregar SHA/PR para promocion, no simular exito.
5. Agente: construir y probar imagen aparte en VPS a partir del SHA, confirmar
   base preservada, exportar imagen privada, asignar ID a Dokploy y ejecutar el
   comando app-only. Frontend: build/publicacion del SHA revisado, con URL HTTPS.
   Si cambian archivos fuera de los cuatro permitidos por el Dockerfile overlay,
   revisar/ampliar explicitamente el empaquetado: no afirmar que se publicaron.
6. Repetir respaldo, ausencia de concurrencia, puertas de verificacion y rollback
   para cada publicacion. Registrar SHA, imagen, resultado y enlace del despliegue.

No crear ahora workflows, webhooks o tokens de despliegue automatico. Se puede
volver a main/autodeploy despues mediante PRs de consolidacion que incorporen
todas las correcciones, revisen los overlays especiales de workers y mantengan
el despliegue acotado. Requiere una autorizacion separada; no volver a apuntar a
main mientras carezca de estos cambios. Mantener main y ramas de publicacion
sin divergencia funcional mediante PRs revisados, nunca force-push ni copias a mano.

## 7. Limites de la autorizacion final

Incluye etapas A/B, publicacion de archivos revisados solo en ramas no-main,
ajustes acotados en los dos recursos Dokploy y reversion condicional descrita.
Puede haber interrupcion breve de app y cambio controlado del frontend.
Excluye bases, migraciones, restauraciones de datos/VPS, workers, cambios de
claves, DNS/firewall global, limpieza Docker, otros sitios, S3/B2 y compras.
Las pruebas reales usan solo datos sinteticos y nunca pulsan Guardar.
La automatizacion contable Facto/Chrome sigue dependiendo del PC: no se modifica.

Referencias: https://docs.dokploy.com/docs/core/docker-compose y
https://docs.docker.com/reference/cli/docker/compose/ (modo --dry-run).
