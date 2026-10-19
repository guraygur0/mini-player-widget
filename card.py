"""Drawing language shared with focus-widget/focus.pyw and the AI limit card (554-unit canvas)."""
import random
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (QColor, QPainter, QPainterPath, QLinearGradient, QFont, QPen, QImage,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QGraphicsScene, QGraphicsPixmapItem, QGraphicsBlurEffect

BLUE = QColor('#009cff')
TEXT = QColor('#f5f5f5')
MUTED = QColor('#999999')
CANVAS_W = 554
LEFT, RIGHT, CENTER = Qt.AlignmentFlag.AlignLeft, Qt.AlignmentFlag.AlignRight, Qt.AlignmentFlag.AlignHCenter
MENU_STYLE = ('QMenu { background:#252525; color:#f5f5f5; padding:6px; } QMenu::item { padding:7px 20px; }'
              ' QMenu::item:selected { background:#007acc; } QMenu::item:disabled { color:#777777; }')


def font(size, bold=False):
    f = QFont('Segoe UI'); f.setFamilies(['Segoe UI', 'Segoe UI Emoji']); f.setPixelSize(size)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def draw_text(p, rect, value, size=19, color=TEXT, bold=False, align=LEFT, elide=False):
    p.setFont(font(size, bold)); p.setPen(color)
    rect = QRectF(*rect)
    if elide:
        value = p.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, int(rect.width()))
    p.drawText(rect, Qt.AlignmentFlag.AlignVCenter | align, value)


def draw_card(p, width, height, backdrop=None):
    path = QPainterPath(); path.addRoundedRect(QRectF(.6, .6, width-1.2, height-1.2), 52, 52)
    gradient = QLinearGradient(0, 0, 0, height*.68)
    gradient.setColorAt(0, QColor('#313131')); gradient.setColorAt(1, QColor('#1d1d1d'))
    p.fillPath(path, gradient)
    if backdrop is not None and not backdrop.isNull():
        # The cover, blurred at device resolution: its colours sit where they are on the cover.
        p.save(); p.setClipPath(path)
        p.setOpacity(.95); p.drawImage(QRectF(0, 0, width, height), backdrop)
        p.setOpacity(1)
        shade = QLinearGradient(0, 0, 0, height)
        shade.setColorAt(0, QColor(0, 0, 0, 55)); shade.setColorAt(.55, QColor(0, 0, 0, 85)); shade.setColorAt(1, QColor(0, 0, 0, 125))
        p.fillPath(path, shade)
        p.drawTiledPixmap(QRectF(0, 0, width, height), grain())   # breaks up banding in the dark gradients
        p.restore()
    p.setPen(QPen(QColor(110, 110, 110, 100), 1)); p.drawPath(path)


_GRAIN = None


def grain():
    global _GRAIN
    if _GRAIN is None:
        img = QImage(96, 96, QImage.Format.Format_ARGB32_Premultiplied)
        rng = random.Random(7)
        for y in range(96):
            for x in range(96):
                v = rng.randint(0, 255); a = rng.randint(0, 9)
                img.setPixelColor(x, y, QColor(v, v, v, a))
        _GRAIN = QPixmap.fromImage(img)
    return _GRAIN


def vivid(image, boost=1.35):
    """A copy with saturation lifted, so the blurred colours stay rich rather than muddy."""
    img = image.convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(img.height()):
        for x in range(img.width()):
            h, s, v, a = img.pixelColor(x, y).getHsvF()
            img.setPixelColor(x, y, QColor.fromHsvF(max(h, 0), min(1.0, s*boost), min(1.0, v*1.05), a))
    return img


def blurred_backdrop(image, width, height, radius):
    """Cover blurred into a width x height image (device pixels), edges fully covered."""
    if image.isNull() or width < 2 or height < 2:
        return QImage()
    margin = int(radius*1.6)
    small = image.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    # Stretch rather than crop: every colour of the cover stays in the card, in its place.
    source = vivid(small).scaled(width+2*margin, height+2*margin, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
    ox, oy = (source.width()-width)//2, (source.height()-height)//2
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(source))
    blur = QGraphicsBlurEffect(); blur.setBlurRadius(radius); blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(blur); scene.addItem(item)
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied); out.fill(Qt.GlobalColor.black)
    painter = QPainter(out); painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    scene.render(painter, QRectF(0, 0, width, height), QRectF(ox, oy, width, height))
    painter.end()
    return out


def draw_circle(p, rect, hover=False, primary=False, enabled=True):
    p.setPen(Qt.PenStyle.NoPen)
    if primary:
        color = QColor('#25acff') if hover else BLUE
    else:
        color = QColor('#484848') if hover and enabled else QColor('#383838' if enabled else '#2a2a2a')
    p.setBrush(color); p.drawEllipse(rect)


def glyph_pen(enabled=True, width=2.2):
    return QPen(TEXT if enabled else QColor('#5c5c5c'), width, Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def draw_pill(p, rect, label, hover=False, size=15):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor('#3a3a3a' if hover else '#2c2c2c'))
    p.drawRoundedRect(rect, rect.height()/2, rect.height()/2)
    draw_text(p, (rect.x()+16, rect.y()+4, rect.width()-32, rect.height()-8), label, size, QColor('#c8c8c8'), elide=True)


def clamp_to_screen(widget):
    center = widget.frameGeometry().center()
    screen = next((s for s in QApplication.screens() if s.availableGeometry().contains(center)),
                  QApplication.primaryScreen())
    a = screen.availableGeometry()
    if a.bottom() == screen.geometry().bottom():
        a.adjust(0, 0, 0, -48)  # Keep clear of the auto-hidden taskbar.
    widget.move(max(a.left(), min(widget.x(), a.right()-widget.width()+1)),
                max(a.top(), min(widget.y(), a.bottom()-widget.height()+1)))
