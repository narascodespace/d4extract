"""D4.Export application entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys

# QtPy / pyvistaqt honor QT_API to pick a Qt binding. Set before any Qt import.
os.environ.setdefault("QT_API", "pyside6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from d4extract.gui.main_window import D4ExportWindow  # noqa: E402
from d4extract.gui.theme import DARK_THEME_QSS  # noqa: E402


# Categories used to fabricate variety in the mock catalog so the
# pill filters have something meaningful to filter against.
_MOCK_CATEGORIES: tuple[str, ...] = (
    "monster_beast",
    "monster_demon",
    "monster_undead",
    "player_barb",
    "player_sorc",
    "player_rogue",
    "player_druid",
    "player_necro",
    "player_spiritborn",
    "npc_vendor",
    "npc_quest",
    "item_armor",
    "item_jewelry",
    "weapon_sword",
    "weapon_bow",
    "weapon_staff",
    "environment_dungeon",
    "environment_outdoor",
    "world_sanctuary",
    "world_hell",
)


def _generate_mock_entries(n: int = 13_000) -> list[str]:
    """Synthesize ~13K plausible SNO paths for UI testing."""
    out: list[str] = []
    cats = _MOCK_CATEGORIES
    for i in range(n):
        cat = cats[i % len(cats)]
        out.append(f"base/meta/Appearance/{cat}_{i:05d}.app")
    return out


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="d4extract.gui", description="D4.Export GUI")
    parser.add_argument(
        "--mock-catalog",
        action="store_true",
        help="Skip CASC and populate the model list with synthetic entries "
             "(set D4EXPORT_MOCK_CATALOG=1 to do the same via env var).",
    )
    parser.add_argument(
        "--mock-count",
        type=int,
        default=13_000,
        help="Number of mock entries to generate when --mock-catalog is set.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging.",
    )
    # Drop unknown args (Qt can swallow some of its own; we don't want
    # argparse to abort on those).
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    use_mock = args.mock_catalog or os.environ.get("D4EXPORT_MOCK_CATALOG") == "1"
    mock_entries = _generate_mock_entries(args.mock_count) if use_mock else None

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("D4.Export")
    app.setOrganizationName("d4extract")
    app.setStyleSheet(DARK_THEME_QSS)

    window = D4ExportWindow(mock_entries=mock_entries)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
