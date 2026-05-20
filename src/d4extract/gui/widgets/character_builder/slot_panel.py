"""Customization dropdowns + equipment slot grid for the Character Builder.

This panel is two stacked sections:

* CHARACTER — five labelled QComboBoxes (Class, Gender, Face, Hair
  Style, Facial Hair) wired into a cascading enable/disable chain.
  Class is always enabled; everything below it is gated on a class
  having been picked, then a gender. Facial Hair is hidden entirely
  for female characters since the game ships no female facial-hair
  meshes.
* EQUIPMENT — seven _SlotCard rows (Helm / Chest / Gloves / Pants /
  Boots / Main Hand / Off-Hand). Behaviour matches the previous
  version: click to select; ``set_equipped`` / ``clear_slot`` mutate
  the right-hand status text.

UI-only — Face / Hair Style / Facial Hair start empty and stay empty
until ``populate_options`` is called by an outer coordinator (later
phase). Class, gender, and slot-selection signals already fire here.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from d4extract.gui.theme import Colors
from d4extract.gui.widgets.character_builder.models import (
    CHARACTER_CLASSES,
    CLASS_PREFIX,
    CUSTOMIZATION_DISPLAY,
    CustomizationField,
    CustomizationOption,
    CustomizationSlotState,
    EQUIPMENT_SLOTS,
    GENDER_SUFFIX,
    GENDERS,
    OPTION_FIELDS,
    SlotState,
)


# Width of the caption to the left of each customization combo box.
# Picked so "Facial Hair" (the longest label) fits without truncation
# at the panel's 200px minimum width.
_CUSTOM_LABEL_WIDTH = 80

# Placeholder text shown in a combo box that has no selection yet.
# Qt's QComboBox.setPlaceholderText only renders when the current
# index is -1, which matches our "nothing selected" state exactly.
_PLACEHOLDER = "—"


def _refresh_style(widget: QWidget) -> None:
    """Re-evaluate QSS for ``widget`` after a dynamic property change.

    Qt's style engine caches selector matches; toggling a property
    used by an attribute selector (``[selected="true"]``) only takes
    visual effect after an unpolish/polish cycle.
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class _SlotCard(QFrame):
    """Single equipment-slot row inside the slot grid.

    A clickable QFrame, not a QPushButton, so the two-column layout
    (slot name on the left, equipped status on the right) lays out
    naturally without fighting QPushButton's centred icon/text
    rendering. Selection is a dynamic property; styling lives in
    ``DARK_THEME_QSS``.
    """

    clicked = Signal(str)  # slot_key

    def __init__(
        self, slot_key: str, display_name: str, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.slot_key = slot_key
        self.display_name = display_name

        self.setObjectName("slotCard")
        # WA_StyledBackground forces QFrame to honour ``background-color``
        # in QSS; without it the frame paints with the palette role and
        # ignores our hover/selected colour shifts.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setProperty("selected", False)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        name_label = QLabel(display_name, self)
        name_label.setObjectName("slotName")
        layout.addWidget(name_label)
        layout.addStretch(1)

        self._status_label = QLabel("Empty", self)
        self._status_label.setObjectName("slotStatus")
        self._status_label.setProperty("filled", False)
        self._status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._status_label)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def set_selected(self, selected: bool) -> None:
        if bool(self.property("selected")) == selected:
            return
        self.setProperty("selected", selected)
        _refresh_style(self)

    def set_status_empty(self) -> None:
        self._status_label.setText("Empty")
        if self._status_label.property("filled"):
            self._status_label.setProperty("filled", False)
            _refresh_style(self._status_label)

    def set_status_filled(self, name: str) -> None:
        self._status_label.setText(name)
        if not self._status_label.property("filled"):
            self._status_label.setProperty("filled", True)
            _refresh_style(self._status_label)

    # ------------------------------------------------------------------
    # Mouse
    # ------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802 — Qt API
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.slot_key)
            event.accept()
            return
        super().mousePressEvent(event)


class _CustomizationRow(QWidget):
    """Label + combo box pair, exposed as one widget for show/hide.

    Wrapping the row in a single widget makes ``setVisible(False)``
    collapse the whole row (used to hide Facial Hair for female
    characters) without leaving an orphan label behind.
    """

    def __init__(
        self,
        field: CustomizationField,
        display_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.field = field

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.label = QLabel(display_name, self)
        self.label.setObjectName("customLabel")
        self.label.setFixedWidth(_CUSTOM_LABEL_WIDTH)
        self.label.setProperty("disabled", False)
        layout.addWidget(self.label)

        self.combo = QComboBox(self)
        self.combo.setPlaceholderText(_PLACEHOLDER)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.combo, 1)

    def set_enabled(self, enabled: bool) -> None:
        self.combo.setEnabled(enabled)
        # Keep the caption colour in sync with the combo's enabled
        # state — Qt doesn't propagate :disabled to siblings, so we do
        # it ourselves via a dynamic property the QSS hooks into.
        self.label.setProperty("disabled", not enabled)
        _refresh_style(self.label)


class SlotPanel(QWidget):
    """Customization dropdowns + equipment slot list."""

    class_changed = Signal(str)               # class name
    gender_changed = Signal(str, str)         # class_name, gender
    customization_changed = Signal(str, str, str)  # field, display, sno_path
    slot_selected = Signal(str)               # slot_key

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(True)

        # ----- Customization state ------------------------------------
        self._current_class: str | None = None
        self._current_gender: str | None = None
        # State objects only exist for the option-bearing fields. Class
        # and gender are simple strings tracked above.
        self._customization_states: dict[
            CustomizationField, CustomizationSlotState
        ] = {
            f: CustomizationSlotState(field=f, display_name=name)
            for f, name in CUSTOMIZATION_DISPLAY
            if f in OPTION_FIELDS
        }
        self._rows: dict[CustomizationField, _CustomizationRow] = {}

        # ----- Equipment state ----------------------------------------
        self._slots: dict[str, SlotState] = {
            key: SlotState(slot_key=key, display_name=name)
            for key, name in EQUIPMENT_SLOTS
        }
        self._cards: dict[str, _SlotCard] = {}
        self._selected_slot: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(12)

        outer.addWidget(self._build_section_header("Character"))
        outer.addLayout(self._build_customization_rows())
        outer.addWidget(self._build_section_header("Equipment"))
        outer.addLayout(self._build_slot_grid())
        outer.addStretch(1)

        # Initial cascade: only Class is enabled; everything else is
        # disabled until the user makes a class selection.
        self._apply_initial_enable_state()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_section_header(self, text: str) -> QLabel:
        label = QLabel(text.upper(), self)
        label.setStyleSheet(
            f"color: {Colors.SUBTEXT}; "
            f"font-size: 11px; font-weight: 700; "
            "letter-spacing: 1px;"
        )
        return label

    def _build_customization_rows(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.setContentsMargins(0, 0, 0, 0)

        for field, name in CUSTOMIZATION_DISPLAY:
            row = _CustomizationRow(field, name, self)
            col.addWidget(row)
            self._rows[field] = row

        # Class and gender are pre-populated; the option-bearing fields
        # stay empty until ``populate_options`` is called.
        self._populate_static_combo(
            CustomizationField.CLASS, CHARACTER_CLASSES,
        )
        self._populate_static_combo(CustomizationField.GENDER, GENDERS)

        # Connect via ``activated`` (user-initiated only); programmatic
        # ``setCurrentIndex`` calls during cascade resets do NOT fire
        # this signal, which is exactly what we want.
        self._rows[CustomizationField.CLASS].combo.activated.connect(
            self._on_class_activated,
        )
        self._rows[CustomizationField.GENDER].combo.activated.connect(
            self._on_gender_activated,
        )
        for field in OPTION_FIELDS:
            self._rows[field].combo.activated.connect(
                lambda idx, f=field: self._on_option_activated(f, idx),
            )
        return col

    def _build_slot_grid(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.setContentsMargins(0, 0, 0, 0)

        for key, name in EQUIPMENT_SLOTS:
            card = _SlotCard(key, name, self)
            card.clicked.connect(self._on_slot_clicked)
            self._cards[key] = card
            col.addWidget(card)
        return col

    def _populate_static_combo(
        self, field: CustomizationField, values: Iterable[str],
    ) -> None:
        combo = self._rows[field].combo
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItems(list(values))
            combo.setCurrentIndex(-1)

    def _apply_initial_enable_state(self) -> None:
        self._rows[CustomizationField.CLASS].set_enabled(True)
        self._rows[CustomizationField.GENDER].set_enabled(False)
        for field in OPTION_FIELDS:
            self._rows[field].set_enabled(False)

    # ------------------------------------------------------------------
    # Customization handlers
    # ------------------------------------------------------------------

    def _on_class_activated(self, index: int) -> None:
        if index < 0 or index >= len(CHARACTER_CLASSES):
            return
        class_name = CHARACTER_CLASSES[index]
        if class_name == self._current_class:
            return
        self._current_class = class_name
        # Class change invalidates everything below: gender resets to
        # unselected, every option-bearing combo clears, equipment
        # slots empty out. Different classes share no skeletons.
        self._reset_gender()
        self._reset_option_fields(disable=True)
        self._rows[CustomizationField.GENDER].set_enabled(True)
        self.clear_all_slots()
        self.class_changed.emit(class_name)

    def _on_gender_activated(self, index: int) -> None:
        if index < 0 or index >= len(GENDERS):
            return
        gender = GENDERS[index]
        if gender == self._current_gender:
            return
        self._current_gender = gender
        # Gender change clears the customization options (different
        # gender = different P##/H##/B## file pool) but leaves
        # equipment alone — armor pieces aren't gender-locked.
        self._reset_option_fields(disable=False)
        self._apply_facial_hair_visibility()
        self.gender_changed.emit(self._current_class or "", gender)

    def _on_option_activated(
        self, field: CustomizationField, index: int,
    ) -> None:
        state = self._customization_states.get(field)
        if state is None:
            return
        if index < 0 or index >= len(state.available_options):
            state.selected_index = -1
            return
        if state.selected_index == index:
            return
        state.selected_index = index
        opt = state.available_options[index]
        self.customization_changed.emit(
            field.value, opt.display_name, opt.sno_path,
        )

    # ------------------------------------------------------------------
    # Cascade helpers
    # ------------------------------------------------------------------

    def _reset_gender(self) -> None:
        self._current_gender = None
        combo = self._rows[CustomizationField.GENDER].combo
        with QSignalBlocker(combo):
            combo.setCurrentIndex(-1)

    def _reset_option_fields(self, *, disable: bool) -> None:
        """Clear the option combos. ``disable=True`` also greys them out.

        Used by both the class-change path (disable until gender is
        picked) and the gender-change path (clear options but leave
        the rows enabled — they're now ready for ``populate_options``
        to fill them).
        """
        for field in OPTION_FIELDS:
            state = self._customization_states[field]
            state.available_options.clear()
            state.selected_index = -1
            row = self._rows[field]
            with QSignalBlocker(row.combo):
                row.combo.clear()
                row.combo.setCurrentIndex(-1)
            row.set_enabled(not disable)

    def _apply_facial_hair_visibility(self) -> None:
        """Hide the Facial Hair row when the current gender is female.

        Female characters have no facial-hair meshes shipped, so the
        row would always be empty — hiding it removes the dead UI
        element rather than leaving a permanently empty combo.
        """
        row = self._rows[CustomizationField.FACIAL_HAIR]
        row.setVisible(self._current_gender == "Male")

    # ------------------------------------------------------------------
    # Slot card handlers
    # ------------------------------------------------------------------

    def _on_slot_clicked(self, slot_key: str) -> None:
        if slot_key == self._selected_slot:
            return
        self._set_selected_slot(slot_key)
        self.slot_selected.emit(slot_key)

    def _set_selected_slot(self, slot_key: str | None) -> None:
        prev = self._selected_slot
        if prev is not None and prev in self._cards:
            self._cards[prev].set_selected(False)
        self._selected_slot = slot_key
        if slot_key is not None and slot_key in self._cards:
            self._cards[slot_key].set_selected(True)

    # ------------------------------------------------------------------
    # Public API — customization
    # ------------------------------------------------------------------

    def get_current_class(self) -> str | None:
        return self._current_class

    def get_current_gender(self) -> str | None:
        return self._current_gender

    def get_class_gender_prefix(self) -> str | None:
        """Return the CASC filename prefix for the current class+gender.

        Examples: ``"barF"`` (female barbarian), ``"necM"`` (male
        necromancer), ``"spiF"`` (female spiritborn). ``None`` if
        either selector is still unset.
        """
        if self._current_class is None or self._current_gender is None:
            return None
        prefix = CLASS_PREFIX.get(self._current_class)
        suffix = GENDER_SUFFIX.get(self._current_gender)
        if prefix is None or suffix is None:
            return None
        return f"{prefix}{suffix}"

    def populate_options(
        self,
        field: CustomizationField,
        options: list[CustomizationOption],
    ) -> None:
        """Fill an option-bearing combo (Face / Hair / Facial Hair).

        Resets the current selection to "nothing picked" — callers
        that want a default selected should follow up with their own
        ``setCurrentIndex`` (or simply rely on the next user click).
        """
        state = self._customization_states.get(field)
        if state is None:
            return
        state.available_options = list(options)
        state.selected_index = -1
        combo = self._rows[field].combo
        with QSignalBlocker(combo):
            combo.clear()
            combo.addItems([opt.display_name for opt in state.available_options])
            combo.setCurrentIndex(-1)

    def get_customization_build(self) -> dict[str, str | None]:
        """Return ``{field_name: sno_path}`` for the option-bearing fields.

        Class and gender are excluded — they're selectors, not file
        choices. Use ``get_current_class`` / ``get_current_gender``
        for those.
        """
        return {
            field.value: self._customization_states[field].selected_sno
            for field in OPTION_FIELDS
        }

    # ------------------------------------------------------------------
    # Public API — equipment
    # ------------------------------------------------------------------

    def get_selected_slot(self) -> str | None:
        return self._selected_slot

    def set_equipped(
        self, slot_key: str, sno_path: str, display_name: str,
    ) -> None:
        state = self._slots.get(slot_key)
        if state is None:
            return
        state.equipped_sno = sno_path
        state.equipped_name = display_name
        card = self._cards.get(slot_key)
        if card is not None:
            card.set_status_filled(display_name)

    def clear_slot(self, slot_key: str) -> None:
        state = self._slots.get(slot_key)
        if state is None:
            return
        state.equipped_sno = None
        state.equipped_name = None
        card = self._cards.get(slot_key)
        if card is not None:
            card.set_status_empty()

    def clear_all_slots(self) -> None:
        for key in self._slots:
            self.clear_slot(key)
        self._set_selected_slot(None)

    def get_build(self) -> dict[str, str | None]:
        return {key: state.equipped_sno for key, state in self._slots.items()}

    # ------------------------------------------------------------------
    # Public API — combined
    # ------------------------------------------------------------------

    def get_full_build(self) -> dict[str, str | None]:
        """Return customization + equipment SNO selections in one dict.

        Keys are namespaced with ``custom:`` / ``slot:`` prefixes so
        callers can tell which side a key came from without keeping a
        separate schema in sync.
        """
        out: dict[str, str | None] = {}
        for key, sno in self.get_customization_build().items():
            out[f"custom:{key}"] = sno
        for key, sno in self.get_build().items():
            out[f"slot:{key}"] = sno
        return out
