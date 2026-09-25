# Native Linux server installation

This installs the Family Dashboard **server** with a systemd unit and a persistent SQLite database. A dedicated touch display only needs the separate kiosk client in `clients/linux`; it connects to this server and stores no calendar data locally.

## Install

From a checkout on the target Linux host:

```sh
sudo ./packaging/linux/install.sh
```

The service starts at boot and listens on `127.0.0.1:8080` by default. Set `FAMILY_DASHBOARD_HOST=0.0.0.0` in `/etc/family-dashboard.env` when the dashboard should be reachable from a tablet or another device on the LAN.

Private and local calendar feed addresses are blocked by default. Set `FAMILY_DASHBOARD_ALLOW_PRIVATE_CALENDARS=true` only when the dashboard must fetch feeds from a trusted host on your LAN.

For a custom prefix, port, or data directory:

```sh
sudo ./packaging/linux/install.sh --prefix /opt/family-dashboard --data-dir /srv/family-dashboard --port 8080
```

After installation, edit `/etc/family-dashboard.env`, set a strong `FAMILY_DASHBOARD_PASSWORD` and `FAMILY_DASHBOARD_SECRET_KEY`, then run:

```sh
sudo systemctl restart family-dashboard
sudo systemctl status family-dashboard
```

## Uninstall

Stop the service without deleting the database:

```sh
sudo ./packaging/linux/uninstall.sh
```

Remove the application, service environment, and persistent data only when the data should be deleted:

```sh
sudo ./packaging/linux/uninstall.sh --purge
```

## Docker alternative

From the project root:

```sh
cp .env.example .env
# Edit .env and choose a password and secret.
docker compose up -d --build
```

The SQLite database is stored in the `family-dashboard-data` Docker volume and survives container replacement.
