# niri-window-watcher

Enforce niri window rules on windows that set their title or app-id *after* they open.

This is aimed at [DankMaterialShell](https://github.com/AvengeMedia/DankMaterialShell) (DMS) users: you author rules in the DMS Window Rules GUI as usual, tag the ones you want enforced, and this small daemon applies them over niri IPC when the window is ready. It works fine without DMS too, using its own config.

## The problem

niri evaluates `open-*` and `default-*` window rules exactly once, at the moment a window maps. Some applications open a window with a placeholder title or app-id and only set the real values a fraction of a second later. By then niri has already decided, so the rule never fires.

Common offenders:

- Zen / Firefox Picture-in-Picture. The window opens as `app_id=zen` with no useful title, then becomes `Picture-in-Picture`.
- Firefox extension popouts (Bitwarden and friends). They open as `app_id=zen`, then the title becomes `Extension: (Bitwarden Password Manager) ...`.
- Various Godot, Bitwarden desktop, and Electron windows that name themselves late.

Because every one of these keeps a generic app-id and only distinguishes itself by title, and the title arrives late, a static `window-rule { match app-id=... title=... open-floating true }` in niri simply does not match at open time.

This is a known limitation. The niri maintainer's position is that this belongs outside the compositor, and niri deliberately exposes an IPC event stream plus an action IPC so external tools can handle it. See the upstream discussion: https://github.com/niri-wm/niri/discussions/1599

## Why this approach

niri already re-applies its *dynamic* rule properties whenever a window's title or app-id changes. That includes opacity, corner radius, borders, block-out, and the min and max size constraints. All of those keep working on their own, even for windows that name themselves late, so this tool does not need to touch them and does not. The only properties that suffer the late-title race are the one-shot `open-*` and `default-*` family, and those are all this daemon handles.

So this daemon does exactly one thing: it listens to `niri msg event-stream`, and when a window *changes* into a state that matches one of your tagged rules, it runs the equivalent `niri msg action` command targeted at that window by id. Each window is acted on once. If you later move it yourself, it stays where you put it.

Rules are not defined in this tool. It reads them from `dms config windowrules list`, which is the same data the DMS GUI writes, so the GUI stays the single source of truth. You opt a rule in by putting a marker (default `[live]`, case-insensitive) anywhere in the rule name.

## Requirements

- [niri](https://github.com/YaLTeR/niri) with `niri msg` IPC (any recent release).
- Python 3.9 or newer. Standard library only, no pip install.
- Optional but recommended: [DankMaterialShell](https://github.com/AvengeMedia/DankMaterialShell) with the `dms` CLI on your PATH, for GUI-authored rules. Without it, use the `extra_rules` section of the settings file.

## How it works

1. You create a normal window rule in the DMS Window Rules GUI: match on app-id and/or title, set an action such as open-floating.
2. You give the rule a name that contains the marker, for example `[live] Zen Picture-in-Picture`.
3. The daemon calls `dms config windowrules list niri`, keeps every enabled rule whose name contains the marker, and translates its one-shot actions into niri IPC calls.
4. It watches the event stream. When a window matches a tagged rule, it applies the actions to that window by id.
5. It hot-reloads when your DMS rules file or the settings file changes, so editing rules in the GUI takes effect without a restart.

### Action translation

Only the one-shot family is translated. Everything else is left to niri, which already handles it on title change.

| DMS action           | niri IPC action                       |
| -------------------- | ------------------------------------- |
| `openFloating` true  | `move-window-to-floating --id`        |
| `openFloating` false | `move-window-to-tiling --id`          |
| `defaultColumnWidth` | `set-window-width --id`               |
| `defaultWindowHeight`| `set-window-height --id`              |
| `openOnWorkspace`    | `move-window-to-workspace --window-id`|
| `openFullscreen`     | `fullscreen-window --id`              |
| `openFocused`        | `focus-window --id`                   |

`openMaximized` is skipped with a warning, because niri's `maximize-column` acts only on the focused window and cannot be applied by id without stealing focus. Use `openFullscreen` if you want that enforced.

Dynamic properties (opacity, corner radius, borders, min and max size, block-out) already work on their own, including on late-titled windows, because niri re-applies them whenever a window's title or app-id changes. This daemon deliberately leaves them to niri so it does not fight the compositor. If you rely on min or max size rules, they will keep working; you do not need this tool for them.

## Install

Clone the repo and place the script and its settings together. The systemd unit expects them in `~/.config/niri/scripts/`.

```sh
git clone git@github.com:dwright134/niri-window-watcher.git
cd niri-window-watcher

mkdir -p ~/.config/niri/scripts
install -m 755 niri-window-watcher.py ~/.config/niri/scripts/
# Only copy the settings file if you do not already have one, so you do not clobber it.
cp -n watcher-settings.json ~/.config/niri/scripts/

mkdir -p ~/.config/systemd/user
install -m 644 niri-window-watcher.service ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable --now niri-window-watcher.service
```

The unit is bound to `graphical-session.target`, so it starts on login and stops on logout, and it restarts automatically if it ever crashes.

If your niri session does not reach `graphical-session.target`, start the daemon from your niri config instead:

```kdl
spawn-at-startup "systemctl" "--user" "start" "niri-window-watcher.service"
```

## Use

### Create a rule

In DMS, open Settings and then Window Rules. Add a rule the normal way, and include the marker in its name.

For Zen and Firefox, match on **title**, not app-id. Every Zen window reports `app_id=zen`, so the app-id alone can never distinguish a PiP or an extension popout. The distinguishing information is in the title, which is exactly what arrives late.

Examples:

- Picture-in-Picture: name `[live] Zen PiP`, match app-id `^zen$` and title `^Picture-in-Picture$`, action open-floating.
- Extension popouts: name `[live] Zen Extensions`, match app-id `^zen$` and title `^Extension:`, action open-floating.

### Find the real title of a window

If you are not sure what title or app-id a window ends up with, run discover mode. It prints every window change and applies nothing.

```sh
~/.config/niri/scripts/niri-window-watcher.py --discover
```

Trigger the window you care about, read its app-id and title from the output, and write your rule to match them.

### See which rules are active

```sh
~/.config/niri/scripts/niri-window-watcher.py --dump-rules
```

This prints the tagged rules the daemon resolved and how each one was translated. If a rule you expected is missing, it was not tagged, was disabled, or had no one-shot action to enforce.

### Watch it work

```sh
journalctl --user -u niri-window-watcher.service -f
```

You will see a `match` line each time a rule fires, and a failure line if an action did not apply.

## Configuration

`watcher-settings.json` lives next to the script. It holds settings only, not rules.

```json
{
  "marker": "[live]",
  "compositor": "niri",
  "log_matches": true,
  "dms_watch_file": "~/.config/niri/dms/windowrules.kdl",
  "extra_rules": []
}
```

- `marker`: the substring that opts a DMS rule in. Matched case-insensitively.
- `compositor`: passed to `dms config windowrules list`.
- `log_matches`: log a line whenever a rule fires.
- `dms_watch_file`: the file whose modification time triggers a rules reload.
- `extra_rules`: an escape hatch for windows you cannot express in DMS, or if you do not use DMS at all. Each entry is a match plus a list of actions:

```json
{
  "extra_rules": [
    {
      "name": "PiP floating and centered",
      "match": { "app_id": "^zen$", "title": "^Picture-in-Picture$" },
      "actions": [
        { "action": "move-window-to-floating" },
        { "action": "set-window-width",  "args": { "change": "480" } },
        { "action": "set-window-height", "args": { "change": "270" } },
        { "action": "center-window" }
      ]
    }
  ]
}
```

`match.app_id` and `match.title` are Python regular expressions, matched with `re.search`. Actions map directly to `niri msg action <action> [args]`.

## Troubleshooting

- **A rule does nothing and is missing from `--dump-rules`.** It is not tagged with the marker, or it is disabled. Check the exact name in the GUI.
- **A rule is listed but never fires.** Your match is wrong. Use `--discover` to read the window's real app-id and title. For Zen this is almost always because you matched on app-id instead of title.
- **The window floats a moment after opening rather than instantly.** That is expected. The daemon can only act once the window announces its real title, which is the whole reason it exists.
- **Nothing runs at all.** Check `systemctl --user status niri-window-watcher.service` and the journal. Confirm `niri msg` works from your shell.

## License

MIT. See [LICENSE](LICENSE).
