#!/usr/bin/env python3
"""niri-window-watcher — enforce DMS window rules on windows that set their
title/app-id *after* they map (e.g. Zen/Firefox PiP, Bitwarden popups).

niri evaluates open-* / default-* window rules once, at map time, so windows
that only set their title afterward never match. This daemon watches niri's
IPC event stream and re-applies the equivalent action over IPC when a window
*changes* into a matching state.

Rules are NOT defined here. They come from your DMS window rules
(`dms config windowrules list`), so you author them in the DMS Settings GUI.
Any rule whose *name* contains the marker (default "[live]", see
watcher-settings.json) is enforced dynamically. niri's dynamic properties
(opacity, corner radius, borders, min/max size) are left to niri, which
already re-applies those on title change — only the one-shot open-* / default-*
family needs this daemon. openFullscreen/openMaximized are NOT enforced: niri
only exposes toggles for those and doesn't report the current state, so
re-applying them would flip already-correct windows.

Rules only fire for windows first seen within `match_window_seconds` (default
5, see watcher-settings.json). Late-titling windows (PiP, extension popouts)
settle within a second of mapping; a long-lived window whose title later
changes to match (e.g. the main browser window opening an extension *tab*,
which carries the same "Extension: ..." title as a popout) is left alone.
Windows already open when the daemon (re)connects are never touched.

Stdlib only. Talks to niri via `niri msg` and to DMS via `dms config`.

Usage:
    niri-window-watcher.py [--config PATH]
    niri-window-watcher.py --discover    # print every window change, apply nothing
    niri-window-watcher.py --dump-rules  # print the resolved (marked) rules and exit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "watcher-settings.json")


def log(msg: str) -> None:
    print(msg, flush=True)          # stdout -> journald under systemd


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


# --------------------------------------------------------------------------- #
# DMS action -> niri IPC translation
#
# Only the "one-shot" family that niri applies at open time (and therefore
# misses on late windows) is translated. Everything else is niri's job.
# --------------------------------------------------------------------------- #
def _size_change(value) -> str | None:
    """DMS width/height value ("proportion 0.5" | "fixed 800" | number) -> niri CHANGE."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.match(r"proportion\s+([0-9.]+)", s)
    if m:
        return f"{float(m.group(1)) * 100:g}%"
    m = re.match(r"fixed\s+([0-9]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[0-9.]+%?", s):
        return s
    return None


def translate_actions(actions: dict, rule_name: str) -> list[list[str]]:
    """Return an ordered list of `niri msg action ...` argv templates (without --id/wid)."""
    out: list[list[str]] = []

    ws = actions.get("openOnWorkspace")
    if ws:
        out.append(["move-window-to-workspace", str(ws), "--focus", "false", "--window-id"])

    if "openFloating" in actions:
        out.append(["move-window-to-floating" if actions["openFloating"] else "move-window-to-tiling", "--id"])

    w = _size_change(actions.get("defaultColumnWidth"))
    if w:
        out.append(["set-window-width", w, "--id"])

    h = _size_change(actions.get("defaultWindowHeight"))
    if h:
        out.append(["set-window-height", h, "--id"])

    if actions.get("openFocused"):
        out.append(["focus-window", "--id"])

    for key, action in (("openFullscreen", "fullscreen-window"),
                        ("openMaximized", "maximize-column")):
        if actions.get(key):
            err(f"rule {rule_name!r}: {key} can't be enforced over IPC — niri's "
                f"`{action}` is a toggle and window state isn't reported, so "
                f"re-applying it would un-{key[4:].lower()} windows niri already "
                f"handled at map time. Skipping that action.")

    return out


class Rule:
    def __init__(self, name, app_id, title, action_templates):
        self.name = name
        self.app_re = re.compile(app_id) if app_id else None
        self.title_re = re.compile(title) if title else None
        self.action_templates = action_templates  # list[list[str]]
        # Desired floating state if the rule sets one (None = don't care).
        self.wants_floating = None
        for t in action_templates:
            if t[0] == "move-window-to-floating":
                self.wants_floating = True
            elif t[0] == "move-window-to-tiling":
                self.wants_floating = False

    def matches(self, app_id: str, title: str) -> bool:
        if self.app_re is None and self.title_re is None:
            return False
        if self.app_re is not None and not self.app_re.search(app_id):
            return False
        if self.title_re is not None and not self.title_re.search(title):
            return False
        return True

    def commands(self, wid: int) -> list[list[str]]:
        cmds = []
        for tmpl in self.action_templates:
            # Trailing flag (--id / --window-id) takes the window id as its value.
            cmds.append(["niri", "msg", "action"] + tmpl + [str(wid)])
        return cmds


# --------------------------------------------------------------------------- #
# Config: settings file + DMS rules
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self, path: str):
        self.path = path
        self.settings_mtime = 0.0
        self.dms_mtime = 0.0
        self.marker = "[live]"
        self.compositor = "niri"
        self.log_matches = True
        self.dms_watch_file = "~/.config/niri/dms/windowrules.kdl"
        self.match_window_seconds = 5.0
        self.rules: list[Rule] = []
        self._dms_retry_at = 0.0     # >0: last DMS listing failed, retry then
        self.reload(force=True)

    # -- settings.json -----------------------------------------------------
    def _load_settings(self) -> dict | None:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            err(f"settings: failed to load {self.path}: {e} (keeping previous)")
            return None

    # -- DMS rules ---------------------------------------------------------
    def _load_dms_rules(self) -> list[Rule] | None:
        """Marked DMS rules, or None if DMS couldn't be queried (caller keeps the old set)."""
        try:
            res = subprocess.run(
                ["dms", "config", "windowrules", "list", self.compositor],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            err(f"dms: cannot list window rules: {e}")
            return None
        if res.returncode != 0:
            err(f"dms: list failed: {res.stderr.strip()}")
            return None
        raw = res.stdout
        brace = raw.find("{")               # tolerate any banner/prefix
        if brace < 0:
            err("dms: no JSON in output")
            return None
        try:
            data = json.loads(raw[brace:])
        except json.JSONDecodeError as e:
            err(f"dms: bad JSON: {e}")
            return None

        rules: list[Rule] = []
        for r in data.get("rules", []):
            name = r.get("name") or ""
            if not r.get("enabled", True):
                continue
            if self.marker.lower() not in name.lower():   # tolerate [Live] vs [live]
                continue
            mc = r.get("matchCriteria", {}) or {}
            app_id = mc.get("appId")
            title = mc.get("title")
            if not app_id and not title:
                err(f"rule {name!r}: marked but has no appId/title match "
                    f"(dynamic-only criteria can't be enforced one-shot) — skipping.")
                continue
            templates = translate_actions(r.get("actions", {}) or {}, name)
            if not templates:
                err(f"rule {name!r}: marked but has no one-shot (open-*/default-*) "
                    f"action to enforce — niri already handles its dynamic properties.")
                continue
            try:
                rules.append(Rule(name, app_id, title, templates))
            except re.error as e:
                err(f"rule {name!r}: bad regex ({e}) — skipping.")
        return rules

    # -- extra_rules from settings ----------------------------------------
    def _load_extra_rules(self, settings: dict) -> list[Rule]:
        rules = []
        for spec in settings.get("extra_rules", []):
            name = spec.get("name", "<extra>")
            match = spec.get("match", {})
            templates = []
            for a in spec.get("actions", []):
                action = a.get("action")
                if not action:
                    continue
                args = a.get("args", {})
                if action == "move-window-to-workspace":
                    templates.append([action, str(args.get("workspace", "")),
                                      "--focus", str(args.get("focus", False)).lower(), "--window-id"])
                elif action in ("set-window-width", "set-window-height"):
                    templates.append([action, str(args.get("change", "")), "--id"])
                else:
                    templates.append([action, "--id"])
            try:
                rules.append(Rule(name, match.get("app_id"), match.get("title"), templates))
            except re.error as e:
                err(f"extra rule {name!r}: bad regex ({e}) — skipping.")
        return rules

    # -- reload ------------------------------------------------------------
    def reload(self, force: bool = False) -> bool:
        try:
            s_mtime = os.path.getmtime(self.path)
        except OSError:
            s_mtime = self.settings_mtime
        dms_file = expand(self.dms_watch_file)
        try:
            d_mtime = os.path.getmtime(dms_file)
        except OSError:
            d_mtime = self.dms_mtime

        retry_due = self._dms_retry_at and time.time() >= self._dms_retry_at
        if not force and not retry_due \
                and s_mtime == self.settings_mtime and d_mtime == self.dms_mtime:
            return False

        settings = self._load_settings()
        if settings is None and not force:
            return False
        settings = settings or {}

        self.marker = settings.get("marker", self.marker)
        self.compositor = settings.get("compositor", self.compositor)
        self.log_matches = settings.get("log_matches", self.log_matches)
        self.dms_watch_file = settings.get("dms_watch_file", self.dms_watch_file)
        self.match_window_seconds = float(settings.get("match_window_seconds",
                                                       self.match_window_seconds))

        dms_rules = self._load_dms_rules()
        self.settings_mtime = s_mtime
        self.dms_mtime = os.path.getmtime(expand(self.dms_watch_file)) \
            if os.path.exists(expand(self.dms_watch_file)) else d_mtime
        if dms_rules is None:
            # DMS/niri mid-rewrite or a transient parse error: keep what we had
            # (a fresh start has nothing, so the extra_rules still load) and
            # retry shortly instead of silently running with zero rules.
            self._dms_retry_at = time.time() + 10
            if self.rules:
                err(f"config: keeping previous {len(self.rules)} rule(s); retrying DMS in 10s")
                return False
            self.rules = self._load_extra_rules(settings)
            err("config: no DMS rules available yet; retrying in 10s")
            return True
        self._dms_retry_at = 0.0
        self.rules = dms_rules + self._load_extra_rules(settings)
        log(f"config: {len(self.rules)} active rule(s) (marker {self.marker!r})")
        for r in self.rules:
            log(f"  - {r.name!r}  app_id={r.app_re.pattern if r.app_re else None!r} "
                f"title={r.title_re.pattern if r.title_re else None!r}  "
                f"actions={[t[0] for t in r.action_templates]}")
        return True


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
def apply_rule(rule: Rule, wid: int) -> None:
    for cmd in rule.commands(wid):
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            err(f"  action failed: {' '.join(cmd)} -> {res.stderr.strip()}")


def handle_window(w: dict, cfg: Config, handled: set, first_seen: dict,
                  discover: bool) -> None:
    """React to one WindowOpenedOrChanged event.

    handled:    window ids already acted on (or deliberately left alone)
    first_seen: window id -> monotonic time of the first event we saw for it
    """
    wid = w.get("id")
    if wid is None:
        return
    app_id = w.get("app_id") or ""
    title = w.get("title") or ""
    now = time.monotonic()
    first_seen.setdefault(wid, now)

    if discover:
        log(f"window id={wid:<4} floating={int(bool(w.get('is_floating')))} "
            f"age={now - first_seen[wid]:.1f}s app_id={app_id!r} title={title!r}")
        return

    if wid in handled:
        return
    for rule in cfg.rules:
        if not rule.matches(app_id, title):
            continue
        age = now - first_seen[wid]
        if age > cfg.match_window_seconds:
            # Long-lived window that only now matches (e.g. main browser window
            # showing an extension tab). Not a late-titling popup: leave it.
            if cfg.log_matches:
                log(f"skip  id={wid} rule={rule.name!r} title={title!r}: "
                    f"window is {age:.0f}s old (> match_window_seconds)")
            handled.add(wid)
            break
        if rule.wants_floating is not None and bool(w.get("is_floating")) == rule.wants_floating:
            # niri already got it right at map time; nothing to enforce.
            handled.add(wid)
            break
        if cfg.log_matches:
            log(f"match id={wid} rule={rule.name!r} app_id={app_id!r} title={title!r}")
        apply_rule(rule, wid)
        handled.add(wid)
        break


# --------------------------------------------------------------------------- #
# Event loop
# --------------------------------------------------------------------------- #
def stream_once(cfg: Config, handled: set, first_seen: dict, discover: bool) -> None:
    proc = subprocess.Popen(
        ["niri", "msg", "--json", "event-stream"],
        stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not discover:
                cfg.reload()                # cheap: mtime check on settings + dms file
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "WindowOpenedOrChanged" in ev:
                handle_window(ev["WindowOpenedOrChanged"]["window"], cfg, handled,
                              first_seen, discover)
            elif "WindowsChanged" in ev:
                # Initial snapshot on (re)connect. These windows were mapped
                # before we were listening, so niri's own rules have had their
                # chance and the user may have rearranged them since: treat
                # them all as handled rather than re-applying anything.
                windows = ev["WindowsChanged"]["windows"]
                ids = {w.get("id") for w in windows}
                handled.intersection_update(ids)
                for k in list(first_seen):
                    if k not in ids:
                        del first_seen[k]
                for w in windows:
                    if discover:
                        handle_window(w, cfg, handled, first_seen, discover)
                    else:
                        handled.add(w.get("id"))
                        first_seen.setdefault(w.get("id"), 0.0)
            elif "WindowClosed" in ev:
                wid = ev["WindowClosed"].get("id")
                handled.discard(wid)
                first_seen.pop(wid, None)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--discover", action="store_true",
                    help="print every window change and apply nothing")
    ap.add_argument("--dump-rules", action="store_true",
                    help="print the resolved marked rules and exit")
    args = ap.parse_args()

    cfg = Config(args.config)

    if args.dump_rules:
        return 0

    handled: set = set()
    first_seen: dict = {}
    if args.discover:
        log("discover mode: trigger the windows you want to match "
            "(PiP, Bitwarden, dialogs) and note their app_id/title. Ctrl-C to stop.")

    while True:
        try:
            stream_once(cfg, handled, first_seen, args.discover)
        except FileNotFoundError:
            err("niri not found on PATH; retrying in 2s")
        except Exception as e:  # noqa: BLE001 - never die on a stray event
            err(f"stream error: {e!r}; reconnecting in 2s")
        else:
            err("event stream closed; reconnecting in 2s")
        # Keep `handled`: the reconnect snapshot re-seeds it from live windows.
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
