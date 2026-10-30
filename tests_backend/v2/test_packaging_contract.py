from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

from packaging.requirements import Requirement

from src.backend_v2.dispatch import _parser


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_packaged_entrypoint_defaults_to_the_backend_launcher() -> None:
    defaults = _parser().parse_args([])
    assert defaults.role == "launcher"
    assert defaults.host == "127.0.0.1"


def test_production_spec_uses_only_the_backend_first_entrypoint() -> None:
    spec = (PROJECT_ROOT / "app.spec").read_text(encoding="utf-8")

    assert "'saber_v2.py'" in spec
    assert "name='MiyakoKB'" in spec
    assert "'src.backend_v2.api.entrypoint'" in spec
    assert "'src.backend_v2.worker.entrypoint'" in spec
    assert "'src.backend_v2.launcher.entrypoint'" in spec
    assert "'PySide6'" not in spec.split("excludes =", 1)[1].split("]", 1)[0]
    # The windowed exe reports startup failures through a Tk dialog, so tkinter
    # must stay bundled even though nothing else uses it.
    assert "'tkinter'" not in spec.split("excludes =", 1)[1].split("]", 1)[0]
    assert "console=False" in spec
    assert "hide_console=" not in spec
    assert "'openapi', 'v2.yaml'" in spec
    assert "'src', 'backend_v2', 'static'" in spec
    assert "'src', 'backend_v2', 'resources'" in spec
    assert "'src', 'shared', 'prompt_defaults_factory.json'" in spec
    assert "datas.append((pic_path, 'pic'))" not in spec
    assert "'rapidocr'" in spec
    assert "accelerate" not in spec
    assert "required packaging dependency is missing" in spec
    assert "shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo')" in spec
    assert "os.stat(bundle_plugins_path).st_mode | stat.S_IWRITE" in spec
    assert "'plugin.json'" in spec
    assert "collect_all({pkg}) FAILED" not in spec
    assert "except:" not in spec
    assert "'app.py'" not in spec
    assert "'src.app" not in spec
    assert not (PROJECT_ROOT / "saber_v2.spec").exists()


def test_production_spec_imports_its_dependency_probe() -> None:
    spec = (PROJECT_ROOT / "app.spec").read_text(encoding="utf-8")

    assert "from importlib.util import find_spec" in spec


def test_packaged_interpreter_initializes_standard_streams_as_utf8() -> None:
    tree = ast.parse((PROJECT_ROOT / "app.spec").read_text(encoding="utf-8"))
    executable = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "EXE"
    )

    assert ("X utf8", None, "OPTION") in ast.literal_eval(executable.args[2])


def test_production_spec_does_not_bundle_foreign_windows_icu() -> None:
    spec = (PROJECT_ROOT / "app.spec").read_text(encoding="utf-8")

    assert "os.path.basename(entry[0]).lower() != 'icuuc.dll'" in spec


def test_production_spec_has_no_missing_project_hidden_imports() -> None:
    spec_path = PROJECT_ROOT / "app.spec"
    tree = ast.parse(spec_path.read_text(encoding="utf-8"), filename=str(spec_path))
    hidden_imports: set[str] = set()

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "hiddenimports"
            and isinstance(node.value, ast.List)
        ):
            continue
        hidden_imports.update(
            element.value
            for element in node.value.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and (element.value == "src" or element.value.startswith("src."))
        )

    missing = []
    for module_name in sorted(hidden_imports):
        module_path = PROJECT_ROOT.joinpath(*module_name.split("."))
        if not module_path.is_dir() and not module_path.with_suffix(".py").is_file():
            missing.append(module_name)

    assert missing == []


def test_release_workflow_builds_the_triggering_revision() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "github.event.inputs.branch" not in workflow
    assert "default: 'release'" not in workflow
    # The guard creates/verifies the tag at github.sha before creating the release.
    assert "prepare_release.cjs" in workflow
    assert "if: steps.release_target.outputs.create_release == 'true'" in workflow
    assert "EVENT_NAME: ${{ github.event_name }}" in workflow
    assert 'MANIFEST_VERSION=$(jq -er .version version.json)' in workflow
    assert '"$VERSION" != "$MANIFEST_VERSION"' in workflow
    assert 'STORAGE_VERSION=$(jq -er .storageVersion version.json)' in workflow
    assert 'test -f "src/storage_migrator/schemas/$STORAGE_VERSION.sql"' in workflow
    assert "prerelease: false" in workflow
    # 提交的前端产物用「源码指纹」校验：产物字节跨机器不可复现
    # （Vue 的 scoped 样式 id 由绝对路径派生），所以门禁比较源码指纹而不是 git status。
    assert "npm run check:build-fresh" in workflow
    assert "git status --porcelain -- src/backend_v2/static/vue" not in workflow
    assert "python -m pytest tests_backend -q" in workflow
    assert "npm test" in workflow
    assert "npm run lint:ui:audit" in workflow
    assert 'echo "Build result: ${{ needs.build.result }}"' in workflow
    assert 'if [ "${{ needs.build.result }}" = "success" ]; then' in workflow


def test_the_web_bundle_stamp_is_committed_and_wired_into_the_build() -> None:
    package_json = (PROJECT_ROOT / "vue-frontend" / "package.json").read_text(
        encoding="utf-8"
    )
    assert '"check:build-fresh": "node scripts/stamp-build.mjs --check"' in package_json
    assert "node scripts/stamp-build.mjs" in package_json
    assert (PROJECT_ROOT / "vue-frontend" / "scripts" / "stamp-build.mjs").is_file()
    stamp = (PROJECT_ROOT / "src" / "backend_v2" / "static" / "vue" / "build-info.json").read_text(
        encoding="utf-8"
    )
    source_hash = json.loads(stamp)["sourceHash"]
    assert isinstance(source_hash, str) and len(source_hash) == 64


def test_direct_runtime_dependencies_are_declared() -> None:
    direct_dependencies = {
        "fonttools": "fontTools",
        "img2pdf": "img2pdf",
        "numpy": "numpy",
        "packaging": "packaging",
        "reportlab": "reportlab",
    }
    for filename in ("requirements.txt",):
        requirements = set()
        for line in (PROJECT_ROOT / filename).read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                requirements.add(Requirement(entry).name.lower())
        assert direct_dependencies.keys() <= requirements

    for import_name in direct_dependencies.values():
        assert importlib.util.find_spec(import_name) is not None


def test_release_ships_one_package_from_one_dependency_manifest() -> None:
    """发行包只有一种形态：一份依赖清单、一次构建、一个不带变体后缀的包名。"""
    assert not (PROJECT_ROOT / "requirements-cpu.txt").exists()
    assert not (PROJECT_ROOT / "requirements-gpu.txt").exists()

    workflow = (PROJECT_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert "python -m pip install -r requirements.txt" in workflow
    assert "matrix" not in workflow
    assert '"MiyakoKB-$version"' in workflow
    assert "MiyakoKB-${{ needs.prepare.outputs.version }}.7z*" in workflow
