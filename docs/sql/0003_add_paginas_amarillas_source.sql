-- Agrega 'paginas_amarillas' al enum source_type (nueva fuente de busqueda).
-- Equivalente de app/db/migrations/versions/0002_add_paginas_amarillas_source.py
--
-- Nota: ALTER TYPE ... ADD VALUE no puede usarse junto con otras sentencias
-- dentro de la misma transaccion en Postgres, asi que este script va suelto
-- (sin BEGIN/COMMIT). Es seguro re-ejecutarlo (IF NOT EXISTS).

ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'paginas_amarillas';
