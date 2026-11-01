# T470s Status Dashboard

Refresh-driven laptop health dashboard for desktop and mobile browsers.

## Behavior

- `GET /` renders the last persisted snapshot immediately.
- The page performs exactly one `POST /api/refresh` after its first paint.
- There is no timer, polling loop, WebSocket, or background collector.
- Refreshes within 10 seconds reuse the most recent snapshot.
- Snapshot writes use a temporary file followed by atomic replacement.
- Static assets are immutable for one year; HTML and APIs are `no-store`.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export DASHBOARD_PASSWORD_HASH='pbkdf2_sha256$...'
export DASHBOARD_SESSION_SECRET='...'
export DASHBOARD_COOKIE_SECURE=0
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
```

Run tests:

```bash
.venv/bin/pytest -q
```

## Production layout

```text
Browser
  -> HTTPS status.mitoto.cn
  -> Nginx on 47.97.193.62
  -> 127.0.0.1:8081 on the server
  -> frps/frpc tunnel
  -> 127.0.0.1:8765 on the T470s
```

The application and FRP remote proxy both listen on loopback only. The only
public entry is Nginx on HTTPS port 443.

Live URL: <https://status.mitoto.cn>

## Deployment ownership

- `ops/laptop-status-dashboard.service` — user-level Uvicorn service on the T470s.
- `ops/frpc-status-snippet.toml` — additional proxy block in the existing frpc client.
- `ops/nginx-status-dashboard-upstream.conf` — rate-limit zones and the server-side loopback upstream.
- `ops/nginx-status.mitoto.cn.conf` — HTTPS vhost, route allowlist, authentication rate limit, and HTTP redirect.
- `ops/certbot-pythonpath.conf` — systemd override required because the server's pip urllib3 shadows Ubuntu's Certbot dependencies.

The schedule panel combines user `crontab`, user-level systemd timers, and
OpenClaw automations. It also reports the number of OS maintenance timers
without exposing their full command lines. Because Ubuntu's `crontab` binary
is setgid, the user service must not enable `NoNewPrivileges`; the application
still binds only to loopback and remains behind the authenticated HTTPS proxy.

The frps server must allow remote ports `8080-8081`, keep
`proxyBindAddr = "127.0.0.1"`, and allow at least three ports per client.

## Security and cache boundary

- The password is stored only as a PBKDF2 hash in the mode-600 file
  `~/.config/laptop-status-dashboard.env`.
- Sessions use an HttpOnly, Secure, SameSite=Strict cookie.
- Operational routes require authentication; `/healthz` exposes only `{status: ok}`.
- Versioned static assets are cached for one year. Login, HTML, and refresh API
  responses are never cached.
- The Let's Encrypt certificate is issued for `status.mitoto.cn`; HTTP redirects
  to HTTPS and simulated automatic renewal must pass before delivery.
