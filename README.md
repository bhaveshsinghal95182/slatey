# Slatey

A clean slate for every video. Slatey is a small GTK 4 / libadwaita app for Linux that keeps each video in one folder and opens OBS, Audacity, recso and DaVinci Resolve already pointed at it. It also polishes voiceovers and cuts the pauses out of recordings with ffmpeg.

Each video is one folder in `~/Videos/Projects`:

```
~/Videos/Projects/<Video>/
  Media/              everything to edit with; import this one folder into Resolve
    Footage/            OBS recordings, recso exports and cut videos
    Voiceover/          polished WAVs
    Voiceover/Raw/      Audacity exports
    Music/  Images/  Other/
  Recso/              recso projects (only once recso has been opened from here)
  Audacity/           Audacity project files (.aup3)
  Renders/            final exports from Resolve
```

## Requirements

- Linux with Python 3.10 or newer, PyGObject, GTK 4.10+ and libadwaita 1.6+
- ffmpeg 7.0 or newer, for **Polish voiceover** and **Cut pauses**
- Whichever of these you use:
  - [OBS Studio](https://obsproject.com/) (`obs` on the `PATH`)
  - [Audacity](https://www.audacityteam.org/)
  - [DaVinci Resolve](https://www.blackmagicdesign.com/products/davinciresolve), installed at `/opt/resolve`
  - recso: its `.deb`, or an AppImage in `~/Applications`. Its row only appears when it's installed.

On Arch:

```sh
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg obs-studio audacity
```

On Debian/Ubuntu (Ubuntu 24.10+ or Debian 13+, for libadwaita 1.6 and ffmpeg 7):

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg obs-studio audacity
```

## Install

```sh
git clone https://github.com/bhaveshsinghal95182/slatey.git
cd slatey
./install.sh
```

This copies the app to `~/.local/share/slatey`, adds the `slatey` command to `~/.local/bin`, and installs an app-menu entry and icon. If you use [nwg-dock](https://github.com/nwg-piotr/nwg-dock-hyprland), it also pins the app to the dock; pass `--no-pin` to skip that.

The installed copy doesn't follow the clone. Run `git pull && ./install.sh` to update.

## Usage

Start it from the app menu or run `slatey`.

1. Press **+** (Ctrl+N) and name the video.
2. **Open OBS.** Recordings save straight into `Media/Footage/`. If OBS is already running, its save folder is switched over obs-websocket. Recordings that end up loose in `~/Videos` show a banner offering to move them into the project.

   **Or open recso.** Recordings save to `Recso/`, and exports from recso's editor land in `Media/Footage/`. Turn on recso's Automation API once under **Settings ▸ Advanced ▸ Automation API**; if it's off, Open says so.
3. **Cut pauses** (optional). Pick a recording. Every pause longer than 0.5 s is shortened to 0.3 s, and the rest is joined into `<name>-cut.mp4` next to it in `Media/Footage/`. The original stays as it is. Both lengths are adjustable; 0 removes pauses completely.
4. **Open Audacity.** Save and Export dialogs start in `Audacity/` and `Media/Voiceover/Raw/`. Audacity only reads these on startup, so quit it first if it's already open.
5. **Polish voiceover** reads from `Media/Voiceover/Raw/` and writes a de-essed, loudness-levelled mono 48 kHz, 24-bit WAV to `Media/Voiceover/`. The default target is −16 LUFS with a −1.5 dB true-peak ceiling. Use **Gentle** if consonants lose clarity, **Strong** if they still hiss.
6. **Open Resolve.** The first time, create a Resolve project with the same name as the video; the app copies the name to the clipboard. After that, Open goes straight into it. To import, go to the **Media** page, open **Projects ▸ <Video>** in **Media Storage**, right-click **Media** and choose **Add Folder and SubFolders into Media Pool (Create Bins)**. The app shows these steps too.

Drop files onto the window, or use **Add files…**, to copy music, images or other material in; they're sorted into folders by type.

### Cutting pauses from the terminal

`cutter.py` also works on its own, on any video with an audio track:

```sh
python3 cutter.py recording.mp4 --dry-run     # list what would be kept, render nothing
python3 cutter.py recording.mp4               # writes recording-cut.mp4
python3 cutter.py recording.mp4 -o out.mp4 --min-silence 0.8 --keep-pause 0.2 --threshold-db -40
```

How it works: ffmpeg's `silencedetect` finds the pauses, every cut point is snapped to a video frame boundary, and one ffmpeg pass keeps the remaining frames and the matching audio. So audio stays in sync however many cuts there are. AV1 sources are re-encoded with NVENC when an NVIDIA GPU is available and SVT-AV1 otherwise (H.264/HEVC sources use NVENC or x264). Audio is always FLAC, which the free Linux build of Resolve can read; it can't read AAC.

## What the app changes outside the project folder

| App | What changes | When |
|---|---|---|
| OBS | Record path of the current profile (`~/.config/obs-studio/basic/profiles/*/basic.ini`), or `SetRecordDirectory` over obs-websocket when OBS is running | Open OBS |
| recso | `recording.outputDir` and `export.outputDir` over recso's Automation API (a Unix socket). recso forgets them when it quits, so Open sets them every time. | Open recso |
| Audacity | `Directories/*` and `ExportAudioDialog/DefaultPath` in `~/.config/audacity/audacity.cfg` | Open Audacity |
| Resolve | Media Storage in `~/.local/share/DaVinciResolve/configs/config.dat`: location 1, which Resolve also uses for cache, stills and backups, becomes `~/Videos/.resolve`; location 2 is `~/Videos/Projects`. The original is backed up as `config.dat.before-video-workflow`. Before a launch it sets `AutoReloadPrevProj` and `LastWorkingProject` in `configs/config.user.xml` when a Resolve project with the video's name exists. | App start and Open Resolve, only while Resolve is closed |

The free Resolve can't be scripted from outside, and it runs its own menu scripts in a sandbox without file access or dialogs. So the app prepares folders and settings, and the import is a manual right-click.

**Preferences ▸ USB microphone** sets the input gain of a custom PipeWire noise-reduction source named `usb_voiceover_denoised`. Without that source it just says so, so you can ignore it.

## Uninstall

```sh
rm -r ~/.local/share/slatey ~/.config/slatey
rm ~/.local/bin/slatey \
   ~/.local/share/applications/io.github.bhaveshsinghal95182.Slatey.desktop \
   ~/.local/share/icons/hicolor/scalable/apps/io.github.bhaveshsinghal95182.Slatey.svg
sed -i '/^io\.github\.bhaveshsinghal95182\.Slatey$/d' ~/.cache/nwg-dock-pinned 2>/dev/null
```

To restore Resolve's original Media Storage, copy `config.dat.before-video-workflow` back over `config.dat` while Resolve is closed. Your projects in `~/Videos/Projects` are left alone.

## Tests

```sh
python3 -m unittest test_cutter -v
```

## License

[MIT](LICENSE). The "S" in the icon is from [Monoton](https://fonts.google.com/specimen/Monoton) by Vernon Adams, under the SIL Open Font License.
