# Caddy, la IP real del cliente y el puerto 8000

## Qué problema resuelve

`client_ip(request)` (la columna `ip` del `audit_log`, p. ej. en `READ_CLINICAL_DATA`) y la clave
del rate limit (`get_remote_address` de slowapi) leen `request.client.host`. En producción la API va
detrás de Caddy, así que eso era **la IP de Caddy**: toda la auditoría apuntaba a la misma IP, y
todos los clientes compartían un solo cupo de rate limit.

uvicorn ya sabe leer `X-Forwarded-For` / `X-Forwarded-Proto`, pero solo si la conexión viene de una
IP de `FORWARDED_ALLOW_IPS`, que por defecto es `127.0.0.1`. Caddy no llega al contenedor desde
`127.0.0.1`, así que las cabeceras se ignoraban.

`FORWARDED_ALLOW_IPS=*` no vale: el 8000 estaba publicado en `0.0.0.0`, y quien llegara directo al
puerto podría poner la IP que quisiera en el `audit_log` y esquivar el rate limit rotando la
cabecera.

## Cómo queda (docker-compose.prod.yml)

```
cliente ──https──▶ Caddy (host EC2, :443) ──http──▶ 127.0.0.1:8000 ──▶ mpv-api (red `api`, 172.30.0.0/24)
                                                     docker-proxy / NAT
                                                     origen visto por uvicorn: 172.30.0.1 (gateway)
```

- **Caddy corre en el host**, no en un contenedor: ningún compose del repo lo levanta, y el
  health público responde con `Via: 1.1 Caddy`. Su config (`/etc/caddy/Caddyfile`) no está en el
  repo.
- **El puerto se publica solo en loopback** (`127.0.0.1:8000:8000`). Desde fuera del EC2 no se
  llega al 8000, aunque se abra el Security Group.
- **Red con subnet fija** `172.30.0.0/24`, gateway `172.30.0.1`. Una conexión del host a
  `127.0.0.1:8000` entra al contenedor con origen en el gateway, tanto por `docker-proxy`
  (userland-proxy, el default) como por NAT (`"userland-proxy": false`).
- **`FORWARDED_ALLOW_IPS: "172.30.0.1"`** en `environment:` del compose. Va ahí y no en
  `.env.production` porque depende de la subnet del mismo archivo, y `environment` tiene prioridad
  sobre `env_file`: un `*` puesto por error en el `.env` no la pisa.
- **Cuál es la IP del cliente:** uvicorn recorre `X-Forwarded-For` de derecha a izquierda y se queda
  con la primera IP que no es de confianza. Esa es la que añade Caddy, no la que haya mandado el
  cliente. Caddy ≥ 2.5 además descarta el `X-Forwarded-For` entrante de clientes que no están en
  `trusted_proxies`.
- **También afecta a `X-Forwarded-Proto`:** `request.base_url` pasa a ser `https://…`. La URL del
  feed de la agenda (`routers/agenda.py`) antes salía como `http://…/agenda/<token>.ics`, con el
  token secreto viajando en claro hasta la redirección de Caddy. Ahora sale en https.

Otro contenedor en otra red de Docker no llega a esta red. Un proceso del propio host que conecte a
`127.0.0.1:8000` sí puede mandar el `X-Forwarded-For` que quiera, igual que Caddy: el host es la
frontera de confianza.

`tests/test_proxy_headers.py` lee el compose y pasa esos valores por
`uvicorn.Config(...).load()`, la misma ruta que en producción. Comprueba que desde el gateway se
registra la IP real (audit y rate limit), que desde otro origen la cabecera se ignora, que una IP
falsa puesta por el cliente no gana, que `FORWARDED_ALLOW_IPS` coincide con el gateway y que el
puerto solo se publica en `127.0.0.1`.

## Deploy (paso manual una sola vez)

`deploy.sh` aplica el compose nuevo (`up -d` recrea el contenedor en la red nueva). Antes del primer
deploy con este cambio, en el EC2:

1. **Confirmar el upstream de Caddy.** Tiene que ir a loopback:
   ```bash
   grep -n reverse_proxy /etc/caddy/Caddyfile
   ```
   - `127.0.0.1:8000`: listo.
   - `localhost:8000`: funciona, pero es mejor cambiarlo a `127.0.0.1:8000`. `localhost` también
     resuelve a `::1`, y el 8000 ya no escucha en IPv6, así que cada conexión fallaría primero por
     ahí.
   - La IP privada o pública del EC2, o un contenedor: **no desplegar todavía**. Con el bind a
     loopback, Caddy dejaría de llegar. Cambiar el upstream a `127.0.0.1:8000` y
     `sudo systemctl reload caddy` (el 8000 sigue publicado en 0.0.0.0 hasta el deploy, así que el
     reload no corta nada).
2. **Confirmar que la subnet está libre:**
   ```bash
   docker network ls -q | xargs docker network inspect -f '{{.Name}} {{range .IPAM.Config}}{{.Subnet}}{{end}}'
   ip route
   ```
   Nada debería usar `172.30.0.0/24`. Si algo la usa, cambiar la subnet, el gateway y
   `FORWARDED_ALLOW_IPS` juntos en el compose (el test falla si no coinciden).
3. `./deploy.sh`. Al final comprueba el health en `localhost:8000` **y** a través de Caddy
   (`PUBLIC_HEALTH_URL`, por defecto `https://api.medicosporvenezuela.org/api/v1/health`). Si el
   primero pasa y el segundo no, el problema es el upstream del paso 1.
4. Verificar:
   ```bash
   docker inspect mpv-api -f '{{json .NetworkSettings.Networks}}'        # red *_api, 172.30.0.x
   docker exec mpv-api printenv FORWARDED_ALLOW_IPS                       # 172.30.0.1
   ss -ltnp | grep ':8000'                                                # solo 127.0.0.1:8000
   ```
   Después, una lectura clínica desde el navegador debe dejar en `audit_log.ip` la IP pública
   de quien la hizo, no `172.30.0.1`:
   ```sql
   select created_at, ip from audit_log where action = 'READ_CLINICAL_DATA'
   order by created_at desc limit 5;
   ```

**Rollback:** volver a desplegar el commit anterior. El compose viejo publica de nuevo en
`0.0.0.0:8000` y no define `FORWARDED_ALLOW_IPS`. No hay nada que deshacer en Caddy.
