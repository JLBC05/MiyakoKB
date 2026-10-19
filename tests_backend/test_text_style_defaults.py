import json
import os
import unittest
from pathlib import Path

from src.shared.text_style_defaults import (
    _validate_text_style_defaults,
    get_text_style_factory_defaults,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TextStyleFactoryDefaultsTests(unittest.TestCase):
    def test_factory_defaults_preserve_finite_decimal_stroke_widths(self) -> None:
        defaults = get_text_style_factory_defaults()
        for width in (0, 0.1, 1.25, 3):
            with self.subTest(width=width):
                validated = _validate_text_style_defaults({**defaults, "strokeWidth": width})
                self.assertEqual(validated["strokeWidth"], width)
        for width in (-0.1, float("nan"), float("inf")):
            with self.subTest(width=width), self.assertRaisesRegex(RuntimeError, "strokeWidth"):
                _validate_text_style_defaults({**defaults, "strokeWidth": width})

    def test_loader_returns_exact_bundled_factory_defaults(self) -> None:
        defaults_path = (
            PROJECT_ROOT / "src" / "shared" / "text_style_defaults_factory.json"
        )
        expected = json.loads(defaults_path.read_text(encoding="utf-8"))

        self.assertEqual(get_text_style_factory_defaults(), expected)

    def test_factory_defaults_enable_auto_font_size(self) -> None:
        self.assertTrue(get_text_style_factory_defaults()["autoFontSize"])

    def test_factory_defaults_are_returned_as_deep_copies(self) -> None:
        first = get_text_style_factory_defaults()
        first["fontSize"] = 999

        self.assertNotEqual(
            first["fontSize"],
            get_text_style_factory_defaults()["fontSize"],
        )

    def test_factory_defaults_reject_unknown_fields_and_boolean_numbers(self) -> None:
        defaults = get_text_style_factory_defaults()
        with self.assertRaisesRegex(RuntimeError, "多余"):
            _validate_text_style_defaults({**defaults, "legacyValue": True})
        for field in ("fontSize", "strokeWidth", "lineSpacing"):
            broken = {**defaults, field: True}
            with self.subTest(field=field), self.assertRaisesRegex(
                RuntimeError,
                "类型错误",
            ):
                _validate_text_style_defaults(broken)


if __name__ == "__main__":
    unittest.main()
