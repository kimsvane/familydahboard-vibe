# Family Dashboard Voice

Stemmeassistent (Assist-satellit) til Home Assistant på en Fedora-enhed — fx den samme Surface Pro 4, der viser kiosk-dashboardet. Den lytter efter et wake word på enheden, sender din tale til Home Assistant og afspiller svaret. Både wake word, talegenkendelse og samtalemodel (LLM) konfigureres i Home Assistant.

Pakken installerer **Linux Voice Assistant** (Open Home Foundation, Apache-2.0) i et virtuelt Python-miljø under `/opt/family-dashboard-voice` og registrerer en systemd-tjeneste. Linux Voice Assistant taler ESPHome-protokollen på port **6053**, så Home Assistant automatisk genkender enheden via ESPHome-integrationen.

## Installér

```sh
sudo dnf install ./family-dashboard-voice-0.1.0-1.noarch.rpm
```

Færdigbyggede pakker ligger under **Releases** i repoet:
https://github.com/kimsvane/familydahboard-vibe/releases/latest

Installationen kræver internet én gang: `%post`-scriptet downloader den faste version af Linux Voice Assistant, bygger `venv` og starter tjenesten som den auto-loggede desktopbruger (den, der ejer PipeWire-lyden).

## Første opsætning

1. Bekræft at lyden virker (Surface Pro 4):

   ```sh
   wpctl status
   arecord -f S16_LE -r 16000 -c 1 -d 3 /tmp/test.wav && aplay /tmp/test.wav
   ```

2. Juster indstillinger i `/etc/family-dashboard-voice.env` (fx `WAKE_MODEL`, `NETWORK_INTERFACE`, `MIC_NOISE_SUPPRESSION`), og genstart:

   ```sh
   sudo systemctl restart family-dashboard-voice
   sudo systemctl status family-dashboard-voice
   ```

3. Tilslut enheden i Home Assistant: **Indstillinger → Enheder og tjenester → ESPHome → Tilføj integration → opsæt en anden instans**, og indtast Surface-ens IP med port 6053.

## Opsæt pipeline og LLM i Home Assistant

1. **Assist-satelliten** dukker op som en enhed. Lav en **Assist-pipeline** (Indstillinger → Voice assistants), vælg satellite, wake word (fx "okay nabu"), talegenkendelse (Whisper eller HA Cloud) og talesyntese (Piper lokal).
2. Vælg **samtalemodellen** i pipelinen under "Conversation agent". Home Assistant har officielle integrationer for:
   - **Claude (Anthropic)** – home-assistant.io/integrations/anthropic
   - **Google Gemini** – home-assistant.io/integrations/google_generative_ai_conversation
   - **OpenAI** – home-assistant.io/integrations/openai
   - **Ollama (lokalt)** – home-assistant.io/integrations/ollama
3. Eksponér kun de entiteter, modellen må styre (Indstillinger → Voice assistants → Exposed entities).

Wake word kan senere skiftes live fra enhedens side i Home Assistant uden genstart.

## Opdatér

```sh
sudo dnf install ./family-dashboard-voice-0.2.0-1.noarch.rpm
```

`/etc/family-dashboard-voice.env` og `/var/lib/family-dashboard-voice` (præferencer, brugerdefinerede wake words/lyde) bevares.

## Afinstallér

```sh
sudo /opt/family-dashboard-voice/scripts/uninstall.sh          # stop
sudo /opt/family-dashboard-voice/scripts/uninstall.sh --purge  # slet alt
```

## Manuelle installation uden RPM

Fra et checkout af repoet på målmaskinen:

```sh
sudo ./clients/voice/scripts/install.sh
```

Flere muligheder: `--user NAME` (tjeneste-bruger), `--no-deps` (spring dnf-afhængigheder over), `--force` (gen-skriv eksisterende konfig).

## Udvikling

```sh
bash clients/voice/tests/check.sh   # syntaks- og pakke-tjek; sæt LVA_NETWORK_CHECK=0 for at springe netværkstjek over
```

Byg RPM (Linux/x86_64 med `rpm`):

```sh
cp -r clients/voice/scripts rpmbuild/SOURCES/
cp -r clients/voice/systemd rpmbuild/SOURCES/
cp -r clients/voice/etc rpmbuild/SOURCES/
rpmbuild -bb --define "_topdir $PWD/rpmbuild" clients/voice/family-dashboard-voice.spec
```

I CI bygges pakken automatisk af `.github/workflows/voice.yml`.