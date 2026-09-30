#!/usr/bin/env python3
"""Video Workflow: one folder per video, with OBS, Audacity and Resolve pointed at it."""

from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import apps  # noqa: E402
import core  # noqa: E402

APP_ID = "local.descentkatil.VideoWorkflow"

# (folder key, label, icon) in the order they appear under Files.
FILE_SECTIONS = (
    ("Footage", "Footage", "video-x-generic-symbolic"),
    ("Voiceover", "Voiceover · polished", "audio-x-generic-symbolic"),
    ("Voiceover Raw", "Voiceover · raw exports", "audio-input-microphone-symbolic"),
    ("Music", "Music", "folder-music-symbolic"),
    ("Images", "Images", "image-x-generic-symbolic"),
    ("Other", "Other", "folder-symbolic"),
    ("Audacity", "Audacity projects", "folder-documents-symbolic"),
    ("Renders", "Renders", "media-record-symbolic"),
)
MAX_FILE_ROWS = 60


def when(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime)
    return stamp.strftime("%H:%M today" if stamp.date() == datetime.now().date() else "%d %b %Y, %H:%M")


def plain_row(title: str, subtitle: str = "") -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_use_markup(False)
    return row


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Video Workflow")
        self.set_default_size(1120, 800)
        self.set_size_request(360, 480)
        self.projects: list[core.Project] = []
        self.project: core.Project | None = None
        self.voice_sources: list[Path] = []
        self.browsed_voice: Path | None = None
        self.file_rows: list[Gtk.Widget] = []
        self.expanded: set[str] = {"Footage"}
        self.busy = 0
        self._build()
        self.refresh_projects()
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh())

    # --- layout ----------------------------------------------------------------

    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=240, max_sidebar_width=300)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        breakpoint.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(breakpoint)

        self.split.set_sidebar(Adw.NavigationPage.new(self._build_sidebar(), "Projects"))
        self.content_page = Adw.NavigationPage.new(self._build_content(), "Project")
        self.split.set_content(self.content_page)

    def _build_sidebar(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        new_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New project (Ctrl+N)",
                                action_name="app.new-project")
        header.pack_start(new_button)
        menu = Gio.Menu()
        menu.append("Open projects folder", "app.open-root")
        menu.append("Preferences", "app.preferences")
        menu.append("About Video Workflow", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        view.add_top_bar(header)

        self.project_list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.project_list.connect("row-selected", self._on_project_selected)
        self.project_list.connect("row-activated", lambda *_: self.split.set_show_content(True))
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroller.set_child(self.project_list)

        empty = Adw.StatusPage(icon_name="folder-videos-symbolic", title="No projects",
                               description="Each video gets its own folder.")
        empty.add_css_class("compact")
        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.add_named(scroller, "list")
        self.sidebar_stack.add_named(empty, "empty")
        view.set_content(self.sidebar_stack)
        return view

    def _build_content(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Video Workflow")
        header.set_title_widget(self.window_title)
        self.spinner = Adw.Spinner(visible=False, tooltip_text="Working…")
        header.pack_start(self.spinner)
        self.folder_button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open project folder")
        self.folder_button.connect("clicked", lambda *_: self.project and apps.open_path(self.project.path))
        header.pack_end(self.folder_button)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh (F5)", action_name="win.refresh")
        header.pack_end(refresh)
        view.add_top_bar(header)

        self.banner = Adw.Banner(button_label="Move into this project")
        self.banner.connect("button-clicked", lambda *_: self._move_loose_recordings())
        view.add_top_bar(self.banner)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        view.add_controller(drop)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                       margin_top=24, margin_bottom=36, margin_start=16, margin_end=16)
        page.append(self._build_apps_group())
        page.append(self._build_resolve_group())
        page.append(self._build_voice_group())
        self.files_group = Adw.PreferencesGroup(
            title="Files", description="Drop files anywhere on this window to copy them in. They're sorted into folders by type.")
        add = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"],
                         child=Adw.ButtonContent(icon_name="list-add-symbolic", label="Add files…"))
        add.connect("clicked", lambda *_: self._choose_files())
        self.files_group.set_header_suffix(add)
        page.append(self.files_group)

        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=620, child=page)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp)

        empty = Adw.StatusPage(icon_name="folder-videos-symbolic", title="Start a video project",
                               description="A project is one folder holding everything for a video. "
                                           "OBS, Audacity and Resolve open already pointed at it.")
        create = Gtk.Button(label="New project", halign=Gtk.Align.CENTER,
                            css_classes=["pill", "suggested-action"], action_name="app.new-project")
        empty.set_child(create)

        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.add_named(scroller, "project")
        self.content_stack.add_named(empty, "empty")
        view.set_content(self.content_stack)
        return view

    def _build_apps_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Record and edit",
            description="Each app opens with its save folders already set to this project.")
        for icon, title, subtitle, callback in (
            ("com.obsproject.Studio", "OBS Studio", "Recordings save to Media/Footage", apps.open_obs),
            ("audacity", "Audacity", "Projects save to Audacity · exports save to Media/Voiceover/Raw", apps.open_audacity),
            ("davinci-resolve", "DaVinci Resolve", "Opens straight into the Resolve project with this video's name",
             apps.open_resolve),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=2)
            row.add_prefix(Gtk.Image(icon_name=icon, pixel_size=36))
            button = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda _b, fn=callback: self._open_app(fn))
            row.add_suffix(button)
            row.set_activatable_widget(button)
            group.add(row)
        return group

    def _build_resolve_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Import into DaVinci Resolve",
            description="Everything to edit with is in this project's Media folder, so it's one right-click.")
        copy = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat"],
                          tooltip_text="For File ▸ Import ▸ Media… (Ctrl+I): paste it into the file dialog",
                          child=Adw.ButtonContent(icon_name="edit-copy-symbolic", label="Copy Media path"))
        copy.connect("clicked", lambda *_: self._copy_media_path())
        group.set_header_suffix(copy)
        self.resolve_steps: list[Adw.ActionRow] = []
        for number in range(1, 4):
            row = Adw.ActionRow(subtitle_lines=3)
            row.set_use_markup(False)
            badge = Gtk.Label(label=str(number), valign=Gtk.Align.CENTER, width_chars=2,
                              css_classes=["numeric", "dim-label", "title-4"])
            row.add_prefix(badge)
            group.add(row)
            self.resolve_steps.append(row)
        return group

    def _update_resolve_steps(self, project: core.Project) -> None:
        for row, (title, subtitle) in zip(self.resolve_steps, (
            ("Open the Media page", "Click Media in the page bar along the bottom of Resolve."),
            (f"In Media Storage, open Projects ▸ {project.name}",
             "Media Storage is the file browser in the top left. Projects is your ~/Videos/Projects folder."),
            ("Right-click Media ▸ Add Folder and SubFolders into Media Pool (Create Bins)",
             "You get Footage, Voiceover, Music, Images and Other bins. For new recordings later, "
             "drag them from Media Storage into their bin."),
        )):
            row.set_title(title)
            row.set_subtitle(subtitle)

    def _copy_media_path(self) -> None:
        if not self.project:
            return
        path = str(self.project.path / core.MEDIA)
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(path.encode())))
        self.toast("Copied the Media folder path")

    def _build_voice_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(
            title="Polish voiceover",
            description="Removes rumble, softens harsh S and T sounds, evens out levels and sets the loudness. "
                        "Writes a new WAV to Media/Voiceover and keeps the raw export.")
        self.voice_model = Gtk.StringList()
        self.voice_row = Adw.ComboRow(title="Recording", model=self.voice_model)
        self.voice_row.connect("notify::selected", lambda *_: self._update_voice_subtitle())
        browse = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                            css_classes=["flat"], tooltip_text="Choose a file from elsewhere")
        browse.connect("clicked", lambda *_: self._choose_voice())
        self.voice_row.add_suffix(browse)
        group.add(self.voice_row)

        presets = list(core.DEESS_PRESETS)
        self.preset_row = Adw.ComboRow(title="Soften S and T", model=Gtk.StringList.new(presets),
                                       subtitle="Use Gentle if words lose clarity, Strong if they still hiss")
        self.preset_row.set_selected(presets.index("Standard"))
        group.add(self.preset_row)

        self.loudness_row = Adw.SpinRow.new_with_range(-24, -12, 1)
        self.loudness_row.set_title("Loudness (LUFS)")
        self.loudness_row.set_subtitle("−14 to −16 suits YouTube")
        self.loudness_row.set_value(-16)
        group.add(self.loudness_row)

        self.polish_row = Adw.ButtonRow(title="Create polished WAV", start_icon_name="object-select-symbolic")
        self.polish_row.add_css_class("suggested-action")
        self.polish_row.connect("activated", lambda *_: self._polish())
        group.add(self.polish_row)
        return group

    # --- state -----------------------------------------------------------------

    def toast(self, message: str, timeout: int = 5) -> None:
        toast = Adw.Toast(title=GLib.markup_escape_text(message), timeout=timeout)
        self.toasts.add_toast(toast)

    def refresh(self) -> None:
        self.refresh_projects()

    def refresh_projects(self, select: Path | None = None) -> None:
        select = select or (self.project.path if self.project else None)
        self.projects = core.list_projects()
        self.project_list.remove_all()
        for project in self.projects:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6)
            title = Gtk.Label(label=project.name, xalign=0, ellipsize=Pango.EllipsizeMode.END)
            subtitle = Gtk.Label(label=project.created.strftime("%d %b %Y"), xalign=0, css_classes=["dim-label", "caption"])
            box.append(title)
            box.append(subtitle)
            row.set_child(box)
            row.project = project
            self.project_list.append(row)
        self.sidebar_stack.set_visible_child_name("list" if self.projects else "empty")
        if not self.projects:
            self.show_project(None)
            return
        index = next((i for i, p in enumerate(self.projects) if p.path == select), None)
        if index is None:
            active = core.read_state().get("active_project")
            index = next((i for i, p in enumerate(self.projects) if str(p.path) == active), 0)
        self.project_list.select_row(self.project_list.get_row_at_index(index))

    def _on_project_selected(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is not None:
            self.show_project(row.project)

    def show_project(self, project: core.Project | None) -> None:
        changed = project != self.project
        self.project = project
        self.folder_button.set_sensitive(project is not None)
        if project is None:
            self.window_title.set_title("Video Workflow")
            self.window_title.set_subtitle("")
            self.content_page.set_title("Video Workflow")
            self.content_stack.set_visible_child_name("empty")
            self.banner.set_revealed(False)
            return
        project.ensure_layout()
        core.set_active_project(project)
        self._update_resolve_steps(project)
        if changed:
            self.browsed_voice = None
        self.window_title.set_title(project.name)
        self.window_title.set_subtitle(str(project.path).replace(str(core.HOME), "~", 1))
        self.content_page.set_title(project.name)
        self.content_stack.set_visible_child_name("project")
        self._refresh_voice_sources()
        self._refresh_files()
        self._refresh_banner()

    def _refresh_banner(self) -> None:
        loose = core.loose_recordings()
        if loose:
            count = len(loose)
            self.banner.set_title(f"{count} recording{'s' if count > 1 else ''} in ~/Videos {'are' if count > 1 else 'is'}n't in any project")
        self.banner.set_revealed(bool(loose))

    def _refresh_voice_sources(self) -> None:
        previous = self.voice_sources[self.voice_row.get_selected()] if self.voice_sources and \
            self.voice_row.get_selected() < len(self.voice_sources) else None
        sources = self.project.files("Voiceover Raw") if self.project else []
        sources = [p for p in sources if p.suffix.lower() in core.AUDIO_EXTS]
        if self.browsed_voice and self.browsed_voice not in sources:
            sources.insert(0, self.browsed_voice)
        self.voice_sources = sources
        labels = [p.name for p in sources] or ["None yet"]
        self.voice_model.splice(0, self.voice_model.get_n_items(), labels)
        self.polish_row.set_sensitive(bool(sources) and not self.busy)
        if previous in sources:
            self.voice_row.set_selected(sources.index(previous))
        self._update_voice_subtitle()

    def _update_voice_subtitle(self) -> None:
        index = self.voice_row.get_selected()
        if index < len(self.voice_sources):
            self.voice_row.set_subtitle(f"Exported {when(self.voice_sources[index])}")
        else:
            self.voice_row.set_subtitle("Export from Audacity and it shows up here")

    def _refresh_files(self) -> None:
        for row in self.file_rows:
            if isinstance(row, Adw.ExpanderRow) and row.get_expanded():
                self.expanded.add(row.key)
            elif isinstance(row, Adw.ExpanderRow):
                self.expanded.discard(row.key)
            self.files_group.remove(row)
        self.file_rows = []
        if not self.project:
            return
        for key, title, icon in FILE_SECTIONS:
            files = self.project.files(key)
            total = sum(p.stat().st_size for p in files)
            subtitle = f"{len(files)} file{'s' if len(files) != 1 else ''} · {core.human_size(total)}" if files else "Empty"
            row = Adw.ExpanderRow(title=title, subtitle=subtitle)
            row.key = key
            row.add_prefix(Gtk.Image(icon_name=icon))
            open_folder = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                     css_classes=["flat"], tooltip_text=f"Open {core.FOLDERS[key]}")
            open_folder.connect("clicked", lambda _b, k=key: apps.open_path(self.project.folder(k)))
            row.add_suffix(open_folder)
            row.set_enable_expansion(bool(files))
            row.set_expanded(bool(files) and key in self.expanded)
            for path in files[:MAX_FILE_ROWS]:
                item = plain_row(path.name, f"{core.human_size(path.stat().st_size)} · {when(path)}")
                item.set_activatable(True)
                item.add_suffix(Gtk.Image(icon_name="adw-external-link-symbolic", css_classes=["dim-label"]))
                item.connect("activated", lambda _r, p=path: apps.open_path(p))
                row.add_row(item)
            if len(files) > MAX_FILE_ROWS:
                row.add_row(plain_row(f"…and {len(files) - MAX_FILE_ROWS} more", "Open the folder to see everything"))
            self.files_group.add(row)
            self.file_rows.append(row)

    # --- background work -------------------------------------------------------------

    def run_job(self, work, done) -> None:
        self.busy += 1
        self.spinner.set_visible(True)
        self.polish_row.set_sensitive(False)

        def finish(result=None, error: Exception | None = None) -> bool:
            self.busy -= 1
            self.spinner.set_visible(self.busy > 0)
            if error:
                self.toast(str(error), timeout=8)
            else:
                done(result)
            self.show_project(self.project)
            return False

        def run() -> None:
            try:
                result = work()
            except Exception as exc:  # shown to the user
                GLib.idle_add(finish, None, exc)
            else:
                GLib.idle_add(finish, result, None)

        threading.Thread(target=run, daemon=True).start()

    # --- actions --------------------------------------------------------------------

    def _open_app(self, opener) -> None:
        if not self.project:
            return
        project = self.project
        self.run_job(lambda: opener(project), self._show_app_message)

    def _show_app_message(self, message: str) -> None:
        if len(message) <= 110:
            self.toast(message, timeout=8)
            return
        dialog = Adw.AlertDialog(heading="Next step", body=message)
        dialog.add_response("ok", "Got it")
        dialog.present(self)

    def _import(self, paths: list[Path], move: bool = False) -> None:
        if not self.project or not paths:
            return
        project = self.project

        def done(placed: list[Path]) -> None:
            verb = "Moved" if move else "Copied"
            self.toast(f"{verb} {len(placed)} file{'s' if len(placed) != 1 else ''} into {project.name}")
            for path in placed:
                self.expanded.add(core.folder_for(path))

        self.run_job(lambda: core.import_files(project, paths, move=move), done)

    def _on_drop(self, _target, value, _x, _y) -> bool:
        paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        if not self.project:
            self.toast("Create a project first.")
            return False
        self._import(paths)
        return True

    def _choose_files(self) -> None:
        dialog = Gtk.FileDialog(title="Add files to project", accept_label="Add")

        def chosen(dialog, result) -> None:
            try:
                files = dialog.open_multiple_finish(result)
            except GLib.Error:
                return
            self._import([Path(f.get_path()) for f in files if f.get_path()])

        dialog.open_multiple(self, None, chosen)

    def _choose_voice(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a voice recording", accept_label="Choose")
        audio = Gtk.FileFilter(name="Audio")
        for ext in sorted(core.AUDIO_EXTS):
            audio.add_suffix(ext[1:])
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(audio)
        dialog.set_filters(filters)
        if self.project:
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.project.folder("Voiceover Raw"))))

        def chosen(dialog, result) -> None:
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            self.browsed_voice = Path(file.get_path())
            self._refresh_voice_sources()
            self.voice_row.set_selected(self.voice_sources.index(self.browsed_voice))

        dialog.open(self, None, chosen)

    def _polish(self) -> None:
        index = self.voice_row.get_selected()
        if not self.project or index >= len(self.voice_sources):
            return
        project, source = self.project, self.voice_sources[index]
        preset = list(core.DEESS_PRESETS)[self.preset_row.get_selected()]
        loudness = int(self.loudness_row.get_value())
        self.toast(f"Polishing {source.name}…", timeout=3)

        def done(output: Path) -> None:
            self.expanded.add("Voiceover")
            self.toast(f"Saved {output.name} to Media/Voiceover")

        self.run_job(lambda: core.polish_voiceover(source, project, preset, loudness), done)

    def _move_loose_recordings(self) -> None:
        loose = core.loose_recordings()
        if not loose or not self.project:
            return
        dialog = Adw.AlertDialog(
            heading=f"Move {len(loose)} recording{'s' if len(loose) > 1 else ''} into “{self.project.name}”?",
            body="They'll be moved from ~/Videos into this project's Footage folder:\n\n"
                 + "\n".join(p.name for p in loose[:8]) + ("\n…" if len(loose) > 8 else ""))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("move", "Move")
        dialog.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("move")
        dialog.connect("response", lambda _d, r: r == "move" and self._import(loose, move=True))
        dialog.present(self)

    def new_project(self) -> None:
        dialog = Adw.AlertDialog(heading="New video project",
                                 body=f"Creates a folder in {str(core.PROJECTS).replace(str(core.HOME), '~', 1)} "
                                      "with everything OBS, Audacity and Resolve need.")
        entry = Gtk.Entry(placeholder_text="Video title", activates_default=True)
        error = Gtk.Label(xalign=0, wrap=True, css_classes=["error", "caption"], visible=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(entry)
        box.append(error)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("create", False)

        def validate(*_) -> None:
            name = entry.get_text()
            message = ""
            try:
                cleaned = core.clean_name(name)
                if (core.PROJECTS / cleaned).exists():
                    message = f"“{cleaned}” already exists."
            except ValueError as exc:
                message = str(exc) if name.strip() else ""
            error.set_label(message)
            error.set_visible(bool(message))
            dialog.set_response_enabled("create", bool(name.strip()) and not message)

        def create(*_) -> None:
            try:
                project = core.create_project(entry.get_text())
            except (ValueError, OSError) as exc:
                self.toast(str(exc))
                return
            self.refresh_projects(select=project.path)
            self.split.set_show_content(True)
            self.toast(f"Created {project.name}")

        def activate(*_) -> None:
            if dialog.get_response_enabled("create"):
                dialog.force_close()
                create()

        entry.connect("changed", validate)
        entry.connect("activate", activate)
        dialog.connect("response", lambda _d, r: r == "create" and create())
        dialog.present(self)
        entry.grab_focus()

    def preferences(self) -> None:
        dialog = Adw.PreferencesDialog(title="Preferences")
        page = Adw.PreferencesPage()
        dialog.add(page)

        mic = Adw.PreferencesGroup(title="USB microphone",
                                   description="Input boost for the noise-reduced USB mic. Keep it modest; "
                                               "Polish voiceover sets the final loudness.")
        gain = Adw.SpinRow.new_with_range(1.0, 1.8, 0.1)
        gain.set_title("Recording gain")
        gain.set_digits(1)
        gain.set_value(apps.mic_gain() or 1.4)
        mic.add(gain)
        apply = Adw.ButtonRow(title="Apply gain")

        def apply_gain(*_) -> None:
            try:
                apps.set_mic_gain(round(gain.get_value(), 1))
                dialog.add_toast(Adw.Toast(title=f"Mic gain set to {gain.get_value():.1f}×"))
            except Exception as exc:
                dialog.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(exc))))

        apply.connect("activated", apply_gain)
        mic.add(apply)
        page.add(mic)

        resolve = Adw.PreferencesGroup(title="DaVinci Resolve")
        resolve.add(plain_row("Projects folder in Media Storage",
                              "Set" if apps.resolve_storage_configured()
                              else "Added the next time you open Resolve from here while it's closed."))
        resolve.add(plain_row("Resolve's cache and gallery stills", str(apps.RESOLVE_SCRATCH)))
        page.add(resolve)

        folders = Adw.PreferencesGroup(title="Folders")
        root = plain_row("Projects", str(core.PROJECTS))
        button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"])
        button.connect("clicked", lambda *_: apps.open_path(core.PROJECTS))
        root.add_suffix(button)
        folders.add(root)
        page.add(folders)
        dialog.present(self)


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        core.PROJECTS.mkdir(parents=True, exist_ok=True)
        for name, callback, accels in (
            ("new-project", lambda: self.window.new_project(), ["<Control>n"]),
            ("preferences", lambda: self.window.preferences(), ["<Control>comma"]),
            ("open-root", lambda: apps.open_path(core.PROJECTS), []),
            ("about", self._about, []),
            ("quit", self.quit, ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a, cb=callback: cb())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.refresh", ["F5", "<Control>r"])
        threading.Thread(target=self._prepare_resolve, daemon=True).start()

    @staticmethod
    def _prepare_resolve() -> None:
        try:
            apps.prepare_resolve()
        except OSError:
            pass  # retried when Resolve is opened from the app

    def do_activate(self) -> None:
        window = self.props.active_window
        if not window:
            window = Window(self)
            refresh = Gio.SimpleAction.new("refresh", None)
            refresh.connect("activate", lambda *_: window.refresh())
            window.add_action(refresh)
        self.window = window
        window.present()

    def _about(self) -> None:
        about = Adw.AboutDialog(application_name="Video Workflow", application_icon=APP_ID,
                                developer_name="descentkatil", version="2.0",
                                comments="One folder per video, with OBS, Audacity and DaVinci Resolve pointed at it.")
        about.present(self.window)


if __name__ == "__main__":
    sys.exit(Application().run(sys.argv))
