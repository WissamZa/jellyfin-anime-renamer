"""
picker — Interactive terminal picker with fzf-like navigation.

Supports:
  - Arrow keys (↑/↓) to navigate
  - j/k (vim-style) to navigate
  - Number keys to jump directly to an option
  - Enter to confirm
  - q / Esc to cancel

Falls back to simple ``input()`` when stdin is not a TTY (piped mode,
IDE terminals, etc.).
"""

import os
import re
import shutil
import sys
from typing import Any, List, Optional, Tuple

# ── ANSI helpers ──────────────────────────────────────────────
_BOLD_CYAN = "\033[1;36m"
_DIM = "\033[2m"
_RESET = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_DOWN = "\033[J"


def _is_tty() -> bool:
    """True when both stdin and stdout are connected to a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _terminal_width() -> int:
    """Return terminal width in columns (default 80 on failure)."""
    try:
        return shutil.get_terminal_size().columns or 80
    except Exception:
        return 80


# Strip ANSI escape sequences to measure visible length
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visible_len(text: str) -> int:
    """Return the visible (non-ANSI) length of *text*."""
    return len(_ANSI_RE.sub("", text))


def _truncate_to_width(text: str, max_width: int) -> str:
    """
    Truncate *text* so that its visible width fits within *max_width*.

    ANSI escape sequences are preserved — only visible characters are
    counted and truncated.  Adds ``…`` when truncated.
    """
    if _visible_len(text) <= max_width:
        return text

    # Walk character-by-character, separating ANSI runs from visible chars
    parts: list[str] = []       # accumulated output
    vis_count = 0
    i = 0
    raw = text
    while i < len(raw) and vis_count < max_width - 1:
        if raw[i] == "\033" and i + 1 < len(raw) and raw[i + 1] == "[":
            # ANSI escape — include verbatim
            j = raw.index("m", i + 2) + 1
            parts.append(raw[i:j])
            i = j
        else:
            parts.append(raw[i])
            vis_count += 1
            i += 1

    # Add ellipsis + any trailing reset
    parts.append("…")
    if not parts[-1].endswith(_RESET):
        parts.append(_RESET)
    return "".join(parts)


# ── Key reading ───────────────────────────────────────────────
def _read_key_raw(fd: int) -> str:
    """Read a single keypress from *fd* (must be in cbreak/raw mode).

    Returns one of: ``"UP"``, ``"DOWN"``, ``"ENTER"``, ``"ESC"``,
    ``"BACKSPACE"``, or the literal character.
    """
    ch = sys.stdin.read(1)
    if ch == "\033":
        ch2 = sys.stdin.read(1)
        if ch2 == "[":
            ch3 = sys.stdin.read(1)
            arrow_map = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
            return arrow_map.get(ch3, f"ESC_{ch3}")
        if ch2 == "\r" or ch2 == "\n":
            return "ENTER"
        return "ESC"
    if ch in ("\r", "\n"):
        return "ENTER"
    if ch in ("\x7f", "\x08"):
        return "BACKSPACE"
    return ch


# ── Picker class ──────────────────────────────────────────────
class Picker:
    """
    Interactive terminal picker.

    Parameters
    ----------
    options:
        List of ``(display_label, value)`` tuples.  The *value* is
        returned on selection; the *display_label* is what the user sees.
    title:
        Optional header printed above the list.
    indicator:
        Character(s) that mark the currently-highlighted row.
    default_index:
        Row that is highlighted when the picker first appears.
    help_text:
        Footer with keybinding hints.  Set to ``""`` to hide.
    """

    def __init__(
        self,
        options: List[Tuple[str, Any]],
        title: str = "",
        indicator: str = ">",
        default_index: int = 0,
        help_text: str = "",
    ):
        if not options:
            raise ValueError("Picker requires at least one option.")
        self.options = options
        self.title = title
        self.indicator = indicator
        self.index = max(0, min(default_index, len(options) - 1))
        self._help = help_text or (
            "↑↓/jk navigate · number+Enter · Enter confirm · q cancel"
        )
        self._num_buf = ""
        self._lines_drawn: int = 0  # for final cleanup only

    # ── public API ────────────────────────────────────────────
    def run(self) -> Optional[Tuple[int, Any]]:
        """
        Show the picker and block until the user makes a choice.

        Returns ``(index, value)`` or ``None`` if the user cancelled.
        """
        if not _is_tty():
            return self._fallback()

        try:
            import termios
            import tty
        except ImportError:
            return self._fallback()

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            return self._run_tty(fd, old)
        except Exception:
            return self._fallback()
        finally:
            # Always restore terminal + cursor
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass
            sys.stdout.write(_SHOW_CURSOR)
            sys.stdout.flush()

    # ── interactive TTY path ──────────────────────────────────
    def _run_tty(self, fd: int, old_settings) -> Optional[Tuple[int, Any]]:
        import termios
        import tty

        tty.setcbreak(fd)

        sys.stdout.write(_HIDE_CURSOR)
        self._draw()
        sys.stdout.flush()

        while True:
            key = _read_key_raw(fd)

            if key in ("UP", "k"):
                self.index = (self.index - 1) % len(self.options)
                self._num_buf = ""
            elif key in ("DOWN", "j"):
                self.index = (self.index + 1) % len(self.options)
                self._num_buf = ""
            elif key == "ENTER":
                if self._num_buf:
                    self._try_jump(self._num_buf)
                    self._num_buf = ""
                self._clear()
                sys.stdout.write(_SHOW_CURSOR)
                sys.stdout.flush()
                return self.index, self.options[self.index][1]
            elif key in ("q", "ESC"):
                self._clear()
                sys.stdout.write(_SHOW_CURSOR)
                sys.stdout.flush()
                return None
            elif key == "BACKSPACE":
                if self._num_buf:
                    self._num_buf = self._num_buf[:-1]
                    if self._num_buf:
                        self._try_jump(self._num_buf)
            elif key.isdigit():
                self._num_buf += key
                self._try_jump(self._num_buf)
            # Ignore other keys

            # Erase previous draw and repaint
            self._clear()
            self._draw()

    # ── fallback (non-TTY / error) ───────────────────────────
    def _fallback(self) -> Optional[Tuple[int, Any]]:
        """Simple ``input()`` based selection for non-TTY environments."""
        self._print_static()
        try:
            raw = input("  Enter number (q to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() == "q":
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(self.options):
                return idx, self.options[idx][1]
        except ValueError:
            pass
        print(f"  Invalid choice.")
        return None

    # ── drawing helpers ───────────────────────────────────────
    def _format_line(self, i: int, max_width: int) -> str:
        label = self.options[i][0]
        if i == self.index:
            line = f"  {self.indicator} {i + 1}. {_BOLD_CYAN}{label}{_RESET}"
        else:
            line = f"    {i + 1}. {label}"
        return _truncate_to_width(line, max_width)

    def _draw(self) -> None:
        """
        Paint the picker at the current cursor position.

        Lines are truncated to the terminal width to prevent wrapping
        which would break the line-counting used by _clear().
        """
        max_width = _terminal_width()
        lines = 0

        if self.title:
            sys.stdout.write(f"  {self.title}\n")
            lines += 1
            sys.stdout.write("\n")
            lines += 1
        else:
            sys.stdout.write("\n")
            lines += 1

        n_total = len(self.options)
        viewport_size = 15

        if n_total <= viewport_size:
            start_idx = 0
            end_idx = n_total
        else:
            start_idx = max(0, self.index - viewport_size // 2)
            end_idx = start_idx + viewport_size
            if end_idx > n_total:
                end_idx = n_total
                start_idx = end_idx - viewport_size

        # Up indicator
        if start_idx > 0:
            sys.stdout.write(f"    {_DIM}▲ (+ {start_idx} more above){_RESET}\n")
            lines += 1

        for i in range(start_idx, end_idx):
            sys.stdout.write(self._format_line(i, max_width) + "\n")
            lines += 1

        # Down indicator
        if end_idx < n_total:
            sys.stdout.write(f"    {_DIM}▼ (+ {n_total - end_idx} more below){_RESET}\n")
            lines += 1

        sys.stdout.write("\n")
        lines += 1

        help_line = _truncate_to_width(
            f"  {_DIM}{self._help}{_RESET}", max_width
        )
        sys.stdout.write(help_line + "\n")
        lines += 1

        sys.stdout.flush()
        self._lines_drawn = lines

    def _clear(self) -> None:
        """
        Erase the picker by moving the cursor up by the number of
        lines drawn and clearing everything below.

        Uses line-counting (\033[N A + \033[J) instead of
        save/restore cursor (\033[s / \033[u) because the latter is
        unreliable in many terminals and causes duplicate menus.
        """
        if self._lines_drawn > 0:
            # Move cursor up N lines (cursor is at col 0 after _draw)
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            sys.stdout.write("\r")          # ensure column 0
            sys.stdout.write(_CLEAR_DOWN)    # clear from cursor to end
            sys.stdout.flush()

    def _print_static(self) -> None:
        """Print a static (non-interactive) version of the list."""
        if self.title:
            print(f"\n  {self.title}\n")
        for i in range(len(self.options)):
            marker = self.indicator if i == self.index else " "
            print(f"  {marker} {i + 1}. {self.options[i][0]}")
        print()

    def _try_jump(self, buf: str) -> None:
        """Try to move the cursor to the option matching *buf* (1-based)."""
        try:
            idx = int(buf) - 1
            if 0 <= idx < len(self.options):
                self.index = idx
        except ValueError:
            pass


# ── Convenience function ──────────────────────────────────────
def pick(
    options: List[Tuple[str, Any]],
    title: str = "",
    default_index: int = 0,
) -> Optional[Tuple[int, Any]]:
    """
    One-shot picker.  Returns ``(index, value)`` or ``None``.

    Example::

        choice = pick(
            [("Dry Run", "dry"), ("Live", "live")],
            title="Mode",
        )
        if choice:
            idx, value = choice   # (0, "dry") or (1, "live")
    """
    return Picker(options, title=title, default_index=default_index).run()


# ── Multi-select Picker ───────────────────────────────────────

_GREEN = "\033[32m"
_YELLOW = "\033[1;33m"


class MultiPicker:
    """
    Interactive terminal multi-select picker.

    Controls
    --------
    ↑/↓ or j/k  — navigate
    Space        — toggle selection on current row
    a            — toggle all (select all / deselect all)
    Enter        — confirm and return selected items
    q / Esc      — cancel (returns None)

    Returns a list of ``(index, value)`` tuples for all checked items,
    or ``None`` if cancelled.  Returns ``[]`` if confirmed with nothing selected.
    """

    def __init__(
        self,
        options: List[Tuple[str, Any]],
        title: str = "",
        indicator: str = ">",
        preselected: Optional[List[int]] = None,
    ):
        if not options:
            raise ValueError("MultiPicker requires at least one option.")
        self.options = options
        self.title = title
        self.indicator = indicator
        self.index = 0
        self._selected: set[int] = set(preselected or [])
        self._help = (
            "↑↓/jk navigate · Space toggle · a all · Enter confirm · q cancel"
        )
        self._lines_drawn: int = 0

    # ── public API ────────────────────────────────────────────

    def run(self) -> Optional[List[Tuple[int, Any]]]:
        """
        Show the multi-picker and block until the user confirms or cancels.

        Returns a list of ``(index, value)`` pairs, or ``None`` if cancelled.
        """
        if not _is_tty():
            return self._fallback()

        try:
            import termios
            import tty
        except ImportError:
            return self._fallback()

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            return self._run_tty(fd, old)
        except Exception:
            return self._fallback()
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass
            sys.stdout.write(_SHOW_CURSOR)
            sys.stdout.flush()

    # ── interactive TTY path ──────────────────────────────────

    def _run_tty(self, fd: int, old_settings) -> Optional[List[Tuple[int, Any]]]:
        import termios
        import tty

        tty.setcbreak(fd)
        sys.stdout.write(_HIDE_CURSOR)
        self._draw()
        sys.stdout.flush()

        while True:
            key = _read_key_raw(fd)

            if key in ("UP", "k"):
                self.index = (self.index - 1) % len(self.options)
            elif key in ("DOWN", "j"):
                self.index = (self.index + 1) % len(self.options)
            elif key == " ":
                # Toggle selection
                if self.index in self._selected:
                    self._selected.discard(self.index)
                else:
                    self._selected.add(self.index)
            elif key in ("a", "A"):
                # Toggle all
                if len(self._selected) == len(self.options):
                    self._selected.clear()
                else:
                    self._selected = set(range(len(self.options)))
            elif key == "ENTER":
                self._clear()
                sys.stdout.write(_SHOW_CURSOR)
                sys.stdout.flush()
                return [(i, self.options[i][1]) for i in sorted(self._selected)]
            elif key in ("q", "ESC"):
                self._clear()
                sys.stdout.write(_SHOW_CURSOR)
                sys.stdout.flush()
                return None

            self._clear()
            self._draw()

    # ── fallback ─────────────────────────────────────────────

    def _fallback(self) -> Optional[List[Tuple[int, Any]]]:
        """Simple input()-based multi-selection for non-TTY environments."""
        self._print_static()
        print(
            "  Enter comma-separated numbers, 'a' for all, or 'q' to cancel:"
        )
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

        if raw.lower() == "q":
            return None
        if raw.lower() == "a":
            return [(i, self.options[i][1]) for i in range(len(self.options))]

        selected: list[tuple[int, Any]] = []
        for token in raw.split(","):
            token = token.strip()
            try:
                idx = int(token) - 1
                if 0 <= idx < len(self.options):
                    selected.append((idx, self.options[idx][1]))
            except ValueError:
                pass
        return selected

    # ── drawing ──────────────────────────────────────────────

    def _format_line(self, i: int, max_width: int) -> str:
        label = self.options[i][0]
        checked = i in self._selected
        check_mark = f"{_GREEN}✓{_RESET}" if checked else " "

        if i == self.index:
            line = (
                f"  {self.indicator} [{check_mark}] {i + 1}. "
                f"{_BOLD_CYAN}{label}{_RESET}"
            )
        else:
            line = f"    [{check_mark}] {i + 1}. {label}"

        return _truncate_to_width(line, max_width)

    def _draw(self) -> None:
        max_width = _terminal_width()
        lines = 0
        n_selected = len(self._selected)
        n_total = len(self.options)

        if self.title:
            sys.stdout.write(f"  {self.title}\n")
            lines += 1
        sys.stdout.write(
            f"  {_DIM}Selected: {_RESET}"
            f"{_YELLOW}{n_selected}{_RESET}{_DIM}/{n_total}{_RESET}\n"
        )
        lines += 1
        sys.stdout.write("\n")
        lines += 1

        viewport_size = 15
        if n_total <= viewport_size:
            start_idx = 0
            end_idx = n_total
        else:
            start_idx = max(0, self.index - viewport_size // 2)
            end_idx = start_idx + viewport_size
            if end_idx > n_total:
                end_idx = n_total
                start_idx = end_idx - viewport_size

        # Up indicator
        if start_idx > 0:
            sys.stdout.write(f"    {_DIM}▲ (+ {start_idx} more above){_RESET}\n")
            lines += 1

        for i in range(start_idx, end_idx):
            sys.stdout.write(self._format_line(i, max_width) + "\n")
            lines += 1

        # Down indicator
        if end_idx < n_total:
            sys.stdout.write(f"    {_DIM}▼ (+ {n_total - end_idx} more below){_RESET}\n")
            lines += 1

        sys.stdout.write("\n")
        lines += 1

        help_line = _truncate_to_width(
            f"  {_DIM}{self._help}{_RESET}", max_width
        )
        sys.stdout.write(help_line + "\n")
        lines += 1

        sys.stdout.flush()
        self._lines_drawn = lines

    def _clear(self) -> None:
        if self._lines_drawn > 0:
            sys.stdout.write(f"\033[{self._lines_drawn}A")
            sys.stdout.write("\r")
            sys.stdout.write(_CLEAR_DOWN)
            sys.stdout.flush()

    def _print_static(self) -> None:
        if self.title:
            print(f"\n  {self.title}\n")
        for i, (label, _) in enumerate(self.options):
            mark = "✓" if i in self._selected else " "
            cursor = self.indicator if i == self.index else " "
            print(f"  {cursor} [{mark}] {i + 1}. {label}")
        print()


def multi_pick(
    options: List[Tuple[str, Any]],
    title: str = "",
    preselected: Optional[List[int]] = None,
) -> Optional[List[Tuple[int, Any]]]:
    """
    One-shot multi-picker.  Returns a list of ``(index, value)`` or ``None``.

    Example::

        choices = multi_pick(
            [("Naruto", p1), ("Bleach", p2), ("One Piece", p3)],
            title="Select Series",
        )
        for idx, path in (choices or []):
            process(path)
    """
    return MultiPicker(options, title=title, preselected=preselected).run()

