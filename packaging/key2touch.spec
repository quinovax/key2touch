# key2touch: keyboard-to-touchscreen mapper for Wayland (Fedora RPM)
# Upstream publishes no tagged tarballs yet; Source0 is a git snapshot of the commit below.
%global commit e3272fec5d1a92a9b4ed8b9b906b01c0826bcf08
%global snapdate 20261104
%global appid io.github.shorin_kiwata.key2touch
# python3_sitelib normally comes from python3-rpm-macros.  The fallback below
# only expands when that macro package is absent: it asks the interpreter that
# will really run the app for its X.Y version, so the site-packages path tracks
# the Python on the build host instead of a hardcoded 3.15 that goes stale.
%{!?python3_sitelib:%global python3_sitelib %{_prefix}/lib/python%( /usr/bin/python3 --version 2>&1 | cut -d' ' -f2 | cut -d. -f1,2 )/site-packages}

Name:           key2touch
Version:        %{snapdate}
Release:        2%{?dist}
Summary:        Map keyboard keys to touchscreen input on Wayland
License:        GPL-3.0-or-later
URL:            https://github.com/SHORiN-KiWATA/key2touch
Source0:        %{name}-%{snapdate}.tar.gz

BuildArch:      noarch
BuildRequires:  desktop-file-utils
BuildRequires:  python3-rpm-macros
BuildRequires:  python3-evdev
BuildRequires:  /usr/bin/python3
Requires:       python3
Requires:       python3-evdev
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       libadwaita >= 1.5
Requires:       gtk4-layer-shell >= 1.0
# udev rule that grants the input group access to /dev/uinput
Requires:       systemd-udev
# pkexec is used from the GUI to join the input group and repair uinput
Requires:       polkit
# Primary target of the app; the Android games and the evdev touchscreen
Recommends:     waydroid

%description
key2touch maps keyboard keys (and mouse movement) to touchscreen events on
Wayland desktops by creating a virtual touchscreen through uinput.  It is
designed for playing Android games under Waydroid: taps, WASD joysticks,
multi-touch, mouse-look regions, skill keys with drag-to-aim, per-window
focus rules, a visual in-game layout editor and a system tray icon.

%prep
%setup -q

%build
# Intentionally empty: pure-Python app, nothing to compile.

%install
rm -rf %{buildroot}

# Importable package: python -m key2touch resolves it from site-packages.
install -d -m 0755 %{buildroot}%{python3_sitelib}
cp -a key2touch %{buildroot}%{python3_sitelib}/
find %{buildroot}%{python3_sitelib}/key2touch -name __pycache__ -type d -exec rm -rf {} +

# Launcher.  Upstream bin/key2touch derives PYTHONPATH from its own location
# (site-packages install would add /usr and force bytecode into ~/.cache,
# ignoring the precompiled tree we ship).  Install a thin wrapper that just
# execs the module; key2touch/__main__.py handles the layer-shell preload and
# keeps bytecode in-tree for a read-only prefix.
install -d -m 0755 %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/key2touch <<'EOF'
#!/bin/sh
# key2touch RPM launcher (upstream bin/key2touch assumes a source checkout)
exec /usr/bin/python3 -m key2touch "$@"
EOF
chmod 0755 %{buildroot}%{_bindir}/key2touch

# Desktop entry + hicolor icon
install -d -m 0755 %{buildroot}%{_datadir}/applications
install -p -m 0644 data/%{appid}.desktop %{buildroot}%{_datadir}/applications/
install -d -m 0755 %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -p -m 0644 data/%{appid}.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/

# Tray icon: gui.py loads <package-parent>/data/<appid>.svg for the
# StatusNotifierItem (site-packages/data from the installed tree).
install -d -m 0755 %{buildroot}%{python3_sitelib}/data
install -p -m 0644 data/%{appid}.svg %{buildroot}%{python3_sitelib}/data/

# udev rule: /dev/uinput group=input mode=0660; ignore key-mirror device
install -d -m 0755 %{buildroot}%{_udevrulesdir}
install -p -m 0644 data/60-key2touch-uinput.rules %{buildroot}%{_udevrulesdir}/

# Load the uinput kernel module at boot (file content: uinput)
install -d -m 0755 %{buildroot}%{_modulesloaddir}
install -p -m 0644 data/key2touch-uinput.conf %{buildroot}%{_modulesloaddir}/

desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop

%check
# The suite imports evdev for real ecodes; guarded so minimal chroots pass.
if /usr/bin/python3 -c 'import evdev' 2>/dev/null; then
  PYTHONPATH=. /usr/bin/python3 -m unittest discover -s tests -t . -v
else
  echo 'python3-evdev unavailable: skipping unit tests' >&2
fi

%post
# Apply the udev rule immediately (containers / rpm-ostree: silently skip).
if [ -S /run/udev/control ]; then
  udevadm control --reload-rules || :
fi
exit 0

%posttrans
# Right after install, before any reboot: load uinput and recreate its
# device node under the new group/mode (same as the in-app repair button).
if [ -S /run/udev/control ]; then
  udevadm control --reload-rules || :
  /usr/sbin/modprobe uinput || :
  udevadm trigger --name-match=uinput || udevadm trigger || :
fi
exit 0

%postun
if [ $1 -eq 0 ] && [ -S /run/udev/control ]; then
  udevadm control --reload-rules || :
fi
exit 0

%files
%license LICENSE
%doc README.md
%{python3_sitelib}/key2touch/
%dir %{python3_sitelib}/data
%{python3_sitelib}/data/%{appid}.svg
%{_bindir}/key2touch
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_udevrulesdir}/60-key2touch-uinput.rules
%{_modulesloaddir}/key2touch-uinput.conf

%changelog
* Sun Nov 08 2026 quinovax <quinovax@users.noreply.github.com> - 20261104-2
- Require libadwaita >= 1.5 (Adw.PreferencesDialog / Adw.AlertDialog) and
  gtk4-layer-shell >= 1.0, per a source audit of the GUI modules.
- Replace the source-checkout launcher with a site-packages-aware wrapper so
  the shipped precompiled bytecode is actually used.

* Sun Nov 08 2026 quinovax <quinovax@users.noreply.github.com> - 20261104-1
- Initial Fedora packaging: git snapshot source; Python package installed to
  site-packages with build-time bytecode; udev rule granting /dev/uinput to
  the input group; modules-load.d for uinput; desktop entry + hicolor icon;
  scriptlets reload/trigger udev after install.