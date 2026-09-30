# Video Workflow

Run `./install.sh` to install it. It copies the app to `~/.local/share/video-workflow`, adds the `video-workflow` command, the app-menu entry and icon, and pins it to the nwg-dock. Pass `--no-pin` to skip the dock. Re-run it after editing the code; the installed copy doesn't follow this folder.

Each video is one folder in `~/Videos/Projects`:

```
~/Videos/Projects/<Video>/
  Media/              everything to edit with; import this one folder into Resolve
    Footage/            OBS recordings and recso exports
    Voiceover/          polished WAVs
    Voiceover/Raw/      Audacity exports
    Music/  Images/  Other/
  Recso/              recso projects, one folder per recording (only once recso has been opened from here)
  Audacity/           Audacity project files (.aup3)
  Renders/            final exports from Resolve
```

1. Press **+** and name the video.
2. **Open OBS.** Recordings save straight into `Media/Footage/`. If OBS is already running, for example in the tray from the bar's record button, its save folder is switched over obs-websocket. Recordings made from the bar button still go to `~/Videos`. The app shows a banner offering to move them into the project.
   **Or open recso** (the row shows only when recso is installed: its `.deb`, or an AppImage in `~/Applications`). Recordings save to `Recso/`, and exports from recso's editor land in `Media/Footage/`, where they show up under Files like OBS recordings. It needs recso's Automation API: turn it on once in recso under **Settings ▸ Advanced ▸ Automation API**. If it's off, Open says so.
3. **Open Audacity.** Save and Export dialogs start in `Audacity/` and `Media/Voiceover/Raw/`. Audacity only reads these on startup, so quit it first if it is already open.
4. **Polish voiceover** reads from `Media/Voiceover/Raw/` and writes a de-essed, loudness-levelled mono 48 kHz, 24-bit WAV to `Media/Voiceover/`. The default target is −16 LUFS with a −1.5 dB true-peak ceiling. Use **Gentle** if consonants lose clarity, **Strong** if they still hiss.
5. **Open Resolve.** The first time, create a Resolve project with the same name as the video; the app copies the name to the clipboard. After that, Open goes straight into it through Resolve's "Load last working project" preference. To import: go to the **Media** page, then in **Media Storage** open **Projects ▸ <Video>**. Right-click **Media** and choose **Add Folder and SubFolders into Media Pool (Create Bins)**. You get Footage, Voiceover, Music, Images and Other bins. The app shows these steps under **Import into DaVinci Resolve**, next to a button that copies the Media path for **File ▸ Import ▸ Media…**.

Drop files onto the window, or use **Add files…**, to copy music, images or other material in; they are sorted by type. The mic gain for the noise-reduced USB mic is under **Preferences**.

## How the apps are configured

| App | What changes | When |
|---|---|---|
| OBS | Record path of the current profile (`~/.config/obs-studio/basic/profiles/*/basic.ini`), or `SetRecordDirectory` over obs-websocket when OBS is running | Open OBS |
| recso | `recording.outputDir` and `export.outputDir` over recso's Automation API (a Unix socket). recso forgets them when it quits, so Open sets them every time. If recso isn't running, `recso-cli --launch` starts it. | Open recso |
| Audacity | `Directories/*` and `ExportAudioDialog/DefaultPath` in `~/.config/audacity/audacity.cfg`. The export dialog ignores `Directories/Export` while `DefaultPath` is set. | Open Audacity |
| Resolve | Media Storage in `configs/config.dat`: location 1, which Resolve also uses as scratch space for cache, stills and backups, is `~/Videos/.resolve`; location 2 is `~/Videos/Projects`, for browsing. The original is backed up as `config.dat.before-video-workflow`. Before a launch, it sets `AutoReloadPrevProj` and `LastWorkingProject` in `configs/config.user.xml` when a Resolve project with the video's name exists. | App start and Open Resolve, only while Resolve is closed |

The free Resolve can't be scripted from outside, and it runs its own menu scripts in a sandbox without file access or dialogs. So the app prepares folders and settings, and the import is a manual right-click.

The live USB mic filter is configured in `~/.config/pipewire/pipewire.conf.d/99-usb-voiceover-denoising.conf`. Room samples and measurements are in [voiceover-noise](../voiceover-noise/README.md).
