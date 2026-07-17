# VoHive Keepalive

[简体中文](README.md) | [English](README_EN.md)

An unofficial VoHive companion service for periodically and audibly using cellular data to keep long-term SMS verification SIM cards active.

At a scheduled time, it briefly enables mobile data on a selected device, **forces an HTTPS request through the configured cellular interface**, records the actual traffic used and the success time, and then restores the desired idle state for receiving SMS messages. Both successful and failed runs can be reported through PushDeer.

> This repository contains only independently developed keepalive service and UI integration code. It does not include the VoHive binary, user data, passwords, PushDeer keys, ICCIDs, phone numbers, server addresses, or deployment secrets. This project is not affiliated with or endorsed by VoHive.

## Features

- **Native-style entry:** adds a “Keepalive / 保号” item to the VoHive sidebar and renders the management view in the main content area.
- **Scheduled runs:** configurable day-based interval; defaults to 120 days and does not consume cellular data immediately after first deployment.
- **Real cellular verification:** uses Linux `SO_BINDTODEVICE` to lock the HTTPS request to the selected interface, preventing a normal Ethernet route from producing a false success.
- **Traffic accounting:** records RX, TX, and total bytes for both the complete cellular session and the verification request.
- **Safety limits:** configurable connection timeout, request timeout, session duration, total session traffic, and response-size caps.
- **Failure retry:** retries failures on a separate interval and never records a failed attempt as a successful keepalive.
- **Idle policies:** restores cellular SMS standby, VoWiFi, or airplane mode after a run.
- **PushDeer notifications:** a success message includes traffic usage and the next scheduled time; a failure message includes the error and retry time.
- **SMS forwarding:** receives VoHive SMS webhooks and relays them through PushDeer, using the SMS body as the notification title and sender/device metadata as the description.
- **Run history:** stores trigger source, HTTP status, duration, traffic, restore result, and errors in SQLite.
- **Boot persistence:** includes a systemd unit; interrupted runs are marked failed and the idle policy is reapplied after restart.
- **Rollback-ready integration:** includes an Nginx gateway example that preserves the original VoHive URL and a rollback script.

## UI Integration

`integration/keepalive-nav.js` injects the keepalive item into the VoHive sidebar. Users continue to open the original VoHive URL:

```text
VoHive sidebar
├── Dashboard
├── Device Management
├── SMS Center
└── Keepalive / 保号  ← added
                       ├── Service status / next run / last success
                       ├── Policy configuration
                       └── Run history
```

Same-port integration is provided by `integration/nginx-vohive-gateway.conf.example`. Nginx listens on the original public port, VoHive is moved to a separate backend port, and the keepalive API is exposed internally through `/keepalive-api/`.

The UI integration has been tested with **VoHive 1.5.5**. If a later VoHive release changes the sidebar DOM, the selectors in the injection script may need to be updated.

## SMS Forwarding to PushDeer

`vohive_pushdeer_bridge.py` provides a loopback-only webhook that forwards SMS messages received by VoHive to PushDeer:

- PushDeer title: the SMS body
- PushDeer description: sender, device, and event type
- The SMS body is not duplicated in both title and description
- Accepts JSON and form-encoded webhooks
- Listens on `127.0.0.1:7581` by default

Example installation:

```bash
sudo install -d -m 755 /opt/vohive/bin
sudo install -m 755 vohive_pushdeer_bridge.py /opt/vohive/bin/
sudo install -m 600 pushdeer.env.example /etc/vohive/pushdeer.env
sudo install -m 644 vohive-pushdeer-bridge.service /etc/systemd/system/
sudoedit /etc/vohive/pushdeer.env
sudo systemctl daemon-reload
sudo systemctl enable --now vohive-pushdeer-bridge.service
```

Then configure the VoHive SMS webhook as:

```text
http://127.0.0.1:7581/vohive
```

The real `PUSHDEER_KEY` should only be stored in an environment file with mode `0600`.

## Why the Default Is 120 Days

Under the current giffgaff inactivity policy, a number must have at least one qualifying activity every six months. A mobile data connection qualifies, while receiving SMS messages alone is not listed as a qualifying action. The default 120-day interval leaves roughly two months for network failures, balance issues, retries, and manual intervention.

- [giffgaff: Understanding why your number has been deactivated](https://help.giffgaff.com/en/articles/242797-understanding-why-your-number-has-been-deactivated)
- [giffgaff Terms and Conditions](https://www.giffgaff.com/terms)

Other carriers may use different inactivity rules. Always adjust the interval to match the current terms of your own operator.

## Requirements

- Linux and root privileges, required by `SO_BINDTODEVICE` and network-interface control
- Python 3.10 or newer; the service uses only the Python standard library
- A working VoHive installation with the cellular device already registered
- Local access to the VoHive API
- A cellular interface available under `/sys/class/net/<interface>`
- Optional: Nginx for native sidebar and same-port integration
- Optional: PushDeer for run notifications

## Installation

The following example installs the service under `/opt/vohive-keepalive`. Review every value and adapt the VoHive port, device ID, and cellular interface to your environment before starting it.

```bash
sudo install -d -m 700 /opt/vohive-keepalive /etc/vohive-keepalive /var/lib/vohive-keepalive
sudo install -m 700 vohive_keepalive.py /opt/vohive-keepalive/
sudo install -m 600 config.example.json /etc/vohive-keepalive/config.json
sudo install -m 600 service.env.example /etc/vohive-keepalive/service.env
sudo install -m 644 vohive-keepalive.service /etc/systemd/system/

sudoedit /etc/vohive-keepalive/config.json
sudoedit /etc/vohive-keepalive/service.env

sudo systemctl daemon-reload
sudo systemctl enable --now vohive-keepalive.service
sudo systemctl status vohive-keepalive.service
```

At minimum, configure:

- `device_id`: the device ID shown in VoHive
- `interface`: the cellular data interface, for example `wwan0`
- `VOHIVE_BASE_URL`, `VOHIVE_USER`, and `VOHIVE_PASSWORD`
- `BASIC_PASSWORD`: a separate strong password for the keepalive management API
- `PUSHDEER_KEY` if notifications are required

The service listens on `127.0.0.1:7582` by default. Do not expose the non-TLS Basic Auth endpoint directly to the public Internet.

## Native Sidebar Integration

1. Back up the VoHive configuration.
2. Move VoHive from its public port to a backend port, for example from `7575` to `17575`.
3. Install the injection script:

   ```bash
   sudo install -d -m 755 /opt/vohive-ui-gateway
   sudo install -m 644 integration/keepalive-nav.js /opt/vohive-ui-gateway/
   ```

4. Copy the Nginx example, generate the Basic credential used for the internal keepalive API, and replace `__KEEPALIVE_BASIC_AUTH__`:

   ```bash
   printf '%s' 'YOUR_API_USER:YOUR_STRONG_PASSWORD' | base64
   sudo install -m 600 integration/nginx-vohive-gateway.conf.example \
     /etc/nginx/conf.d/vohive-gateway.conf
   sudoedit /etc/nginx/conf.d/vohive-gateway.conf
   ```

5. Validate and restart the services:

   ```bash
   sudo nginx -t
   sudo systemctl restart vohive.service vohive-keepalive.service nginx.service
   curl -fsS http://127.0.0.1:7575/keepalive-api/status
   ```

6. Confirm that the original VoHive page and WebSocket connections still work before keeping the gateway configuration.

The example ports are only defaults. If your installation paths or configuration format differ, update both Nginx and `integration/rollback-gateway.sh`. The rollback script expects a pre-deployment backup at:

```text
/opt/vohive/config/config.yaml.before-keepalive-gateway
```

## Environment Variables

See [`service.env.example`](service.env.example). The real environment file should have mode `0600` and must never be committed to Git.

| Variable | Purpose | Default |
| --- | --- | --- |
| `VOHIVE_BASE_URL` | VoHive API root URL | `http://127.0.0.1:7575/api` |
| `VOHIVE_USER` | VoHive username | `admin` |
| `VOHIVE_PASSWORD` | VoHive password | empty |
| `BASIC_USER` | Keepalive API username | `admin` |
| `BASIC_PASSWORD` | Keepalive API password | empty |
| `LISTEN_HOST` | Keepalive service listen address | `127.0.0.1` |
| `LISTEN_PORT` | Keepalive service port | `7582` |
| `CONFIG_PATH` | Configuration file | `/etc/vohive-keepalive/config.json` |
| `DATABASE_PATH` | SQLite database | `/var/lib/vohive-keepalive/keepalive.db` |
| `PUSHDEER_KEY` | PushDeer PushKey | empty; notifications disabled |
| `PUSHDEER_ENDPOINT` | PushDeer API endpoint | official endpoint |

## HTTP API

All endpoints except `/health` require HTTP Basic Auth.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `GET` | `/` | Standalone management page |
| `GET` | `/api/status` | Current status, last success, and next run |
| `GET` / `PUT` | `/api/config` | Read or update configuration |
| `GET` | `/api/history?limit=50` | Run history |
| `POST` | `/api/run` | Run immediately; body must be `{"confirm": true}` |

An immediate run enables real cellular data and may incur roaming charges.

## Tests

```bash
python3 -m unittest discover -v
```

When run as a normal user, the root-only interface-binding test is skipped. When run as root on Linux, it performs a real `SO_BINDTODEVICE` test using loopback and does not use cellular data.

## Security Notes

- Never commit `service.env`, a real configuration file, SQLite databases, logs, or the generated Nginx Authorization value.
- Verification targets must use HTTPS and cannot contain embedded credentials.
- `max_session_bytes` is a safety cap and may not exactly match the carrier's billable byte count.
- Verify the device ID, interface name, and idle policy before deployment. Do not click “Run now” on an expensive SIM without reviewing the configuration.
- Add TLS, access control, and firewall rules before any public exposure. Private-network deployment is recommended.

## License

[MIT](LICENSE)
