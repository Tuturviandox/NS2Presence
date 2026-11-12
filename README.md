# NS2 Presence

Discord Rich Presence for the Switch 2 emulators in the `emus` folder.

The presence shows:

- the **running game** as the title (no more "Eden");
- the **real game icon**, extracted from your `.nup` files;
- the **emulator in use** and its version.

## How it works

`NS2Presence.exe` is a small **external** program: it never modifies any emulator
file. It runs in the background, in the notification area (next to the clock),
and:

1. lists the running processes and keeps only the emulator that is actually
   open, using the path of its process to find its install folder;
2. reads the emulator log (`user\log\eden_log.txt`) to find the running game and
   its Title ID;
3. sends the presence to Discord through its local channel (no password, no
   token, no account connection);
4. disables the emulators' **built-in** Discord presence to avoid two presences
   fighting each other.

### Automatic emulator detection

No emulator list to fill in: the program follows the **process that is
running**. It reads the path of the process (`QueryFullProcessImageNameW`), so
the install directory is known directly and only the emulator you actually
launched is watched; opening another one switches the presence to it.

The main executable is recognised either by its well-known name (`eden.exe`,
`lichii.exe`, `eglantine.exe`, `yuzu.exe`, ...) or, inside `emus`, by its
installation files (`qt.conf`, `emu-manager.json`, `Qt6Core.dll`,
`Qt6Gui.dll`). Helper tools shipped with the emulator (`eden-cli.exe`,
`*-updater.exe`, `vc_redist.x64.exe`, `tests.exe`, etc.) are ignored.

As a result, a **new folder**, a renamed fork or an executable that changes name
during an update is detected automatically. Each emulator opening and closing is
written to `presence.log`.

Backup folders (`.update-backup`, `update-old`) and technical subfolders
(`plugins`, `platforms`, `user`, etc.) are ignored.

When several emulators are open at once, the one running a game wins (then the
one with the most recent log); an emulator left in its menu is only shown when
no game is running anywhere.

## Usage

1. Start **Discord**.
2. Run `NS2Presence.exe` (a shortcut is available on the Desktop).
3. The program appears in the notification area. Right-click its icon:
   - **Status**: what it currently sees;
   - **Open interface**: game library and `.nup` folder;
   - **Start with Windows**: enables/disables auto-start;
   - **Reconnect Discord**: use it if Discord was started after the program;
   - **Discord diagnostics**: sends a test presence and reports whether the
     connection works;
   - **Quit**.

Only one copy can run at a time. If you start it again, the second one exits by
itself. To open only the window while the program is already running:
`NS2Presence.exe --gui`.

## Interface and games folder

The **Game library** window lets you choose the folder that contains your games
(including subfolders):

1. click **Browse…** and select the folder (for example
   `Desktop\switch 2\jeux`);
2. click **Scan**;
3. the program reads every `.nup` file, extracts the **name**, the **Title ID**,
   the **version** and the **real 256×256 icon**, then lists them.

The folder is saved in `config.json` (`games_dir`) and scanned again
automatically when the program starts.

### How the `.nup` file is read

A `.nup` contains a small metadata block per package (base game, update, DLC).
The program only reads those blocks (a few kilobytes), never the 9–25 GB of the
file: the scan is therefore almost instant.

- The base package may have a Title ID of zero (this is the case for *Pokemon
  Pokopia*): the program then uses the update package's Title ID.
- Icons are saved in `cache\nup\TITLE_ID.jpg`.
- The name found in the `.nup` replaces the one from the emulator log.

### Icon hosting (catbox.moe)

Discord refuses local files: it can only display images reachable through a
public URL. For the icon extracted from the `.nup` to appear in your profile,
the program uploads it to **catbox.moe** (a public image hosting service), then
stores the URL in `cache\nup_games.json`.

- Uploading is **optional**: the *Upload extracted icons to catbox.moe* checkbox
  can be cleared (`upload_icons` in `config.json`).
- Only game artwork extracted from the `.nup` files is uploaded.
- If the upload is refused or unavailable, the program falls back to the usual
  public icons (see below).
- A given game is uploaded only once: the URL is reused on later launches.

## Icons

The icon is chosen in this order:

1. from `icons.json` (your choice, highest priority);
2. the real icon extracted from your `.nup` (hosted on catbox.moe);
3. from `switchiconshowdown.com` (search by name);
4. from `tinfoil.media` (by Title ID);
5. from the Eden box art repository;
6. otherwise, the emulator logo is used.

### Add or fix an icon

Open `icons.json` and add a `TITLE_ID` → image URL entry. The URL must be
**public** (Discord downloads it itself) and point to a square PNG/JPG, for
example 256×256.

```json
{
  "0400C3F00006E000": "https://example.com/mario-kart-world.png"
}
```

The Title ID is visible in the emulator log, on the
`Loading <name> (<TITLE_ID>)` line, or in the diagnostics. Restart the program
after editing the file.

## Settings (`config.json`)

The file is created on first launch. Useful values:

| Key | Purpose |
| --- | --- |
| `client_id` | Discord application ID used for the presence. |
| `poll_seconds` | Check frequency (3 s by default). |
| `details_playing` / `state_playing` | Text shown while playing. `{game}` and `{emulator}` are replaced. |
| `details_idle` / `state_idle` | Text shown when the emulator is open but no game is running. |
| `idle_presence` | `false` to show nothing until a game is launched. |
| `show_version` | Adds the emulator version to the displayed name. |
| `show_timer` | Shows the elapsed play time. |
| `debug` | Writes more details to `presence.log`. |
| `ignore` | List of `.exe` paths not to watch. |
| `emulators` | Renames an emulator or changes its logo (see below). |
| `games_dir` | Folder scanned for `.nup` files (chosen in the interface). |
| `upload_icons` | Uploads `.nup` icons to catbox.moe (checkbox in the interface). |

Example to rename an emulator or force its logo:

```json
{
  "emulators": {
    "PokeDinner (fork)": {
      "name": "PokeDinner",
      "logo": "https://example.com/pokedinner.png"
    }
  }
}
```

The key is the **folder name** inside `emus` (or the full path / executable name),
matched case-insensitively.

## Name shown in Discord

The card title is the **game name**: the program fills the `name` field of the
Discord activity. Eden's public application is only used as a technical
container (identifier), and its name "Eden" no longer appears.

To use your own Discord application instead:

1. create an application at <https://discord.com/developers/applications>;
2. copy its **Application ID**;
3. replace `client_id` in `config.json` with that ID;
4. restart the program.

No Discord asset has to be uploaded: images are provided by URL. The icons
extracted from `.nup` files are hosted on catbox.moe (see above).

## Troubleshooting

- **Nothing appears**: Discord must be running. Open the notification area menu
  → **Discord diagnostics**.
- **The presence stays on the previous game**: click **Reconnect Discord**, or
  quit and restart the program.
- **An emulator is not detected**: the emulator must be running. A well-known
  executable name (`eden.exe`, `lichii.exe`, ...) is accepted anywhere; an
  unknown fork must be inside `emus`, in a folder that contains `qt.conf`,
  `emu-manager.json`, `Qt6Core.dll` or `Qt6Gui.dll`, and must not be a helper
  tool. Details are in `presence.log`.
- **The icon is generic**: open the interface, choose the `.nup` folder and
  click **Scan** to fetch the real icon. If the game is not in a `.nup`, add a
  URL in `icons.json`.
- **The icon does not change right away**: Discord keeps a cache. Restart
  Discord (or wait a few minutes) after a scan.

## Files

| File | Purpose |
| --- | --- |
| `NS2Presence.exe` | The program (no installation needed). |
| `config.json` | Settings, including the games folder. |
| `icons.json` | Custom icons (highest priority). |
| `presence.log` | Runtime log. |
| `cache\` | Game names and icons already found (safe to delete). |
| `cache\nup\` | Icons extracted from `.nup` files. |
| `cache\nup_games.json` | Names, versions and URLs of scanned `.nup` games. |

## Credits

See [CONTRIBUTORS.md](CONTRIBUTORS.md).

## Uninstall

Right-click the icon → clear **Start with Windows**, then **Quit**.
Then delete the `Presence` folder. The emulators were not modified.
