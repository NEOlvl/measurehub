"""Login window (MH-48, FR-34). The token is kept only in memory."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from measurehub_client.controller import Controller, describe_error
from measurehub_client.qt_util import set_button_text


class LoginDialog(QDialog):
    def __init__(
        self, controller: Controller, username: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("MeasureHub — вход")
        self.setMinimumWidth(360)
        self._ctl = controller
        self.server = QLabel(controller.api.base_url)
        self.server.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.username = QLineEdit(username)
        self.username.setObjectName("username")
        self.password = QLineEdit()
        self.password.setObjectName("password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.status = QLabel()
        self.status.setObjectName("login_status")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #c0392b")
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        set_button_text(self.buttons, QDialogButtonBox.StandardButton.Ok, "Войти")
        set_button_text(self.buttons, QDialogButtonBox.StandardButton.Cancel, "Выход")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Сервер", self.server)
        form.addRow("Логин", self.username)
        form.addRow("Пароль", self.password)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)
        self.password.returnPressed.connect(self._submit)

    def _set_busy(self, busy: bool) -> None:
        self.buttons.setEnabled(not busy)
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)

    def _submit(self) -> None:
        username, password = self.username.text().strip(), self.password.text()
        if not username or not password:
            self.status.setText("Введите логин и пароль")
            return
        self.status.clear()
        self._set_busy(True)
        api = self._ctl.api
        self._ctl.run(lambda: api.login(username, password), self._logged_in, self._failed)

    def _logged_in(self, _: object) -> None:
        self.password.clear()  # the password is not kept; only the token stays in memory
        self.accept()

    def _failed(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status.setText(describe_error(exc))
        self.password.selectAll()
        self.password.setFocus()
