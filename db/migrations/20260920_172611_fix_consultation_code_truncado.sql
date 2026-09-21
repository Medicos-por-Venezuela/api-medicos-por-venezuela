-- Migración: fix_consultation_code_truncado
-- Creada:    2026-09-20 17:26:11
--
-- Debe ser idempotente (IF NOT EXISTS / ON CONFLICT). El runner la envuelve en
-- una transacción; no uses CREATE INDEX CONCURRENTLY (no corre en transacción).
--
-- BUG LATENTE QUE CORRIGE: el trigger usaba `lpad(nextval(...), 4, '0')`, y `lpad`
-- TRUNCA cuando el texto es más largo que el ancho pedido: con la secuencia en
-- 10076, el código salía "CONS-20260920-1007" y la siguiente consulta generaba el
-- mismo código -> violación de `consultations_code_key`. A partir de 10.000
-- consultas el generador colisiona (en producción ya hay 858+). El arreglo
-- conserva el relleno a 4 dígitos por debajo de 10.000 y deja crecer el número
-- sin truncarlo por encima.
create or replace function public.generate_consultation_code()
returns trigger
language plpgsql
as $function$
DECLARE
  seq bigint;
BEGIN
  seq := nextval('consultation_seq');
  NEW.code := 'CONS-' ||
              TO_CHAR(NOW(), 'YYYYMMDD') || '-' ||
              LPAD(CAST(seq AS TEXT), GREATEST(4, LENGTH(CAST(seq AS TEXT))), '0');
  RETURN NEW;
END;
$function$;
