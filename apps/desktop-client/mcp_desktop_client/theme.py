STYLESHEET = """
QWidget {
  background: #f3f4f8;
  color: #18212f;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei";
  font-size: 13px;
}

QMainWindow {
  background: #eef1f7;
}

QFrame#Sidebar,
QFrame#Panel {
  background: #fbfcfe;
  border: 1px solid #dde3ee;
  border-radius: 18px;
}

QLabel#Title {
  font-size: 28px;
  font-weight: 700;
  color: #101828;
}

QLabel#Eyebrow {
  font-size: 11px;
  letter-spacing: 2px;
  color: #6b7280;
  text-transform: uppercase;
}

QPushButton {
  background: #111827;
  color: white;
  border: none;
  border-radius: 12px;
  padding: 10px 16px;
  font-weight: 600;
}

QPushButton:hover {
  background: #1f2937;
}

QPushButton[secondary="true"] {
  background: #e8edf5;
  color: #1f2937;
}

QPushButton[secondary="true"]:hover {
  background: #dce4f0;
}

QLineEdit, QTextEdit, QComboBox, QSpinBox {
  background: white;
  border: 1px solid #d7deea;
  border-radius: 10px;
  padding: 8px 10px;
}

QListWidget {
  background: transparent;
  border: none;
}

QListWidget::item {
  background: white;
  border: 1px solid #d7deea;
  border-radius: 14px;
  margin: 0 0 10px 0;
  padding: 12px;
}

QListWidget::item:selected {
  background: #e9efff;
  border: 1px solid #9db6ff;
  color: #0f172a;
}

QGroupBox {
  font-weight: 700;
  border: 1px solid #dde3ee;
  border-radius: 14px;
  margin-top: 12px;
  padding-top: 14px;
  background: #ffffff;
}

QGroupBox::title {
  subcontrol-origin: margin;
  left: 14px;
  padding: 0 6px;
}
"""
