%global app_prefix /opt/family-dashboard-voice

Name:           family-dashboard-voice
Version:        0.1.0
Release:        1%{?dist}
Summary:        Home Assistant stemmeassistent (Assist-satellit) til Surface Pro 4

License:         Apache-2.0
URL:            https://github.com/kimsvane/familydahboard-vibe
BuildArch:      noarch

Requires:       systemd
Requires:       python3
Requires:       python3-pip
Requires:       python3-devel
Requires:       gcc
Requires:       gcc-c++
Requires:       make
Requires:       pipewire
Requires:       pipewire-pulseaudio
Requires:       pipewire-alsa
Requires:       alsa-utils
Requires:       alsa-lib
Requires:       mpv-libs
Requires:       avahi-tools
Requires:       pulseaudio-utils
Requires:       iproute
Requires:       procps-ng
Requires:       curl
Requires:       ca-certificates

%description
Home Assistant Assist-satellit til en Fedora-enhed (fx Surface Pro 4).
Installeres i et virtuelt Python-miljø under /opt/family-dashboard-voice
og lytter på port 6053 (ESPHome-protokollen), så Home Assistant automatisk
genkender enheden som en stemmesatellit. Linux Voice Assistant fra
Open Home Foundation bruges som kernen (Apache-2.0).

%install
install -d -m 0755 %{buildroot}%{app_prefix}/app
install -d -m 0755 %{buildroot}%{app_prefix}/venv
install -d -m 0755 %{buildroot}%{app_prefix}/scripts
install -d -m 0755 %{buildroot}%{app_prefix}/etc
install -m 0755 %{_sourcedir}/scripts/install.sh %{buildroot}%{app_prefix}/scripts/install.sh
install -m 0755 %{_sourcedir}/scripts/uninstall.sh %{buildroot}%{app_prefix}/scripts/uninstall.sh
install -m 0644 %{_sourcedir}/systemd/family-dashboard-voice.service.in %{buildroot}%{app_prefix}/etc/family-dashboard-voice.service.in
install -m 0640 %{_sourcedir}/etc/family-dashboard-voice.env %{buildroot}%{app_prefix}/etc/family-dashboard-voice.env

%post
%{app_prefix}/scripts/install.sh --from-rpm

%preun
if [ "$1" -eq 0 ]; then
  systemctl disable --now family-dashboard-voice.service >/dev/null 2>&1 || true
fi

%postun
if [ "$1" -eq 0 ]; then
  rm -f /etc/systemd/system/family-dashboard-voice.service
  systemctl daemon-reload >/dev/null 2>&1 || true
fi

%files
%dir %attr(0755,root,root) %{app_prefix}
%dir %attr(0755,root,root) %{app_prefix}/app
%dir %attr(0755,root,root) %{app_prefix}/venv
%dir %attr(0755,root,root) %{app_prefix}/scripts
%dir %attr(0755,root,root) %{app_prefix}/etc
%attr(0755,root,root) %{app_prefix}/scripts/install.sh
%attr(0755,root,root) %{app_prefix}/scripts/uninstall.sh
%attr(0644,root,root) %{app_prefix}/etc/family-dashboard-voice.service.in
%attr(0640,root,root) %{app_prefix}/etc/family-dashboard-voice.env

%changelog
* Sat Oct 03 2026 opencode <kimsvane@users.noreply.github.com> - 0.1.0-1
- Første pakke: linux-voice-assistant som Home Assistant Assist-satellit på Fedora