"""Every image says which licences it carries, and only the ultra ones carry AGPL.

Wactorz is Apache-2.0. The ultra app image and the ultra add-on install
Ultralytics, which is AGPL-3.0, so they are distributed under both, and their
labels and documentation say so. The default image and the standard add-on
contain no AGPL component, and their labels must not claim one: a label read
by a scanner or a registry is the first place someone checks.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

APACHE = "Apache-2.0"
WITH_ULTRALYTICS = "Apache-2.0 AND AGPL-3.0-only"


def _label_line(workflow: str) -> str:
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
    (line,) = re.findall(r"org\.opencontainers\.image\.licenses=.*", text)
    return line


def test_the_app_images_are_labelled_by_flavour() -> None:
    line = _label_line("image.yml")

    assert f"matrix.flavour == 'ultra' && '{WITH_ULTRALYTICS}' || '{APACHE}'" in line


def test_the_add_on_images_are_labelled_by_variant() -> None:
    line = _label_line("addon-image.yml")

    assert f"matrix.variant == 'wactorz-ultra' && '{WITH_ULTRALYTICS}' || '{APACHE}'" in line


def test_the_add_ons_own_build_labels_agree() -> None:
    def licences(addon: str) -> str:
        build = yaml.safe_load((ROOT / "ha-addon" / addon / "build.yaml").read_text())
        return build["labels"]["org.opencontainers.image.licenses"]

    assert licences("wactorz") == APACHE
    assert licences("wactorz-ultra") == WITH_ULTRALYTICS


def test_what_carries_ultralytics_says_so_where_people_choose() -> None:
    which_image = (ROOT / "docs" / "dockerhub.md").read_text(encoding="utf-8")
    ultra_docs = (ROOT / "ha-addon" / "wactorz-ultra" / "DOCS.md").read_text(encoding="utf-8")

    for text in (which_image, ultra_docs):
        assert "AGPL-3.0" in text
        assert "https://github.com/ultralytics/ultralytics" in text
        assert "https://github.com/waldiez/wactorz" in text, "where the corresponding source is"
    standard_docs = (ROOT / "ha-addon" / "wactorz" / "DOCS.md").read_text(encoding="utf-8")
    assert "AGPL" not in standard_docs


def test_the_example_built_on_ultralytics_is_marked_as_such() -> None:
    example = ROOT / "examples" / "yolo_watch"

    for source in sorted(example.glob("*.py")):
        first_lines = source.read_text(encoding="utf-8").splitlines()[:3]
        assert "# SPDX-License-Identifier: AGPL-3.0-only" in first_lines, source.name
    assert "AGPL-3.0" in (example / "README.md").read_text(encoding="utf-8")
