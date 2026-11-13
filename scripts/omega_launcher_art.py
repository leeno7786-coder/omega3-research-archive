"""Paint the Omega setup wizard off Windows exactly as the Win32 wizard paints it.

``SetupWizard._paint_hero`` and ``_create_controls`` in ``scripts/omega_launcher.py``
are the reference: the same client coordinates, palette, texts, stepper, cards and
button styles. This module is pure Pillow (no Tk): it renders one frame per page,
and the Linux wizard shows that frame with its live inputs placed on top at the
rectangles below. Fonts ship with the product in ``config/fonts`` (the portable
Tk has no Xft, and a host font would be a host dependency, AGENTS.md section 1).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageFont

from scripts.omega_launcher import (
    LOCAL_BASE_URL,
    OMEGA_PALETTE,
    SETUP_CLIENT_HEIGHT,
    SETUP_CLIENT_WIDTH,
)

WIDTH = SETUP_CLIENT_WIDTH
HEIGHT = SETUP_CLIENT_HEIGHT
FONTS_RELATIVE = Path("config/fonts")
_SYMBOLS = frozenset("●✓")

# Win32 CreateFontW sizes are pixel em-heights; weight 600-800 maps to Bold.
_FONT_SPECS: dict[str, tuple[str, int]] = {
    "brand": ("NotoSans-Bold.ttf", 24),
    "emblem": ("NotoSans-Bold.ttf", 32),
    "title": ("NotoSans-Bold.ttf", 20),
    "subtitle": ("NotoSans-Regular.ttf", 13),
    "section": ("NotoSans-Bold.ttf", 13),
    "body": ("NotoSans-Regular.ttf", 13),
    "mono": ("NotoSansMono-Regular.ttf", 12),
    "button": ("NotoSans-Bold.ttf", 13),
    "badge": ("NotoSans-Bold.ttf", 11),
    "small": ("NotoSans-Regular.ttf", 11),
}


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom


@dataclass(frozen=True)
class Label:
    """A STATIC control: fixed ``text`` or the state value named by ``key``."""

    rect: Rect
    font: str = "body"
    text: str = ""
    key: str = ""
    color: str = "muted"  # a palette name, or a rule: "status", "security", "boot"
    wrap: bool = True


@dataclass(frozen=True)
class Button:
    action: str
    label: str
    rect: Rect
    style: str = "default"  # default | primary | boot | reset


@dataclass(frozen=True)
class Field:
    """A live input the wizard places on top of the frame."""

    name: str
    rect: Rect
    kind: str  # entry | password | combo | combo_edit | help


@dataclass(frozen=True)
class Page:
    labels: tuple[Label, ...] = ()
    buttons: tuple[Button, ...] = ()
    fields: tuple[Field, ...] = ()


BACK = Button("back", "Back", Rect(32, 608, 110, 38))
NEXT = Button("next", "Next", Rect(798, 608, 110, 38), "primary")
BACK_PAGES = frozenset({1, 2, 3, 4})
NEXT_PAGES = frozenset({0, 1, 2, 3})

COMMON_LABELS = (
    Label(Rect(52, 164, 836, 26), "title", key="title", color="text", wrap=False),
    Label(Rect(52, 190, 836, 24), "subtitle", key="subtitle", color="text", wrap=False),
)

PAGES: dict[int, Page] = {
    0: Page(
        labels=(
            Label(Rect(52, 232, 490, 54), text="Welcome to Omega 3.0. Choose the model seat for the mind, review the product information, then save the setup before booting."),
            Label(Rect(52, 340, 300, 20), "section", "SELECT MIND MODEL SEAT"),
            Label(Rect(52, 408, 490, 20), "small", "Choose Local LM Studio for offline privacy, or Cloud Provider for hosted APIs."),
        ),
        buttons=(
            Button("info", "Product info & help", Rect(52, 475, 210, 38)),
            Button("reset", "Master reset", Rect(278, 475, 150, 38), "reset"),
        ),
        fields=(Field("backend", Rect(52, 365, 340, 30), "combo"),),
    ),
    1: Page(
        labels=(
            Label(Rect(52, 232, 836, 40), text="Protect this portable installation with an optional launch password. Passwords are never stored; only a salted PBKDF2 verifier is kept on the product drive."),
            Label(Rect(52, 285, 300, 20), "section", "NEW LAUNCH PASSWORD"),
            Label(Rect(52, 355, 300, 20), "section", "CONFIRM PASSWORD"),
            Label(Rect(52, 422, 600, 20), "small", "Minimum 8 characters. Leave both fields blank to keep the current setting."),
            Label(Rect(260, 473, 400, 24), key="security_status", color="security"),
        ),
        buttons=(Button("disable_lock", "Disable launch lock", Rect(52, 465, 190, 36)),),
        fields=(
            Field("launch_password", Rect(52, 310, 420, 32), "password"),
            Field("launch_password_confirm", Rect(52, 380, 420, 32), "password"),
        ),
    ),
    # Windows shows three buttons here (LM Studio GUI via the broker, the Linux
    # runtime through WSL, and the Windows-only updater). Natively on Linux the
    # portable LM Studio GUI *is* the Linux runtime, and its AppImage updates
    # itself (LM-Studio-Linux/run-ai.sh applies staged updates at launch).
    2: Page(
        labels=(
            Label(Rect(52, 232, 836, 40), text="Local LM Studio keeps model traffic on this machine. Start the portable GUI, enable its OpenAI-compatible server, and enter the loaded model id."),
            Label(Rect(290, 294, 598, 24), key="lm_update_status"),
            Label(Rect(52, 345, 300, 20), "section", "LM STUDIO SERVER ENDPOINT"),
            Label(Rect(52, 370, 454, 28), "mono", LOCAL_BASE_URL, wrap=False),
            Label(Rect(52, 415, 300, 20), "section", "LOADED MODEL IDENTIFIER"),
        ),
        buttons=(Button("launch_lm_studio", "Launch LM Studio GUI", Rect(52, 285, 220, 38)),),
        fields=(Field("local_model", Rect(52, 440, 454, 32), "entry"),),
    ),
    3: Page(
        labels=(
            Label(Rect(52, 230, 836, 32), text="Choose a cloud seat, enter its endpoint and API key, then test it to discover model ids."),
            Label(Rect(52, 270, 250, 20), "section", "CLOUD PROVIDER"),
            Label(Rect(52, 344, 250, 20), "section", "ENDPOINT URL"),
            Label(Rect(460, 270, 250, 20), "section", "MODEL IDENTIFIER"),
            Label(Rect(460, 344, 250, 20), "section", "API KEY (ENCRYPTED AT REST)"),
            Label(Rect(52, 480, 836, 24), key="status", color="status"),
        ),
        buttons=(
            Button("save_key", "Save key securely", Rect(460, 414, 200, 36)),
            Button("test", "Test connection", Rect(52, 435, 170, 36)),
        ),
        fields=(
            Field("provider", Rect(52, 294, 380, 30), "combo"),
            Field("endpoint", Rect(52, 368, 380, 32), "entry"),
            Field("model", Rect(460, 294, 420, 30), "combo_edit"),
            Field("key", Rect(460, 368, 420, 32), "password"),
        ),
    ),
    4: Page(
        labels=(
            Label(Rect(52, 230, 836, 30), text="Review your setup. Save for later keeps Omega stopped. Save & Boot Omega starts the backend services and the HUD."),
            Label(Rect(52, 268, 836, 180), "mono", key="summary", wrap=False),
        ),
        buttons=(
            Button("later", "Save for later", Rect(52, 470, 160, 38)),
            Button("reset", "Master reset", Rect(228, 470, 160, 38), "reset"),
            Button("boot", "Save & Boot Omega", Rect(660, 465, 228, 44), "boot"),
        ),
    ),
    5: Page(
        labels=(Label(Rect(52, 230, 836, 22), text="Product information and help"),),
        buttons=(
            Button("website", "Open website", Rect(52, 520, 140, 36)),
            Button("privacy", "Privacy policy", Rect(206, 520, 160, 36)),
            Button("info_back", "Back to setup", Rect(738, 520, 150, 36)),
        ),
        fields=(Field("help", Rect(52, 256, 836, 250), "help"),),
    ),
    6: Page(
        labels=(
            Label(Rect(52, 240, 836, 28), "section", "This portable Omega installation is locked."),
            Label(Rect(52, 275, 836, 24), text="Enter the launch password to continue to setup."),
        ),
        buttons=(Button("unlock", "Unlock Omega", Rect(52, 375, 180, 40), "primary"),),
        fields=(Field("unlock_password", Rect(52, 318, 400, 34), "password"),),
    ),
    7: Page(
        labels=(
            Label(Rect(52, 282, 836, 32), key="boot_status", color="boot"),
            Label(Rect(52, 310, 836, 32), key="boot_detail", color="boot"),
            Label(Rect(52, 395, 300, 24), key="boot_elapsed", color="boot"),
            Label(Rect(52, 430, 836, 32), text="You can hide this window safely; Omega will continue in the background.", color="boot"),
        ),
        buttons=(Button("boot_hide", "Hide window", Rect(738, 500, 150, 38)),),
    ),
}


def page_buttons(page: int) -> tuple[Button, ...]:
    """Every clickable button on ``page``, navigation included."""
    buttons = list(PAGES[page].buttons)
    if page in BACK_PAGES:
        buttons.append(BACK)
    if page in NEXT_PAGES:
        buttons.append(NEXT)
    return tuple(buttons)


def button_at(page: int, x: int, y: int) -> Button | None:
    for button in page_buttons(page):
        if button.rect.contains(x, y):
            return button
    return None


class FontSet:
    """The product's bundled faces. A missing file fails loudly (no host fallback)."""

    def __init__(self, product: Path) -> None:
        root = Path(product) / FONTS_RELATIVE
        self._faces: dict[str, ImageFont.FreeTypeFont] = {}
        for name, (filename, size) in _FONT_SPECS.items():
            path = root / filename
            if not path.is_file():
                raise FileNotFoundError(f"Omega setup font is missing from the portable product: {path}")
            self._faces[name] = ImageFont.truetype(str(path), size)
        symbols = root / "DejaVuSans.ttf"
        if not symbols.is_file():
            raise FileNotFoundError(f"Omega setup font is missing from the portable product: {symbols}")
        self._symbol_path = str(symbols)
        self._symbols: dict[int, ImageFont.FreeTypeFont] = {}

    def face(self, name: str) -> ImageFont.FreeTypeFont:
        return self._faces[name]

    def symbol(self, size: int) -> ImageFont.FreeTypeFont:
        if size not in self._symbols:
            self._symbols[size] = ImageFont.truetype(self._symbol_path, size)
        return self._symbols[size]


def _rgb(name: str) -> tuple[int, int, int]:
    return OMEGA_PALETTE[name]


def label_color(label: Label, state: Mapping[str, Any]) -> str:
    """The Win32 WM_CTLCOLORSTATIC rules, by the same text tests."""
    if label.color == "status":
        text = str(state.get("status") or "")
        return "success" if "OK" in text else ("danger" if "failed" in text.lower() else "muted")
    if label.color == "security":
        text = str(state.get("security_status") or "")
        return "success" if "enabled" in text else ("ember" if "disabled" in text else "muted")
    if label.color == "boot":
        phase = str(state.get("boot_phase") or "")
        return "danger" if phase == "failed" else ("success" if phase == "ready" else "cyan")
    return label.color


class Painter:
    """Draws one frame with the Win32 wizard's primitives."""

    def __init__(self, fonts: FontSet) -> None:
        self.fonts = fonts
        self.image = Image.new("RGB", (WIDTH, HEIGHT), _rgb("window"))
        self.draw = ImageDraw.Draw(self.image)

    def fill(self, left: int, top: int, right: int, bottom: int, color: str) -> None:
        self.draw.rectangle((left, top, right - 1, bottom - 1), fill=_rgb(color))

    def line(self, x1: int, y1: int, x2: int, y2: int, color: str = "border") -> None:
        self.draw.line((x1, y1, x2, y2), fill=_rgb(color), width=1)

    def round_rect(self, left: int, top: int, right: int, bottom: int, fill: str, outline: str | None = None, radius: int = 8) -> None:
        # GDI RoundRect takes the corner ellipse's diameter.
        self.draw.rounded_rectangle(
            (left, top, right - 1, bottom - 1),
            radius=max(1, radius // 2),
            fill=_rgb(fill),
            outline=_rgb(outline or fill),
            width=1,
        )

    def _runs(self, text: str, font: str) -> list[tuple[str, ImageFont.FreeTypeFont]]:
        face = self.fonts.face(font)
        runs: list[tuple[str, ImageFont.FreeTypeFont]] = []
        for char in text:
            chosen = self.fonts.symbol(face.size) if char in _SYMBOLS else face
            if runs and runs[-1][1] is chosen:
                runs[-1] = (runs[-1][0] + char, chosen)
            else:
                runs.append((char, chosen))
        return runs

    def text_width(self, text: str, font: str) -> float:
        return sum(face.getlength(run) for run, face in self._runs(text, font))

    def line_height(self, font: str) -> int:
        ascent, descent = self.fonts.face(font).getmetrics()
        return ascent + descent

    def _draw_line_text(self, x: float, y: float, text: str, font: str, color: str) -> None:
        base_ascent = self.fonts.face(font).getmetrics()[0]
        for run, face in self._runs(text, font):
            ascent = face.getmetrics()[0]
            self.draw.text((x, y + base_ascent - ascent), run, font=face, fill=_rgb(color))
            x += face.getlength(run)

    def text(
        self,
        text: str,
        left: int,
        top: int,
        right: int,
        bottom: int,
        *,
        font: str = "body",
        color: str = "text",
        align: str = "left",
        vcenter: bool = False,
        wrap: bool = False,
    ) -> None:
        width = right - left
        lines: list[str] = []
        for paragraph in str(text).replace("\r\n", "\n").split("\n"):
            if not wrap:
                lines.append(paragraph)
                continue
            current = ""
            for word in paragraph.split(" "):
                candidate = f"{current} {word}" if current else word
                if current and self.text_width(candidate, font) > width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        height = self.line_height(font)
        y = top + ((bottom - top) - height) / 2 if vcenter else top
        for line_text in lines:
            if y + height > bottom + height:  # clip like DrawText in its rectangle
                break
            line_width = self.text_width(line_text, font)
            if align == "center":
                x = left + (width - line_width) / 2
            elif align == "right":
                x = right - line_width
            else:
                x = left
            self._draw_line_text(x, y, line_text, font, color)
            y += height


def _paint_chrome(p: Painter, page: int, state: Mapping[str, Any]) -> None:
    width = WIDTH
    # 2. Header bar
    p.fill(0, 0, width, 86, "header")
    p.line(0, 86, width, 86, "border")
    p.round_rect(32, 16, 86, 70, "card_alt", "border_light", radius=12)
    p.text("Ω", 32, 16, 86, 70, font="emblem", color="cyan", align="center", vcenter=True)
    p.text("OMEGA 3.0", 102, 18, 550, 46, font="brand", color="text")
    p.text("AUTONOMOUS COGNITIVE ARCHITECTURE  //  PORTABLE RUNTIME", 104, 50, 650, 70, font="badge", color="muted")
    badge_left = width - 210
    p.round_rect(badge_left, 26, width - 32, 60, "card_alt", "border", radius=8)
    p.text("●  SSD AIRGAPPED", badge_left, 26, width - 32, 60, font="badge", color="success", align="center", vcenter=True)

    # 3. Stepper navigation bar
    p.fill(0, 87, width, 138, "window")
    p.line(0, 138, width, 138, "border")
    if page in {0, 1, 2, 3, 4}:
        steps = (("01", "SEAT SELECTION"), ("02", "SECURITY LOCK"), ("03", "MODEL ENGINE"), ("04", "READY TO BOOT"))
        active = 0 if page == 0 else (1 if page == 1 else (2 if page in {2, 3} else 3))
        for index, (number, label) in enumerate(steps):
            x = 32 + index * 224
            if index == active:
                p.round_rect(x, 96, x + 208, 128, "card", "cyan", radius=6)
                p.text(f"●  {number}  {label}", x, 96, x + 208, 128, font="badge", color="cyan", align="center", vcenter=True)
            elif index < active:
                p.round_rect(x, 96, x + 208, 128, "window", "border", radius=6)
                p.text(f"✓  {number}  {label}", x, 96, x + 208, 128, font="badge", color="text", align="center", vcenter=True)
            else:
                p.round_rect(x, 96, x + 208, 128, "window", "border", radius=6)
                p.text(f"    {number}  {label}", x, 96, x + 208, 128, font="badge", color="muted", align="center", vcenter=True)
    elif page == 5:
        p.text("DOCUMENTATION  //  PRODUCT INFORMATION & OPERATOR GUIDE", 52, 98, width - 52, 128, font="badge", color="cyan", vcenter=True)
    elif page == 6:
        p.text("SECURITY CHECKPOINT  //  ENCRYPTED SESSION LOCK", 52, 98, width - 52, 128, font="badge", color="ember", vcenter=True)
    elif page == 7:
        p.text("BOOT SEQUENCE  //  PORTABLE SERVICES STARTING", 52, 98, width - 52, 128, font="badge", color="cyan", vcenter=True)

    # 4. Content cards
    card_top, card_bottom = 150, 585
    if page == 0:
        p.round_rect(32, card_top, 562, card_bottom, "card", "border", radius=10)
        p.line(48, 222, 546, 222, "border")
        for left, right, text, color in (
            (52, 202, "✓  100% PORTABLE", "success"),
            (212, 372, "✓  SINGLE-FIFO MIND", "cyan"),
            (382, 542, "✓  ZERO HOST POLLUTION", "muted"),
        ):
            p.round_rect(left, 296, right, 324, "card_alt", "border", radius=6)
            p.text(text, left, 296, right, 324, font="badge", color=color, align="center", vcenter=True)
        p.round_rect(580, card_top, 908, card_bottom, "card", "border", radius=10)
        p.round_rect(598, 168, 890, 362, "card_alt", "border", radius=8)
        hero = state.get("hero")
        if hero is not None:
            p.image.paste(hero, (606, 176))
        p.text("PORTABLE SUBSTRATE PROFILE", 602, 378, 886, 396, font="badge", color="cyan")
        p.line(602, 402, 886, 402, "border")
        for top, key, value, value_color in (
            (412, "COGNITION", "Unified 4-Pipeline Pressure", "text"),
            (440, "MEMORY", "12-Lobe Dewey Coordinates", "text"),
            (468, "VOICE", f"Parakeet STT + {state.get('hero_tts') or 'TTS capability'}", "text"),
            (496, "STORAGE", "Airgapped SSD Volume", "text"),
            (524, "CONSENT", "Final at Connection (§0.1)", "success"),
        ):
            p.text(key, 602, top, 710, top + 18, font="small", color="muted")
            p.text(value, 710, top, 886, top + 18, font="small", color=value_color, align="right")
    else:
        p.round_rect(32, card_top, width - 32, card_bottom, "card", "border", radius=10)
        p.line(48, 222, width - 48, 222, "border")

    if page == 7:
        phase = str(state.get("boot_phase") or "")
        percent = int(state.get("boot_percent") or 0)
        bar = "danger" if phase == "failed" else "cyan"
        p.text("OMEGA BOOT STATUS", 52, 248, width - 52, 272, font="badge", color="cyan")
        p.round_rect(52, 352, width - 52, 374, "control", "border_light", radius=8)
        filled = max(0, min(width - 104, int((width - 104) * percent / 100)))
        if filled:
            p.round_rect(52, 352, 52 + filled, 374, bar, None, radius=8)
        p.text(f"{percent}%", width - 150, 382, width - 52, 410, font="badge", color=bar, align="right", vcenter=True)

    # 5. Footer action bar
    p.fill(0, 595, width, HEIGHT, "window")
    p.line(0, 595, width, 595, "border")
    p.text(
        "OMEGA 3.0  //  LOCAL-FIRST COGNITIVE SYSTEM  •  OPERATOR CONSENT IS FINAL",
        160, 615, width - 160, 642, font="small", color="muted", align="center", vcenter=True,
    )


def _paint_button(p: Painter, button: Button, *, pressed: bool) -> None:
    r = button.rect
    if button.style == "boot":
        fill = outline = "ember_hover" if pressed else "ember"
        color = "text"
    elif button.style == "primary":
        fill = outline = "cyan"
        color = "header"
    elif button.style == "reset":
        fill, outline, color = ("border" if pressed else "card_alt"), "danger", "danger"
    else:
        fill = "border" if pressed else "card_alt"
        outline = "cyan" if pressed else "border_light"
        color = "text"
    p.round_rect(r.x, r.y, r.right, r.bottom, fill, outline, radius=8)
    offset = 1 if pressed else 0
    p.text(button.label, r.x, r.y + offset, r.right, r.bottom + offset, font="button", color=color, align="center", vcenter=True)


def render_page(
    page: int,
    fonts: FontSet,
    state: Mapping[str, Any],
    *,
    pressed: str | None = None,
) -> Image.Image:
    """One full client-area frame for ``page``: chrome, labels and buttons."""
    painter = Painter(fonts)
    _paint_chrome(painter, page, state)
    for label in (*COMMON_LABELS, *PAGES[page].labels):
        text = label.text or str(state.get(label.key) or "")
        if text:
            r = label.rect
            painter.text(
                text, r.x, r.y, r.right, r.bottom,
                font=label.font, color=label_color(label, state), wrap=label.wrap,
            )
    for button in page_buttons(page):
        _paint_button(painter, button, pressed=pressed == button.action)
    return painter.image


def render_text_block(fonts: FontSet, text: str, width: int, *, font: str = "mono") -> Image.Image:
    """A tall, wrapped block (the About page's read-only help edit)."""
    probe = Painter(fonts)
    height = probe.line_height(font)
    lines = 0
    for paragraph in str(text).replace("\r\n", "\n").split("\n"):
        current = ""
        count = 1
        for word in paragraph.split(" "):
            candidate = f"{current} {word}" if current else word
            if current and probe.text_width(candidate, font) > width - 16:
                count += 1
                current = word
            else:
                current = candidate
        lines += count
    painter = Painter(fonts)
    painter.image = Image.new("RGB", (width, max(1, lines * height + 16)), _rgb("control"))
    painter.draw = ImageDraw.Draw(painter.image)
    painter.text(text, 8, 8, width - 8, painter.image.height, font=font, color="text", wrap=True)
    return painter.image


def load_hero(path: Path) -> Image.Image:
    """The hero exactly as GDI+ DrawImageRectI stretches it: 276x178 at (606, 176)."""
    with Image.open(path) as image:
        return image.convert("RGB").resize((276, 178), Image.Resampling.LANCZOS)


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()
