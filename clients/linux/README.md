# Family Dashboard Kiosk

Kiosk-appen er en dedikeret Linux-klient til en touch-enhed. Den kører ikke kalendere eller database selv. Den starter automatisk efter login, åbner Family Dashboard i fuldskærm og logger ind med adgangskoden på dashboardets egen login-skærm.

Pakkerne findes som `.deb` og `.rpm` til `x64` og `arm64`. Dermed dækkes blandt andet Surface Pro 4, Fedora, Debian, Ubuntu, Raspberry Pi 5 og andre 64-bit ARM-enheder.

## Første gang

1. Installer pakken på den enhed, der skal vise dashboardet.
2. Start appen fra programmen, eller log ind på enheden.
3. Indtast TrueNAS-adressen, for eksempel `http://truenas:8080`.
4. Tryk **Forbind**.
5. Indtast dashboardets adgangskode på den viste login-skærm.

Serveradressen gemmes lokalt i `~/.config/family-dashboard-kiosk/client.json`. Der gemmes ingen adgangskode på enheden.

## Installér

### Fedora, RHEL, Rocky Linux og andre RPM-systemer

```sh
sudo dnf install ./family-dashboard-kiosk-0.1.0-x64.rpm
```

På ARM skal du bruge `family-dashboard-kiosk-0.1.0-arm64.rpm`.

### Debian, Ubuntu, Mint og lignende

```sh
sudo apt install ./family-dashboard-kiosk-0.1.0-x64.deb
```

På ARM skal du bruge `family-dashboard-kiosk-0.1.0-arm64.deb`.

Pakkerne bygges automatisk uden signering, og pakkehåndtering kan derfor vise en advarsel om manglende udgiver-signatur.

## Start automatisk ved boot

Pakken installerer en XDG-autostart-post, så appen starter automatisk, når den dedikerede bruger logger ind på det grafiske skrivebord.

For at skærmen skal vise dashboardet direkte efter en strømcyklus skal den dedikerede bruger desuden have automatisk login aktiveret:

- **Fedora/GNOME:** Åbn *Indstillinger → Brugere → Automatisk login* for den dedikerede bruger.
- **Debian/Ubuntu:** Sæt `AutomaticLoginEnable=true` og `AutomaticLogin=<brugernavn>` i `/etc/gdm3/custom.conf`, og genstart GDM.
- **Raspberry Pi OS:** Brug indstillingen for automatisk login i Raspberry Pi OS. Kiosk-appen kræver Raspberry Pi OS Desktop eller en anden Linux med et grafisk skrivebord; Raspberry Pi OS Lite understøttes ikke af denne Electron-pakke.

## Betjening

- Tryk på tandhjulet i nederste højre hjørne for at åbne **Indstillinger**.
- Der kan ændres serveradresse, genindlæses dashboardet eller lukkes appen.
- Hvis serveren ikke svarer, prøver appen automatisk hvert 15. sekund og viser en stor **Prøv igen**-knap.
- Skærmen holdes vågen af appen og `systemd-inhibit`, så den ikke går i pause under visningen.
- Berøring, rulning og tastatur fungerer normalt, fordi dashboardet vises i Chromium.

## Opdatér

Byg en ny pakke og installér den over den eksisterende:

```sh
sudo dnf install ./family-dashboard-kiosk-0.1.0-x64.rpm
```

Serverens kalendere og øvrige data ligger kun på TrueNAS. En opdatering eller geninstallation af kiosk-appen sletter dem ikke.

## Nulstil opsætningen

```sh
rm -f ~/.config/family-dashboard-kiosk/client.json
```

Start appen igen, og indtast serveradressen.

## Udvikling

```sh
npm ci
npm run check
npm test
npm start
```

Byg Linux-pakker på en Linux-maskine eller i CI:

```sh
npm run dist
```
