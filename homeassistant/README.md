# Family Dashboard for Home Assistant

This directory contains a Home Assistant custom integration and a Lovelace iframe card for Family Dashboard. The integration polls the dashboard API every five minutes and creates one sensor for the next event and one for the next birthday.

## Install the integration

1. Copy `custom_components/familydashboard` to `<home-assistant-config>/custom_components/familydashboard`.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and select **Family Dashboard**.
4. Enter the Family Dashboard base URL, such as `https://dashboard.example.com`. Do not append an API endpoint.
5. Enter the bearer token only when the dashboard API requires one. The token is stored in the Home Assistant config entry and is sent only as an `Authorization: Bearer ...` request header.

The config flow validates all three endpoints before creating the entry:

- `/api/dashboard/summary`
- `/api/events`
- `/api/birthdays`

## Sensors

The integration accepts a JSON object or list from every endpoint. Events and birthdays can be returned directly as lists or under common list keys such as `events` and `birthdays`. An explicit `next_event` or `next_birthday` object in the summary response takes precedence. Otherwise, the integration selects the earliest current or future record.

The sensor state is the record's title, name, or person. Top-level response fields are exposed as attributes, excluding credential-like field names. An ISO `next_at` attribute and a `days_until` attribute are added when a date can be interpreted.

## Refresh service

Call `familydashboard.refresh` to refresh immediately instead of waiting for the next five-minute poll:

```yaml
action: familydashboard.refresh
```

## Install the Lovelace card

1. Copy `www/family-dashboard-card.js` to `<home-assistant-config>/www/family-dashboard-card.js`.
2. Add the JavaScript resource from **Settings → Dashboards → Resources**:

```yaml
url: /local/family-dashboard-card.js
type: module
```

3. Add the card to a dashboard:

```yaml
type: custom:family-dashboard-card
url: https://dashboard.example.com
title: Family Dashboard
height: 650
```

`url` accepts an absolute HTTP(S) address or a same-origin relative URL. `height` is optional and accepts 320–2000 pixels. The card fills the available width, preserves touch scrolling and browser zoom, and supports fullscreen content.

A ready-to-edit example is available in `lovelace-family-dashboard.yaml`.

## Security and browser compatibility

Do not put a bearer token in card YAML, an iframe URL, JavaScript, or a committed file. An iframe cannot add the custom bearer header used by the integration. If the dashboard requires authentication, expose its UI through a same-origin reverse proxy with an appropriate browser session mechanism.

The dashboard must allow the Home Assistant origin in `Content-Security-Policy: frame-ancestors` and must not send `X-Frame-Options: DENY`. An HTTPS Home Assistant frontend also cannot load an HTTP dashboard in the iframe; use HTTPS for the dashboard or expose it through the same HTTPS origin.
