"""Small shared Qt presentation helpers; no media or database operations."""
from math import ceil
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QPen, QTextOption
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QWidget, QScrollArea,
    QFrame, QLayout, QDialogButtonBox, QStyledItemDelegate, QStyleOptionViewItem, QStyle, QTextEdit)

COLORS = dict(text='#263344', muted='#64748B', primary='#2466C4',
              success='#24734A', warning='#9A5B0A', error='#B42318')


def status_color(value):
    value = str(value or '')
    if any(word in value for word in ('失败', '异常', 'failed', 'error')):
        return COLORS['error']
    if any(word in value for word in ('待确认', '待恢复', '待收尾', '保留', '停止', 'stopped', 'source_retained')):
        return COLORS['warning']
    if value in ('complete', 'completed', 'published', 'skipped') or any(word in value for word in ('完成', '已还原', '已导入', '已写入', '已另存', '已校验', '重复跳过')):
        return COLORS['success']
    return COLORS['muted']


def work_area(window):
    return window.screen().availableGeometry()


def fit_window(window, available=None):
    """Use logical available-work-area dimensions; never alter OS display scaling."""
    area = available or work_area(window)
    # Qt move() places the native frame, whereas resize() sizes the client.
    # Account for the actual title bar instead of assuming it fits in 24 px.
    handle=window.windowHandle()
    margins=handle.frameMargins() if handle else None
    frame_width=(margins.left()+margins.right()) if margins else 0
    frame_height=(margins.top()+margins.bottom()) if margins else 0
    width, height = max(1, area.width()-24-frame_width), max(1, area.height()-24-max(24,frame_height))
    window.resize(min(window.width(), width), min(window.height(), height))
    window.move(area.x()+(area.width()-window.width()-frame_width)//2, area.y()+12)


class WrappingText(QTextEdit):
    """Selectable plain values, including paths with no natural word boundaries."""
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet('QTextEdit { background: transparent; border: none; padding: 0; }')
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self.setText(text)

    def _fit_height(self, size):
        self.setFixedHeight(max(self.fontMetrics().height()+2, ceil(size.height())+2))

    def setText(self, text):
        self.setPlainText(text)

    def text(self):
        return self.toPlainText()


class AdaptiveDialog(QDialog):
    """Scroll only dialog content when space is tight; action buttons stay reachable."""
    def showEvent(self, event):
        if not getattr(self, '_adaptive_ready', False):
            self._adaptive_ready = True
            old = self.layout()
            if old is not None:
                actions = []
                for i in reversed(range(old.count())):
                    widget = old.itemAt(i).widget()
                    if isinstance(widget, QDialogButtonBox):
                        old.takeAt(i); actions.insert(0, widget)
                content = QWidget()
                content.setLayout(old)
                old.setContentsMargins(0, 0, 0, 0)
                outer = QVBoxLayout(self)
                outer.setContentsMargins(12, 10, 12, 10)
                outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
                self.content_scroll = QScrollArea()
                self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
                self.content_scroll.setWidgetResizable(True)
                self.content_scroll.setWidget(content)
                content.setAutoFillBackground(False)
                outer.addWidget(self.content_scroll, 1)
                for widget in actions: outer.addWidget(widget)
            self.setMinimumSize(0, 0)
        super().showEvent(event)
        fit_window(self)


class MediaDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        model = index.model()
        fields = getattr(model, 'fields', None)
        is_name = index.column() in (1, 2, 8) if fields is None else any(
            part in fields[index.column()] for part in ('name', 'path', 'directory', 'subject'))
        if is_name: opt.textElideMode = Qt.TextElideMode.ElideMiddle
        color = index.data(Qt.ItemDataRole.ForegroundRole) or QColor(COLORS['text'])
        opt.palette.setColor(QPalette.ColorRole.Text, color)
        opt.palette.setColor(QPalette.ColorRole.HighlightedText, color)
        view = opt.widget
        style = view.style() if view else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, view)
        if view and view.currentIndex().row() == index.row():
            painter.save()
            painter.setPen(QPen(QColor('#9DB9DE'), 1))
            painter.drawLine(opt.rect.topLeft(), opt.rect.topRight())
            painter.drawLine(opt.rect.bottomLeft(), opt.rect.bottomRight())
            first = next((i for i in range(model.columnCount()) if not view.isColumnHidden(i)), 0)
            if index.column() == first:
                painter.fillRect(opt.rect.x(), opt.rect.y(), 3, opt.rect.height(), QColor(COLORS['primary']))
            painter.restore()
